"""Design-documented location for the shared subprocess runner.

The implementation lives in ``services.subprocess_runner`` to avoid a circular
import: the service singletons import the runner during their own
initialization, and importing anything from this ``services.tools`` package
would run its ``__init__`` (which eagerly builds the registry by importing the
tool wrappers, which import the still-initializing service modules).  This
module re-exports the names so ``services.tools.runner`` resolves as designed.
"""
from __future__ import annotations

from services.subprocess_runner import (
    ConversionCancelled,
    SubprocessAbandoned,
    SubprocessRunner,
    reraise_if_abandoned,
)

__all__ = [
    "ConversionCancelled",
    "SubprocessAbandoned",
    "SubprocessRunner",
    "reraise_if_abandoned",
]
