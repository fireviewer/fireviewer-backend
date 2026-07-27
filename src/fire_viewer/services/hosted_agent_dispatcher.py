from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from fire_viewer.core.config import Settings
from fire_viewer.services.agent_dispatcher import build_runpod_client, run_dispatcher_once


def process_one_hosted_dispatch(
    factory: sessionmaker[Session],
    *,
    worker_id: str,
    settings: Settings,
) -> bool:
    """Process one persisted dispatch without a resident CPU process."""
    with build_runpod_client(settings) as client:
        return run_dispatcher_once(
            factory,
            worker_id=worker_id,
            settings=settings,
            client=client,
        )
