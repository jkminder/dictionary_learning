import torch as th
import torch.nn as nn
from torch.utils.data import DataLoader
from datasets import Dataset
from nnsight import LanguageModel
from typing import Tuple, List
import numpy as np
import os
from tqdm.auto import tqdm
from multiprocessing import Pool, Manager
import time
import json
from .config import DEBUG

if DEBUG:
    tracer_kwargs = {"scan": True, "validate": True}
else:
    tracer_kwargs = {"scan": False, "validate": False}


class ActivationShard:
    def __init__(self, store_dir: str, shard_idx: int):
        self.shard_file = os.path.join(store_dir, f"shard_{shard_idx}.memmap")
        with open(self.shard_file.replace(".memmap", ".meta"), "r") as f:
            self.shape = tuple(json.load(f)["shape"])
        self.activations = np.memmap(
            self.shard_file, dtype=np.float32, mode="r", shape=self.shape
        )

    def __len__(self):
        return self.activations.shape[0]

    def __getitem__(self, *indices):
        return th.tensor(self.activations[(*indices,)], dtype=th.float32)


def save_shard(activations, store_dir, shard_count, name, io):
    print(f"Storing activation shard ({activations.shape})")
    memmap_file = os.path.join(store_dir, f"shard_{shard_count}.memmap")
    memmap_file_meta = memmap_file.replace(".memmap", ".meta")
    memmap = np.memmap(
        memmap_file,
        dtype=np.float32,
        mode="w+",
        shape=(activations.shape[0], activations.shape[1]),
    )
    memmap[:] = activations.numpy()
    memmap.flush()
    with open(memmap_file_meta, "w") as f:
        json.dump({"shape": list(activations.shape)}, f)
    del memmap
    print(f"Finished storing activations for shard {shard_count}")


