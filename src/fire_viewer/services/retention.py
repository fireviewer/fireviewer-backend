"""Retention operations owned by the application backend.

The backend never stores evidence binaries.  After a human decision it removes the
remaining transient text/reference metadata synchronously; normalized decision data,
non-reversible hashes and explicitly publishable geometry remain.
"""

from __future__ import annotations

from datetime import datetime

from fire_viewer.db.models import Observation


def purge_observation_transient_metadata(
    observation: Observation,
    *,
    decided_at: datetime,
) -> bool:
    """Purge transient metadata immediately unless a documented hold exists."""

    observation.raw_purge_due_at = decided_at
    if observation.raw_retention_hold_reason:
        return False
    observation.toponyms = []
    observation.canonical_name_hint = None
    observation.external_reference = None
    observation.raw_purged_at = decided_at
    return True
