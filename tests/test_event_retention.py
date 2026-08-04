from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select

from fire_viewer.core.config import Settings
from fire_viewer.db.models import AuditEvent, EvidenceAsset
from fire_viewer.domain.enums import EvidenceAssetState
from fire_viewer.main import create_app
from fire_viewer.services.event_retention import purge_due_event_evidence
from fire_viewer.storage import ObjectStorageError, build_object_store

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_expired_unattached_evidence_is_removed_with_an_audit_report(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        environment="test",
        auth_mode="disabled",
        event_v2_enabled=True,
        event_antivirus_mode="test_clean",
        database_url=f"sqlite:///{tmp_path / 'event-retention.sqlite'}",
        zone_upload_storage_dir=tmp_path / "objects",
        trusted_hosts=["testserver"],
        log_level="CRITICAL",
    )
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "head")
    app = create_app(settings)
    content = b"\xff\xd8\xff" + b"\x00" * 13
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            opened = client.post(
                "/api/v2/evidence/uploads",
                json={
                    "files": [
                        {
                            "file_name": "expired.jpg",
                            "media_type": "image/jpeg",
                            "size_bytes": len(content),
                        }
                    ]
                },
            )
            assert opened.status_code == 201, opened.text
            asset = opened.json()["assets"][0]
            uploaded = client.put(
                "/api/v2/evidence/uploads/"
                f"{opened.json()['upload_id']}/assets/{asset['evidence_asset_id']}",
                content=content,
                headers={"Content-Type": "image/jpeg"},
            )
            assert uploaded.status_code == 204, uploaded.text
        with app.state.session_factory() as session:
            row = session.execute(select(EvidenceAsset)).scalar_one()
            object_uri = row.object_uri
            row.purge_after = datetime.now(UTC) - timedelta(seconds=1)
            session.commit()
        with app.state.session_factory() as session:
            report = purge_due_event_evidence(session, settings=settings)
        assert report.uploads_removed == 1
        assert report.assets_removed == 1
        assert report.bytes_removed == len(content)
        assert report.failures == 0
        with app.state.session_factory() as session:
            row = session.execute(select(EvidenceAsset)).scalar_one()
            audit = session.execute(
                select(AuditEvent).where(AuditEvent.action == "event.evidence_cleanup.completed")
            ).scalar_one()
            assert row.state == EvidenceAssetState.REJECTED
            assert row.purged_at is not None
            assert row.purge_after is None
            assert audit.after_snapshot["size_bytes"] == len(content)
        with pytest.raises(ObjectStorageError):
            build_object_store(settings).head(object_uri)
    finally:
        app.state.engine.dispose()
