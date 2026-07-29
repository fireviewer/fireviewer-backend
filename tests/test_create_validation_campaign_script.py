from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from fire_viewer.scripts.create_validation_campaign import _campaign_summary


def test_campaign_summary_exposes_immutable_upload_window_ids() -> None:
    campaign = SimpleNamespace(
        campaign_id="fontainebleau-trevillach-july-2026-v2",
        manifest_sha256="a" * 64,
        days=[
            SimpleNamespace(
                ordinal=1,
                state=SimpleNamespace(value="ready"),
                manifest_sha256="b" * 64,
                analysis_window=SimpleNamespace(
                    analysis_id="analysis-window-fontainebleau-2026-07-12",
                    local_date=date(2026, 7, 12),
                    incident=SimpleNamespace(fire_id="FR-77-00001"),
                ),
            )
        ],
    )

    assert _campaign_summary(campaign) == {
        "campaign_id": "fontainebleau-trevillach-july-2026-v2",
        "manifest_sha256": "a" * 64,
        "days": [
            {
                "ordinal": 1,
                "fire_id": "FR-77-00001",
                "local_date": "2026-07-12",
                "analysis_window_id": "analysis-window-fontainebleau-2026-07-12",
                "state": "ready",
                "manifest_sha256": "b" * 64,
            }
        ],
        "jobs_enqueued": 0,
    }
