from datetime import UTC, datetime, timedelta

from fire_viewer.core.config import Settings
from fire_viewer.domain.enums import IncidentStatus, MatchDecision, SourceTrust
from fire_viewer.domain.matching import Candidate, ObservationForMatch, match_observation


def test_candidate_overflow_forces_review_even_when_visible_candidates_score_low() -> None:
    now = datetime.now(UTC)
    settings = Settings(_env_file=None, environment="test", auth_mode="disabled")
    observation = ObservationForMatch(
        longitude=6.0,
        latitude=43.0,
        uncertainty_m=50.0,
        observed_at=now,
        toponyms=("Unrelated place",),
        canonical_name_hint=None,
        source_trust=SourceTrust.UNVERIFIED,
    )
    candidates = [
        Candidate(
            incident_db_id=1,
            episode_db_id=1,
            fire_id="FR-83-00001",
            episode_id="E01",
            reference_lon=7.0,
            reference_lat=44.0,
            uncertainty_m=50.0,
            canonical_name="Another place",
            status=IncidentStatus.MONITORING,
            started_at=now - timedelta(hours=1),
            last_observed_at=now - timedelta(minutes=30),
            ended_at=None,
        )
    ]

    result = match_observation(
        observation,
        candidates,
        settings,
        candidate_overflow=True,
    )

    assert result.decision == MatchDecision.REVIEW
    assert result.review_reasons == ("candidate_limit_exceeded",)


def test_equal_score_candidates_have_a_stable_public_proposal_order() -> None:
    now = datetime.now(UTC)
    settings = Settings(_env_file=None, environment="test", auth_mode="disabled")
    observation = ObservationForMatch(
        longitude=6.0214,
        latitude=43.2897,
        uncertainty_m=250.0,
        observed_at=now,
        toponyms=("Massif des Maures",),
        canonical_name_hint="Massif des Maures",
        source_trust=SourceTrust.UNVERIFIED,
    )
    first = Candidate(
        incident_db_id=10,
        episode_db_id=10,
        fire_id="FR-83-00010",
        episode_id="E01",
        reference_lon=6.0214,
        reference_lat=43.2897,
        uncertainty_m=250.0,
        canonical_name="Massif des Maures",
        status=IncidentStatus.MONITORING,
        started_at=now - timedelta(hours=1),
        last_observed_at=now,
        ended_at=None,
    )
    second = Candidate(
        incident_db_id=11,
        episode_db_id=11,
        fire_id="FR-83-00011",
        episode_id="E01",
        reference_lon=6.0214,
        reference_lat=43.2897,
        uncertainty_m=250.0,
        canonical_name="Massif des Maures",
        status=IncidentStatus.MONITORING,
        started_at=now - timedelta(hours=1),
        last_observed_at=now,
        ended_at=None,
    )

    forward = match_observation(observation, [first, second], settings)
    reverse = match_observation(observation, [second, first], settings)

    assert forward.decision == reverse.decision == MatchDecision.REVIEW
    assert forward.best and reverse.best
    assert forward.second and reverse.second
    assert forward.best.candidate.fire_id == reverse.best.candidate.fire_id == "FR-83-00010"
    assert forward.second.candidate.fire_id == reverse.second.candidate.fire_id == "FR-83-00011"
    assert forward.margin == reverse.margin == 0.0
