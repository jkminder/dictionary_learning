from typing import Optional, Callable
from ..utils import (
    set_decoder_norm_to_unit_norm,
    remove_gradient_parallel_to_decoder_directions,
)
import torch
import einops


class SAETrainer:
    """
    Generic class for implementing SAE training algorithms
    """

    def __init__(self, seed=None):
        self.seed = seed
        self.logging_parameters = []

    def update(
        self,
        step,  # index of step in training
        activations,  # of shape [batch_size, d_submodule]
    ):
        pass  # implemented by subclasses

    def get_logging_parameters(self):
        stats = {}
        for param in self.logging_parameters:
            if hasattr(self, param):
                stats[param] = getattr(self, param)
            else:
                print(f"Warning: {param} not found in {self}")
        return stats

    @property
    def config(self):
        return {
            "wandb_name": "trainer",
        }

    @property
    def model(self):
        return self.ae


def get_lr_schedule(
    total_steps: int,
    warmup_steps: int,
    decay_start: Optional[int] = None,
    resample_steps: Optional[int] = None,
    sparsity_warmup_steps: Optional[int] = None,
) -> Callable[[int], float]:
    """
    Creates a learning rate schedule function with linear warmup followed by an optional decay phase.

    Note: resample_steps creates a repeating warmup pattern instead of the standard phases, but
    is rarely used in practice.

    Args:
        total_steps: Total number of training steps
        warmup_steps: Steps for linear warmup from 0 to 1
        decay_start: Optional step to begin linear decay to 0
        resample_steps: Optional period for repeating warmup pattern
        sparsity_warmup_steps: Used for validation with decay_start

    Returns:
        Function that computes LR scale factor for a given step
    """
    if decay_start is not None:
        assert (
            resample_steps is None
        ), "decay_start and resample_steps are currently mutually exclusive."
        assert 0 <= decay_start < total_steps, "decay_start must be >= 0 and < steps."
        assert decay_start > warmup_steps, "decay_start must be > warmup_steps."
        if sparsity_warmup_steps is not None:
            assert (
                decay_start > sparsity_warmup_steps
            ), "decay_start must be > sparsity_warmup_steps."

    assert 0 <= warmup_steps < total_steps, "warmup_steps must be >= 0 and < steps."

    if resample_steps is None:

        def lr_schedule(step: int) -> float:
            if step < warmup_steps:
                # Warm-up phase
                return step / warmup_steps

            if decay_start is not None and step >= decay_start:
                # Decay phase
                return (total_steps - step) / (total_steps - decay_start)

            # Constant phase
            return 1.0

    else:
        assert (
            0 < resample_steps < total_steps
        ), "resample_steps must be > 0 and < steps."

        def lr_schedule(step: int) -> float:
            return min((step % resample_steps) / warmup_steps, 1.0)

    return lr_schedule


def get_sparsity_warmup_fn(
    total_steps: int, sparsity_warmup_steps: Optional[int] = None
) -> Callable[[int], float]:
    """
    Return a function that computes a scale factor for sparsity penalty at a given step.

    If `sparsity_warmup_steps` is None or 0, returns 1.0 for all steps.
    Otherwise, scales from 0.0 up to 1.0 across `sparsity_warmup_steps`.
    """

    if sparsity_warmup_steps is not None:
        assert (
            0 <= sparsity_warmup_steps < total_steps
        ), "sparsity_warmup_steps must be >= 0 and < steps."

    def scale_fn(step: int) -> float:
        if not sparsity_warmup_steps:
            # If it's None or zero, we just return 1.0
            return 1.0
        else:
            # Gradually increase from 0.0 -> 1.0 as step goes from 0 -> sparsity_warmup_steps
            return min(step / sparsity_warmup_steps, 1.0)

    return scale_fn
