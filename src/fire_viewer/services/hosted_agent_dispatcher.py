from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from fire_viewer.core.config import Settings
from fire_viewer.services.agent_dispatcher import build_runpod_client, run_dispatcher_once
from fire_viewer.services.event_dispatcher import run_event_dispatcher_once
from fire_viewer.services.event_retention import purge_due_event_evidence


def process_one_hosted_dispatch(
    factory: sessionmaker[Session],
    *,
    worker_id: str,
    settings: Settings,
) -> bool:
    """Process one persisted dispatch without a resident CPU process."""
    with factory() as cleanup_session:
        purge_due_event_evidence(cleanup_session, settings=settings)
    with build_runpod_client(settings) as client:
        if run_event_dispatcher_once(
            factory,
            worker_id=worker_id,
            settings=settings,
            client=client,
        ):
            return True
        return run_dispatcher_once(
            factory,
            worker_id=worker_id,
            settings=settings,
            client=client,
        )
