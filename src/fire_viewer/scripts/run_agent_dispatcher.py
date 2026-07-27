from __future__ import annotations

import argparse
import os
import socket
import time

from fire_viewer.core.config import get_settings
from fire_viewer.db.engine import create_db_engine, create_session_factory
from fire_viewer.services.agent_dispatcher import build_runpod_client, run_dispatcher_once


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dispatch private agent media batches to RunPod")
    parser.add_argument("--once", action="store_true", help="Process at most one due dispatch")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    settings = get_settings()
    if not settings.agent_dispatch_enabled:
        raise SystemExit("Agent dispatch is disabled; set FV_AGENT_DISPATCH_ENABLED=true")
    worker_id = f"agent-dispatcher:{socket.gethostname()}:{os.getpid()}"
    engine = create_db_engine(settings)
    factory = create_session_factory(engine)
    try:
        with build_runpod_client(settings) as client:
            while True:
                processed = run_dispatcher_once(
                    factory,
                    worker_id=worker_id,
                    settings=settings,
                    client=client,
                )
                if args.once:
                    return
                if not processed:
                    time.sleep(settings.agent_poll_interval_seconds)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