class ActivationCache:
    __pool = None
    __active_processes = None
    __process_lock = None
    __manager = None

    def __init__(self, store_dir: str):
        self.store_dir = store_dir
        self.config = json.load(open(os.path.join(store_dir, "config.json"), "r"))
        self.shards = [
            ActivationShard(store_dir, i) for i in range(self.config["shard_count"])
        ]
        self._range_to_shard_idx = np.cumsum([0] + [s.shape[0] for s in self.shards])
        if "store_tokens" in self.config and self.config["store_tokens"]:
            self._tokens = th.load(os.path.join(store_dir, "tokens.pt"), weights_only=True)

    def __len__(self):
        return self.config["total_size"]

    def __getitem__(self, index: int):
        shard_idx = np.searchsorted(self._range_to_shard_idx, index, side="right") - 1
        offset = index - self._range_to_shard_idx[shard_idx]
        shard = self.shards[shard_idx]
        return shard[offset]

    @property
    def tokens(self):
        return self._tokens

    @staticmethod
    def get_activations(submodule: nn.Module, io: str):
        if io == "in":
            return submodule.input[0]
        else:
            return submodule.output[0]

    @staticmethod
    def __init_multiprocessing(max_concurrent_saves: int = 3):
        if ActivationCache.__pool is None:
            ActivationCache.__manager = Manager()
            ActivationCache.__active_processes = ActivationCache.__manager.Value("i", 0)
            ActivationCache.__process_lock = ActivationCache.__manager.Lock()
            ActivationCache.__pool = Pool(processes=max_concurrent_saves)

    @staticmethod
    def cleanup_multiprocessing():
        if ActivationCache.__pool is not None:
            # wait for all processes to finish
            while ActivationCache.__active_processes.value > 0:
                print(
                    f"Waiting for {ActivationCache.__active_processes.value} save processes to finish"
                )
                time.sleep(1)
            ActivationCache.__pool.close()
            ActivationCache.__pool = None
            ActivationCache.__manager.shutdown()
            ActivationCache.__manager = None
            ActivationCache.__active_processes = None
            ActivationCache.__process_lock = None

    @staticmethod
    def collate_store_shards(
        store_dirs: Tuple[str],
        shard_count: int,
        activation_cache: List[th.Tensor],
        submodule_names: Tuple[str],
        shuffle_shards: bool = True,
        io: str = "out",
        multiprocessing: bool = True,
        max_concurrent_saves: int = 3,
    ):
        # Create a process pool if multiprocessing is enabled
        if multiprocessing and ActivationCache.__pool is None:
            ActivationCache.__init_multiprocessing(max_concurrent_saves)

        if multiprocessing:
            pool = ActivationCache.__pool
            active_processes = ActivationCache.__active_processes
            process_lock = ActivationCache.__process_lock

        for i, name in enumerate(submodule_names):
            activations = th.cat(
                activation_cache[i], dim=0
            )  # (N x B x T) x D (N = number of batches per shard)

            if shuffle_shards:
                idx = np.random.permutation(activations.shape[0])
                activations = activations[idx]

            if multiprocessing:
                # Wait if we've reached max concurrent processes
                while active_processes.value >= max_concurrent_saves:
                    time.sleep(0.1)

                # Increment active process count
                with process_lock:
                    active_processes.value += 1

                def callback(result):
                    with process_lock:
                        active_processes.value -= 1

                print(
                    f"Applying async save for shard {shard_count} (current num of workers: {active_processes.value})"
                )
                pool.apply_async(
                    save_shard,
                    args=(activations, store_dirs[i], shard_count, name, io),
                    callback=callback,
                )
            else:
                save_shard(activations, store_dirs[i], shard_count, name, io)

    @staticmethod
    def shard_exists(store_dir: str, shard_count: int):
        if os.path.exists(os.path.join(store_dir, f"shard_{shard_count}.memmap")):
            # load the meta file
            with open(os.path.join(store_dir, f"shard_{shard_count}.meta"), "r") as f:
                shape = json.load(f)["shape"]
            return shape
        else:
            return None

    @th.no_grad()
    @staticmethod
    def collect(
        data: Dataset,
        submodules: Tuple[nn.Module],
        submodule_names: Tuple[str],
        model: LanguageModel,
        store_dir: str,
        batch_size: int = 64,
        context_len: int = 128,
        shard_size: int = 10**6,
        d_model: int = 1024,
        shuffle_shards: bool = False,
        io: str = "out",
        num_workers: int = 8,
        max_total_tokens: int = 10**8,
        last_submodule: nn.Module = None,
        overwrite: bool = False,
        store_tokens: bool = False,
        ignore_first_n_tokens_per_sample: int = 0,
    ):

        dataloader = DataLoader(data, batch_size=batch_size, num_workers=num_workers)

        activation_cache = [[] for _ in submodules]
        tokens_cache = []
        store_dirs = [
            os.path.join(store_dir, f"{submodule_names[i]}_{io}")
            for i in range(len(submodules))
        ]
        for store_dir in store_dirs:
            os.makedirs(store_dir, exist_ok=True)
        total_size = 0
        current_size = 0
        shard_count = 0
        if ignore_first_n_tokens_per_sample > 0:
            model.tokenizer.padding_side = "right"
        for batch in tqdm(dataloader, desc="Collecting activations"):
            tokens = model.tokenizer(
                batch,
                max_length=context_len,
                truncation=True,
                return_tensors="pt",
                padding=True,
            ).to(model.device)
            attention_mask = tokens["attention_mask"]

            store_mask = attention_mask.clone()
            if ignore_first_n_tokens_per_sample > 0:
                store_mask[:, :ignore_first_n_tokens_per_sample] = 0
            if store_tokens:
                tokens_cache.append(tokens["input_ids"].reshape(-1)[store_mask.reshape(-1).bool()])

            shape = ActivationCache.shard_exists(store_dir, shard_count)
            if overwrite or shape is None:
                with model.trace(
                    tokens,
                    **tracer_kwargs,
                ):
                    for i, submodule in enumerate(submodules):
                        local_activations = (
                            ActivationCache.get_activations(submodule, io)
                            .reshape(-1, d_model)
                            .save()
                        )  # (B x T) x D
                        activation_cache[i].append(local_activations)

                    if last_submodule is not None:
                        last_submodule.output.stop()

                for i in range(len(submodules)):
                    activation_cache[i][-1] = (
                        activation_cache[i][-1]
                        .value[store_mask.reshape(-1).bool()]
                        .to(th.float32)
                        .cpu()
                    )  # remove padding tokens
                
                assert len(tokens_cache[-1]) == activation_cache[0][-1].shape[0]
                assert activation_cache[0][-1].shape[0] == store_mask.sum().item()
                current_size += activation_cache[0][-1].shape[0]
            else:
                current_size += store_mask.sum().item()

            if current_size > shard_size:
                if shape is not None and not overwrite:
                    assert shape[0] == sum([token_cache.shape[0] for token_cache in tokens_cache]) - total_size
                    print(f"Shard {shard_count} already exists. Skipping.")
                else:
                    ActivationCache.collate_store_shards(
                        store_dirs,
                        shard_count,
                        activation_cache,
                        submodule_names,
                        shuffle_shards,
                        io,
                        multiprocessing=True,
                    )
                shard_count += 1

                total_size += current_size
                current_size = 0
                activation_cache = [[] for _ in submodules]

            if total_size > max_total_tokens:
                print(f"Max total tokens reached. Stopping collection.")
                break

        if current_size > 0:
            ActivationCache.collate_store_shards(
                store_dirs,
                shard_count,
                activation_cache,
                submodule_names,
                shuffle_shards,
                io,
                multiprocessing=True,
            )

        if store_tokens:
            print(f"Storing tokens...")
            tokens_cache = th.cat(tokens_cache, dim=0)
            assert tokens_cache.shape[0] == total_size
            th.save(tokens_cache, os.path.join(store_dir, "tokens.pt"))

        # store configs
        for i, store_dir in enumerate(store_dirs):
            with open(os.path.join(store_dir, "config.json"), "w") as f:
                json.dump(
                    {
                        "batch_size": batch_size,
                        "context_len": context_len,
                        "shard_size": shard_size,
                        "d_model": d_model,
                        "shuffle_shards": shuffle_shards,
                        "io": io,
                        "total_size": total_size,
                        "shard_count": shard_count,
                        "store_tokens": store_tokens,
                    },
                    f,
                )
        ActivationCache.cleanup_multiprocessing()
        print(f"Finished collecting activations. Total size: {total_size}")


