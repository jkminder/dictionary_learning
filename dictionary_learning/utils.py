from datasets import load_dataset
import zstandard as zstd
import io
import json
import numpy as np
import torch as th


def hf_dataset_to_generator(dataset_name, split="train", streaming=True):
    dataset = load_dataset(dataset_name, split=split, streaming=streaming)

    def gen():
        for x in iter(dataset):
            yield x["text"]

    return gen()


def zst_to_generator(data_path):
    """
    Load a dataset from a .jsonl.zst file.
    The jsonl entries is assumed to have a 'text' field
    """
    compressed_file = open(data_path, "rb")
    dctx = zstd.ZstdDecompressor()
    reader = dctx.stream_reader(compressed_file)
    text_stream = io.TextIOWrapper(reader, encoding="utf-8")

    def generator():
        for line in text_stream:
            yield json.loads(line)["text"]

    return generator()


NUMPY_TO_TORCH_DTYPE_DICT = {
    np.bool: th.bool,
    np.uint8: th.uint8,
    np.int8: th.int8,
    np.int16: th.int16,
    np.int32: th.int32,
    np.int64: th.int64,
    np.float16: th.float16,
    np.float32: th.float32,
    np.float64: th.float64,
    np.complex64: th.complex64,
    np.complex128: th.complex128,
}

TORCH_TO_NUMPY_DTYPE_DICT = {v: k for k, v in NUMPY_TO_TORCH_DTYPE_DICT.items()}


def numpy_to_torch_dtype(np_dtype):
    return NUMPY_TO_TORCH_DTYPE_DICT[np_dtype]


def torch_to_numpy_dtype(th_dtype):
    return TORCH_TO_NUMPY_DTYPE_DICT[th_dtype]


def dtype_to_str(dtype):
    if isinstance(dtype, th.dtype):
        return str(dtype)
    elif isinstance(dtype, np.dtype):
        return dtype.str
    else:
        raise ValueError(f"Unsupported dtype: {dtype}")


def str_to_dtype(dtype_str):
    if dtype_str.startswith("torch."):
        return getattr(th, dtype_str.split(".")[-1])
    else:
        return np.dtype(dtype_str)
