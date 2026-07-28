"""Create one internal ordered campaign from an immutable V2 manifest."""

from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path

from fire_viewer.core.config import get_settings
from fire_viewer.db.engine import create_db_engine, create_session_factory
from fire_viewer.services.agent_validation_campaigns import create_campaign_from_manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Persist a locked internal validation campaign. "
            "This command does not enqueue a job or contact a pod."
        )
    )
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument(
        "--created-by",
        default=f"internal-cli:{getpass.getuser()}",
        help="Non-secret operator audit identifier.",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = get_settings()
    engine = create_db_engine(settings)
    factory = create_session_factory(engine)
    try:
        with factory() as session:
            campaign = create_campaign_from_manifest(
                session,
                manifest_path=args.manifest,
                created_by=args.created_by,
            )
            print(
                json.dumps(
                    {
                        "campaign_id": campaign.campaign_id,
                        "manifest_sha256": campaign.manifest_sha256,
                        "days": len(campaign.days),
                        "first_state": campaign.days[0].state.value,
                        "jobs_enqueued": 0,
                    },
                    sort_keys=True,
                )
            )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
