from __future__ import annotations

from fire_viewer.scripts.restore_die_retrospective import _next_revision


def test_next_zone_revision_filters_by_incident_episode_and_zone_kind() -> None:
    class RecordingSession:
        statement: object | None = None

        def scalar(self, statement: object) -> int:
            self.statement = statement
            return 4

    session = RecordingSession()

    assert _next_revision(session, 11, 22, "burned") == 5
    statement = str(session.statement)
    assert "active_fire_zone_revision.incident_id" in statement
    assert "active_fire_zone_revision.episode_id" in statement
    assert "active_fire_zone_revision.zone_kind" in statement
