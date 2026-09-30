"""Pipeline failure type."""

from __future__ import annotations


class PipelineError(RuntimeError):
    """A reproducible pipeline validation or execution failure."""
