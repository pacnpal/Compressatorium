"""RomM integration services.

Four concerns, one package, in the order a conversion meets them:

* :mod:`~services.romm.settings` — what instance, which library, and every
  in-app preference the other three read;
* :mod:`~services.romm.client` — the HTTP surface: the bounded, redirect-safe
  opener, the path mapper that turns a RomM record into a local path, and the
  multipart writer that pushes metadata back;
* :mod:`~services.romm.auto` — the unattended sweep: per-platform rules, their
  schedules and filters, and the provenance that makes a rule idempotent;
* :mod:`~services.romm.repin` — the metadata queue: what was recorded before a
  conversion, and how it is re-applied once RomM rescans.

The client's names are re-exported here because they are the package's public
face — ``from services.romm import romm_client`` reads better at a call site
than the module path, and it is what every caller already used when this was a
single module.
"""
from .client import (
    DAT_SAFE_OUTPUT_EXTS,
    METADATA_ID_FIELDS,
    RommClient,
    RommError,
    RommNotConfigured,
    romm_client,
)

__all__ = [
    "DAT_SAFE_OUTPUT_EXTS",
    "METADATA_ID_FIELDS",
    "RommClient",
    "RommError",
    "RommNotConfigured",
    "romm_client",
]