class PairedActivationCache:
    def __init__(self, store_dir_1: str, store_dir_2: str):
        self.activation_cache_1 = ActivationCache(store_dir_1)
        self.activation_cache_2 = ActivationCache(store_dir_2)
        assert len(self.activation_cache_1) == len(self.activation_cache_2)

    def __len__(self):
        return len(self.activation_cache_1)

    def __getitem__(self, index: int):
        return th.stack(
            (self.activation_cache_1[index], self.activation_cache_2[index]), dim=0
        )

    @property
    def tokens(self):
        return th.stack(
            (self.activation_cache_1.tokens, self.activation_cache_2.tokens), dim=0
        )


class ActivationCacheTuple:
    def __init__(self, *store_dirs: str):
        self.activation_caches = [
            ActivationCache(store_dir) for store_dir in store_dirs
        ]
        assert len(self.activation_caches) > 0
        for i in range(1, len(self.activation_caches)):
            assert len(self.activation_caches[i]) == len(self.activation_caches[0])

    def __len__(self):
        return len(self.activation_caches[0])

    def __getitem__(self, index: int):
        return th.stack([cache[index] for cache in self.activation_caches], dim=0)

    @property
    def tokens(self):
        return th.stack([cache.tokens for cache in self.activation_caches], dim=0)
