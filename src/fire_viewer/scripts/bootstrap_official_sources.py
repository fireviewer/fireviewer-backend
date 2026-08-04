from __future__ import annotations

from fire_viewer.core.config import get_settings
from fire_viewer.core.ids import new_prefixed_id
from fire_viewer.db.engine import create_db_engine, create_session_factory
from fire_viewer.services.external_source_registry import (
    register_external_collection,
    register_external_provider,
)
from fire_viewer.services.official_connectors import official_source_definitions


def main() -> None:
    """Idempotently register reviewed provider metadata without making network requests."""

    settings = get_settings()
    engine = create_db_engine(settings)
    factory = create_session_factory(engine)
    actor_id = "official-source-bootstrap"
    try:
        with factory() as session:
            for definition in official_source_definitions():
                provider_enabled = settings.official_connectors_enabled and any(
                    settings.official_connector_collections.get(
                        f"{definition.provider.provider_key}/{collection.collection_key}", {}
                    ).get("enabled")
                    is True
                    for collection in definition.collections
                )
                provider = definition.provider.model_copy(update={"enabled": provider_enabled})
                register_external_provider(
                    session,
                    payload=provider,
                    actor_id=actor_id,
                    trace_id=new_prefixed_id("TRC"),
                )
                for collection in definition.collections:
                    register_external_collection(
                        session,
                        payload=collection,
                        actor_id=actor_id,
                        trace_id=new_prefixed_id("TRC"),
                    )
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
