from __future__ import annotations

from pathlib import Path

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
    expected_revision = "e5b7c9d2a410"
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
    calls: list[tuple[str, object, str]] = []
    monkeypatch.setattr(
        migrate_vercel,
        "_upgrade_postgresql",
        lambda url, config, revision: calls.append((url, config, revision)),
    )

    migrate_vercel.main()

    assert len(calls) == 1
    assert calls[0][0] == "postgresql://example.invalid/fireviewer"
    assert calls[0][2] == expected_revision


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
            return "e5b7c9d2a410"

    monkeypatch.setattr(
        migrate_vercel.ScriptDirectory,
        "from_config",
        lambda _config: FakeScriptDirectory(),
    )

    with pytest.raises(RuntimeError, match="FV_DATABASE_SCHEMA_REVISION"):
        migrate_vercel.main()
