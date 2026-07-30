from __future__ import annotations

from pathlib import Path
from unittest.mock import ANY

import pytest

from fire_viewer.scripts import migrate_vercel


def test_migration_gate_skips_non_production(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("VERCEL_TARGET_ENV", "preview")
    monkeypatch.delenv("FV_DATABASE_URL", raising=False)

    migrate_vercel.main()

    assert "skipped outside production" in capsys.readouterr().out


def test_migration_gate_rejects_missing_production_database_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VERCEL_TARGET_ENV", "production")
    monkeypatch.delenv("FV_DATABASE_URL", raising=False)

    with pytest.raises(RuntimeError, match="FV_DATABASE_URL"):
        migrate_vercel.main()


def test_migration_gate_uses_packaged_head(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected_revision = "a6c9d1e4f720"
    monkeypatch.setenv("VERCEL_TARGET_ENV", "production")
    monkeypatch.setenv("FV_DATABASE_URL", "postgresql://example.invalid/fireviewer")
    monkeypatch.setenv("FV_DATABASE_SCHEMA_REVISION", expected_revision)
    monkeypatch.setattr(migrate_vercel, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        migrate_vercel,
        "_alembic_config",
        lambda _root: object(),
    )

    class FakeScriptDirectory:
        def get_current_head(self) -> str:
            return expected_revision

    monkeypatch.setattr(
        migrate_vercel.ScriptDirectory,
        "from_config",
        lambda _config: FakeScriptDirectory(),
    )
    calls: list[tuple[str, object, str, Path]] = []
    monkeypatch.setattr(
        migrate_vercel,
        "_upgrade_postgresql",
        lambda url, config, revision, *, project_root: calls.append(
            (url, config, revision, project_root)
        ),
    )

    migrate_vercel.main()

    assert len(calls) == 1
    assert calls[0][0] == "postgresql://example.invalid/fireviewer"
    assert calls[0][2] == expected_revision
    assert calls[0][3] == tmp_path


def test_migration_gate_rejects_stale_runtime_revision(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("VERCEL_TARGET_ENV", "production")
    monkeypatch.setenv("FV_DATABASE_URL", "postgresql://example.invalid/fireviewer")
    monkeypatch.setenv("FV_DATABASE_SCHEMA_REVISION", "db7c2e4f9a10")
    monkeypatch.setattr(migrate_vercel, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        migrate_vercel,
        "_alembic_config",
        lambda _root: object(),
    )

    class FakeScriptDirectory:
        def get_current_head(self) -> str:
            return "f4b7d2c9a610"

    monkeypatch.setattr(
        migrate_vercel.ScriptDirectory,
        "from_config",
        lambda _config: FakeScriptDirectory(),
    )

    with pytest.raises(RuntimeError, match="FV_DATABASE_SCHEMA_REVISION"):
        migrate_vercel.main()


def test_die_retrospective_restoration_is_disabled_without_explicit_flag(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv("FV_RESTORE_DIE_RETROSPECTIVE", raising=False)

    migrate_vercel._restore_die_retrospective_if_requested(object(), tmp_path)


def test_die_retrospective_restoration_uses_only_the_packaged_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("FV_RESTORE_DIE_RETROSPECTIVE", "1")
    expected_dataset = (
        tmp_path / "src" / "fire_viewer" / "retrospectives" / "die-2026-v1.json"
    )
    payload = {"dataset_id": "die-2026-v1"}
    calls: list[tuple[object, dict[str, str], str, bool]] = []

    class FakeSession:
        def __enter__(self) -> object:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(migrate_vercel, "Session", lambda *, bind: FakeSession())
    monkeypatch.setattr(
        migrate_vercel,
        "_load_payload",
        lambda dataset: payload if dataset == expected_dataset else {},
    )
    monkeypatch.setattr(
        migrate_vercel,
        "restore",
        lambda session, actual_payload, *, actor, apply: calls.append(
            (session, actual_payload, actor, apply)
        ) or {"mode": "applied"},
    )

    migrate_vercel._restore_die_retrospective_if_requested(object(), tmp_path)

    assert calls == [
        (
            ANY,
            payload,
            "fireviewer-retrospective-recovery",
            True,
        )
    ]
