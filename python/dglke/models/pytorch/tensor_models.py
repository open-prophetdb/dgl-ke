# -*- coding: utf-8 -*-
#
# tensor_models.py
#
# Copyright 2020 Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""
KG Sparse embedding
"""
import os
import numpy as np
import pandas as pd

import torch as th
import torch.nn as nn
import torch.nn.functional as functional
import torch.nn.init as INIT

import torch.multiprocessing as mp
from torch.multiprocessing import Queue
from _thread import start_new_thread
import traceback
from functools import wraps

from .. import *

logsigmoid = functional.logsigmoid


def abs(val):
    return th.abs(val)


def masked_select(input, mask):
    return th.masked_select(input, mask)


def get_dev(gpu):
    return th.device("cpu") if gpu < 0 else th.device("cuda:" + str(gpu))


def get_device(args):
    return (
        th.device("cpu") if args.gpu[0] < 0 else th.device("cuda:" + str(args.gpu[0]))
    )


none = lambda x: x
norm = lambda x, p: x.norm(p=p) ** p
get_scalar = lambda x: x.detach().item()
reshape = lambda arr, x, y: arr.view(x, y)
cuda = lambda arr, gpu: arr.cuda(gpu)


def l2_dist(x, y, pw=False):
    if pw is False:
        x = x.unsqueeze(1)
        y = y.unsqueeze(0)

    return -th.norm(x - y, p=2, dim=-1)


def l1_dist(x, y, pw=False):
    if pw is False:
        x = x.unsqueeze(1)
        y = y.unsqueeze(0)

    return -th.norm(x - y, p=1, dim=-1)


def dot_dist(x, y, pw=False):
    if pw is False:
        x = x.unsqueeze(1)
        y = y.unsqueeze(0)

    return th.sum(x * y, dim=-1)


def cosine_dist(x, y, pw=False):
    score = dot_dist(x, y, pw)

    x = x.norm(p=2, dim=-1)
    y = y.norm(p=2, dim=-1)
    if pw is False:
        x = x.unsqueeze(1)
        y = y.unsqueeze(0)

    return score / (x * y)


def extended_jaccard_dist(x, y, pw=False):
    score = dot_dist(x, y, pw)

    x = x.norm(p=2, dim=-1) ** 2
    y = y.norm(p=2, dim=-1) ** 2
    if pw is False:
        x = x.unsqueeze(1)
        y = y.unsqueeze(0)

    return score / (x + y - score)


def floor_divide(input, other):
    return th.floor_divide(input, other)


def thread_wrapped_func(func):
    """Wrapped func for torch.multiprocessing.Process.

    With this wrapper we can use OMP threads in subprocesses
    otherwise, OMP_NUM_THREADS=1 is mandatory.

    How to use:
    @thread_wrapped_func
    def func_to_wrap(args ...):
    """

    @wraps(func)
    def decorated_function(*args, **kwargs):
        queue = Queue()

        def _queue_result():
            exception, trace, res = None, None, None
            try:
                res = func(*args, **kwargs)
            except Exception as e:
                exception = e
                trace = traceback.format_exc()
            queue.put((res, exception, trace))

        start_new_thread(_queue_result, ())
        result, exception, trace = queue.get()
        if exception is None:
            return result
        else:
            assert isinstance(exception, Exception)
            raise exception.__class__(trace)

    return decorated_function


@thread_wrapped_func
def async_update(args, emb, queue):
    """Asynchronous embedding update for entity embeddings.
    How it works:
        1. trainer process push entity embedding update requests into the queue.
        2. async_update process pull requests from the queue, calculate
           the gradient state and gradient and write it into entity embeddings.

    Parameters
    ----------
    args :
        Global confis.
    emb : ExternalEmbedding
        The entity embeddings.
    queue:
        The request queue.
    """
    th.set_num_threads(args.num_thread)
    while True:
        (grad_indices, grad_values, gpu_id) = queue.get()
        clr = emb.args.lr
        if grad_indices is None:
            return
        with th.no_grad():
            grad_sum = (grad_values * grad_values).mean(1)
            device = emb.state_sum.device
            if device != grad_indices.device:
                grad_indices = grad_indices.to(device)
            if device != grad_sum.device:
                grad_sum = grad_sum.to(device)

            emb.state_sum.index_add_(0, grad_indices, grad_sum)
            std = emb.state_sum[grad_indices]  # _sparse_mask
            if gpu_id >= 0:
                std = std.cuda(gpu_id)
            std_values = std.sqrt_().add_(1e-10).unsqueeze(1)
            tmp = -clr * grad_values / std_values
            if tmp.device != device:
                tmp = tmp.to(device)
            emb.emb.index_add_(0, grad_indices, tmp)


class InferEmbedding:
    def __init__(self, device):
        self.device = device

    def load(self, path, name):
        """Load embeddings.

        Parameters
        ----------
        path : str
            Directory to load the embedding.
        name : str
            Embedding name.
        """
        file_name = os.path.join(path, name + ".npy")
        self.emb = th.Tensor(np.load(file_name))

    def load_emb(self, emb_array):
        """Load embeddings from numpy array.

        Parameters
        ----------
        emb_array : numpy.array  or torch.tensor
            Embedding array in numpy array or torch.tensor
        """
        if isinstance(emb_array, np.ndarray):
            self.emb = th.Tensor(emb_array)
        else:
            self.emb = emb_array

    def __call__(self, idx):
        return self.emb[idx].to(self.device)


class ExternalEmbedding:
    """Sparse Embedding for Knowledge Graph
    It is used to store both entity embeddings and relation embeddings.

    Parameters
    ----------
    args :
        Global configs.
    num : int
        Number of embeddings.
    dim : int
        Embedding dimention size.
    device : th.device
        Device to store the embedding.
    """

    def __init__(self, args, num, dim, device):
        self.gpu = args.gpu
        self.args = args
        self.num = num
        self.trace = []

        self.emb = th.empty(num, dim, dtype=th.float32, device=device)
        self.state_sum = self.emb.new().resize_(self.emb.size(0)).zero_()
        self.state_step = 0
        self.has_cross_rel = False
        # queue used by asynchronous update
        self.async_q = None
        # asynchronous update process
        self.async_p = None

    def init(self, emb_init):
        """Initializing the embeddings.

        Parameters
        ----------
        emb_init : float
            The intial embedding range should be [-emb_init, emb_init].
        """
        INIT.uniform_(self.emb, -emb_init, emb_init)
        INIT.zeros_(self.state_sum)

    def setup_cross_rels(self, cross_rels, global_emb):
        cpu_bitmap = th.zeros((self.num,), dtype=th.bool)
        for i, rel in enumerate(cross_rels):
            cpu_bitmap[rel] = 1
        self.cpu_bitmap = cpu_bitmap
        self.has_cross_rel = True
        self.global_emb = global_emb

    def get_noncross_idx(self, idx):
        cpu_mask = self.cpu_bitmap[idx]
        gpu_mask = ~cpu_mask
        return idx[gpu_mask]

    def share_memory(self):
        """Use torch.tensor.share_memory_() to allow cross process tensor access"""
        self.emb.share_memory_()
        self.state_sum.share_memory_()

    def __call__(self, idx, gpu_id=-1, trace=True):
        """Return sliced tensor.

        Parameters
        ----------
        idx : th.tensor
            Slicing index
        gpu_id : int
            Which gpu to put sliced data in.
        trace : bool
            If True, trace the computation. This is required in training.
            If False, do not trace the computation.
            Default: True
        """
        if self.has_cross_rel:
            cpu_idx = idx.cpu()
            cpu_mask = self.cpu_bitmap[cpu_idx]
            cpu_idx = cpu_idx[cpu_mask]
            cpu_idx = th.unique(cpu_idx)
            if cpu_idx.shape[0] != 0:
                cpu_emb = self.global_emb.emb[cpu_idx]
                self.emb[cpu_idx] = cpu_emb.cuda(gpu_id)
        s = self.emb[idx]
        if gpu_id >= 0:
            s = s.cuda(gpu_id)
        # During the training, we need to trace the computation.
        # In this case, we need to record the computation path and compute the gradients.
        if trace:
            data = s.clone().detach().requires_grad_(True)
            self.trace.append((idx, data))
        else:
            data = s
        return data

    def read_emb_from_disk(self, idx_ids: pd.DataFrame, emb_file: pd.DataFrame):
        """Read embeddings from disk.

        Args:
            idx_ids: the ids of the entities or relations to be read
            emb_file: the file containing the embeddings, which must have the following columns: embedding_id, embedding, relation_type for relations, and embedding_id, embedding, entity_id, entity_name, entity_type for entities

        Returns:
            np.array: the embeddings in the same order as idx_ids
        """
        assert "id" in idx_ids.columns, "idx_ids must have a column called id"
        assert "idx" in idx_ids.columns, "idx_ids must have a column called idx"

        embedding_df = pd.read_csv(emb_file, sep="\t")
        embedding_df["embedding"] = embedding_df["embedding"].apply(
            lambda x: np.array([np.float32(i) for i in x.split("|")])
        )

        if "relation_type" in embedding_df.columns:
            embedding_df = embedding_df.rename(columns={"relation_type": "id"})
        elif (
            "entity_id" in embedding_df.columns
            and "entity_type" in embedding_df.columns
        ):
            embedding_df["id"] = (
                embedding_df["entity_type"] + "::" + embedding_df["entity_id"]
            )

        # Retrieve the length of the embeddings from emb_df
        # Assuming all embeddings have the same length
        embedding_length = len(embedding_df["embedding"].iloc[0])

        # Merge the dataframes on 'id', preserving the order of idx_ids
        merged_df = pd.merge(idx_ids, embedding_df, on="id", how="left")

        def isna(x):
            if isinstance(x, np.ndarray):
                return np.isnan(x).any()
            elif isinstance(x, list):
                return pd.isna(x).any()
            elif isinstance(x, float):
                return pd.isna(x)
            else:
                raise ValueError(f"Unknown type: {type(x)}")

        # Sort the merged dataframe by 'index' to ensure the order is correct
        merged_df = merged_df.sort_values(by="idx")

        print("Merge external embeddings with initialized embs.")
        print(
            f"Number of missing embeddings: {merged_df['embedding'].apply(isna).sum()}"
        )

        # Extract the 'embedding' column as a list
        ordered_embeddings = merged_df["embedding"].tolist()

        cloned_emb = self.emb.clone()

        # Update self.emb with the ordered embeddings when the embeddings are not nan
        for idx, emb in enumerate(ordered_embeddings):
            if not isna(emb):
                # print(f"Updating embedding for {merged_df['id'].iloc[idx]}")
                cloned_emb[idx] = th.Tensor(emb)
            else:
                print(
                    f"Missing embedding for {merged_df['id'].iloc[idx]}, using embeding from random initialization instead."
                )

        emb_filepath = emb_file.replace(".tsv", "_ordered.tsv")
        merged_df["embedding"] = merged_df["embedding"].apply(
            lambda x: "|".join([str(i) for i in x]) if not isna(x) else ""
        )
        merged_df.to_csv(emb_filepath, sep="\t", index=False)

        return cloned_emb

    def load_emb(self, emb_array):
        """Load embeddings from numpy array.

        Parameters
        ----------
        emb_array : numpy.array  or torch.tensor
            Embedding array in numpy array or torch.tensor
        """
        if isinstance(emb_array, np.ndarray):
            self.emb = th.Tensor(emb_array)
        else:
            self.emb = emb_array

    def update(self, gpu_id=-1):
        """Update embeddings in a sparse manner
        Sparse embeddings are updated in mini batches. we maintains gradient states for
        each embedding so they can be updated separately.

        Parameters
        ----------
        gpu_id : int
            Which gpu to accelerate the calculation. if -1 is provided, cpu is used.
        """
        self.state_step += 1
        with th.no_grad():
            for idx, data in self.trace:
                grad = data.grad.data

                clr = self.args.lr
                # clr = self.args.lr / (1 + (self.state_step - 1) * group['lr_decay'])

                # the update is non-linear so indices must be unique
                grad_indices = idx
                grad_values = grad
                if self.async_q is not None:
                    grad_indices.share_memory_()
                    grad_values.share_memory_()
                    self.async_q.put((grad_indices, grad_values, gpu_id))
                else:
                    grad_sum = (grad_values * grad_values).mean(1)
                    device = self.state_sum.device
                    if device != grad_indices.device:
                        grad_indices = grad_indices.to(device)
                    if device != grad_sum.device:
                        grad_sum = grad_sum.to(device)

                    if self.has_cross_rel:
                        cpu_mask = self.cpu_bitmap[grad_indices]
                        cpu_idx = grad_indices[cpu_mask]
                        if cpu_idx.shape[0] > 0:
                            cpu_grad = grad_values[cpu_mask]
                            cpu_sum = grad_sum[cpu_mask].cpu()
                            cpu_idx = cpu_idx.cpu()
                            self.global_emb.state_sum.index_add_(0, cpu_idx, cpu_sum)
                            std = self.global_emb.state_sum[cpu_idx]
                            if gpu_id >= 0:
                                std = std.cuda(gpu_id)
                            std_values = std.sqrt_().add_(1e-10).unsqueeze(1)
                            tmp = -clr * cpu_grad / std_values
                            tmp = tmp.cpu()
                            self.global_emb.emb.index_add_(0, cpu_idx, tmp)
                    self.state_sum.index_add_(0, grad_indices, grad_sum)
                    std = self.state_sum[grad_indices]  # _sparse_mask
                    if gpu_id >= 0:
                        std = std.cuda(gpu_id)
                    std_values = std.sqrt_().add_(1e-10).unsqueeze(1)
                    tmp = -clr * grad_values / std_values
                    if tmp.device != device:
                        tmp = tmp.to(device)
                    # TODO(zhengda) the overhead is here.
                    self.emb.index_add_(0, grad_indices, tmp)
        self.trace = []

    def create_async_update(self):
        """Set up the async update subprocess."""
        self.async_q = Queue(1)
        self.async_p = mp.Process(
            target=async_update, args=(self.args, self, self.async_q)
        )
        self.async_p.start()

    def finish_async_update(self):
        """Notify the async update subprocess to quit."""
        self.async_q.put((None, None, None))
        self.async_p.join()

    def curr_emb(self):
        """Return embeddings in trace."""
        data = [data for _, data in self.trace]
        return th.cat(data, 0)

    def save(self, path, name):
        """Save embeddings.

        Parameters
        ----------
        path : str
            Directory to save the embedding.
        name : str
            Embedding name.
        """
        file_name = os.path.join(path, name + ".npy")
        np.save(file_name, self.emb.cpu().detach().numpy())

    def load(self, path, name):
        """Load embeddings.

        Parameters
        ----------
        path : str
            Directory to load the embedding.
        name : str
            Embedding name.
        """
        file_name = os.path.join(path, name + ".npy")
        self.emb = th.Tensor(np.load(file_name))
