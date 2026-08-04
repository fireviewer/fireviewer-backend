from __future__ import annotations

import argparse
import os
import socket
import time

import httpx

from fire_viewer.core.config import get_settings
from fire_viewer.db.engine import create_db_engine, create_session_factory
from fire_viewer.services.external_source_scheduler import run_external_source_scheduler_once
from fire_viewer.services.official_connectors import build_official_connector_registry


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Acquire one incident-scoped revision from configured official sources"
    )
    parser.add_argument("--once", action="store_true", help="Process at most one due source plan")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = get_settings()
    if not settings.official_connectors_enabled:
        raise SystemExit(
            "Official connectors are disabled; set FV_OFFICIAL_CONNECTORS_ENABLED=true"
        )
    worker_id = f"official-source-scheduler:{socket.gethostname()}:{os.getpid()}"
    engine = create_db_engine(settings)
    factory = create_session_factory(engine)
    try:
        with httpx.Client(trust_env=False) as client:
            connectors = build_official_connector_registry(settings, client=client)
            while True:
                processed = run_external_source_scheduler_once(
                    factory,
                    settings=settings,
                    worker_id=worker_id,
                    connectors=connectors,
                )
                if args.once:
                    return
                if not processed:
                    time.sleep(settings.agent_poll_interval_seconds)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
