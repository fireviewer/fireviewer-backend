from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher

from fire_viewer.core.config import Settings
from fire_viewer.core.time import as_utc
from fire_viewer.domain.enums import IncidentStatus, MatchDecision, SourceTrust
from fire_viewer.domain.geospatial import combine_uncertainties_m, haversine_m


@dataclass(frozen=True, slots=True)
class Candidate:
    incident_db_id: int
    episode_db_id: int
    fire_id: str
    episode_id: str
    reference_lon: float
    reference_lat: float
    uncertainty_m: float
    canonical_name: str | None
    status: IncidentStatus
    started_at: datetime
    last_observed_at: datetime
    ended_at: datetime | None


@dataclass(frozen=True, slots=True)
class ObservationForMatch:
    longitude: float
    latitude: float
    uncertainty_m: float
    observed_at: datetime
    toponyms: tuple[str, ...]
    canonical_name_hint: str | None
    source_trust: SourceTrust


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    candidate: Candidate
    score: float
    factors: dict[str, float]
    distance_m: float


@dataclass(frozen=True, slots=True)
class MatchResult:
    decision: MatchDecision
    best: ScoredCandidate | None
    second: ScoredCandidate | None
    margin: float | None
    review_reasons: tuple[str, ...]
    candidate_overflow: bool


def _normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return " ".join("".join(ch for ch in normalized if not unicodedata.combining(ch)).split())


def _toponym_score(observation: ObservationForMatch, candidate: Candidate) -> float:
    names = [*observation.toponyms]
    if observation.canonical_name_hint:
        names.append(observation.canonical_name_hint)
    if not names or not candidate.canonical_name:
        return 0.5
    target = _normalize_text(candidate.canonical_name)
    return max(SequenceMatcher(None, _normalize_text(name), target).ratio() for name in names)


def _source_score(trust: SourceTrust) -> float:
    return {
        SourceTrust.UNVERIFIED: 0.35,
        SourceTrust.PARTNER: 0.65,
        SourceTrust.INSTITUTIONAL: 0.90,
        SourceTrust.OPERATOR: 0.95,
    }[trust]


def _time_score(
    observation: ObservationForMatch,
    candidate: Candidate,
    settings: Settings,
) -> float:
    observed_at = as_utc(observation.observed_at)
    last_observed_at = as_utc(candidate.last_observed_at)
    delta_hours = abs((observed_at - last_observed_at).total_seconds()) / 3_600.0

    if candidate.status in {
        IncidentStatus.CANDIDATE,
        IncidentStatus.UNDER_REVIEW,
        IncidentStatus.ACTIVE_CONFIRMED,
        IncidentStatus.MONITORING,
    }:
        return math.exp(-delta_hours / settings.matching_active_time_decay_hours)

    if candidate.status in {IncidentStatus.EXTINGUISHED, IncidentStatus.CLOSED}:
        end = as_utc(candidate.ended_at or candidate.last_observed_at)
        if observed_at < end:
            return 0.25 * math.exp(-abs((end - observed_at).total_seconds()) / 3_600.0 / 24.0)
        gap_hours = (observed_at - end).total_seconds() / 3_600.0
        return 0.80 * math.exp(-gap_hours / settings.matching_closed_time_decay_hours)

    return 0.0


def score_candidate(
    observation: ObservationForMatch,
    candidate: Candidate,
    settings: Settings,
) -> ScoredCandidate:
    distance_m = haversine_m(
        observation.longitude,
        observation.latitude,
        candidate.reference_lon,
        candidate.reference_lat,
    )
    combined_uncertainty = combine_uncertainties_m(
        observation.uncertainty_m, candidate.uncertainty_m
    )
    distance_denominator = combined_uncertainty + settings.matching_distance_scale_m
    distance_score = math.exp(-0.5 * (distance_m / distance_denominator) ** 2)
    time_score = _time_score(observation, candidate, settings)
    toponym_score = _toponym_score(observation, candidate)
    source_score = _source_score(observation.source_trust)

    factors = {
        "distance": round(distance_score, 6),
        "time": round(time_score, 6),
        "toponym": round(toponym_score, 6),
        "source": round(source_score, 6),
    }
    score = 0.45 * distance_score + 0.25 * time_score + 0.15 * toponym_score + 0.15 * source_score
    return ScoredCandidate(
        candidate=candidate,
        score=round(max(0.0, min(1.0, score)), 6),
        factors=factors,
        distance_m=round(distance_m, 3),
    )


def match_observation(
    observation: ObservationForMatch,
    candidates: list[Candidate],
    settings: Settings,
    *,
    candidate_overflow: bool = False,
) -> MatchResult:
    scored = sorted(
        (score_candidate(observation, candidate, settings) for candidate in candidates),
        # Scores are deliberately rounded before they enter the public response.
        # A stable secondary key makes an equal-score review proposal reproducible
        # even when a caller did not preserve database ordering.
        key=lambda item: (
            -item.score,
            item.candidate.fire_id,
            item.candidate.episode_id,
            item.candidate.incident_db_id,
            item.candidate.episode_db_id,
        ),
    )
    best = scored[0] if scored else None
    second = scored[1] if len(scored) > 1 else None
    margin = round(best.score - second.score, 6) if best and second else None

    if candidate_overflow:
        return MatchResult(
            decision=MatchDecision.REVIEW,
            best=best,
            second=second,
            margin=margin,
            review_reasons=("candidate_limit_exceeded",),
            candidate_overflow=True,
        )

    if best is None or best.score < settings.matching_create_below:
        return MatchResult(
            decision=MatchDecision.CREATE,
            best=best,
            second=second,
            margin=margin,
            review_reasons=(),
            candidate_overflow=candidate_overflow,
        )

    reasons: list[str] = []
    if best.candidate.status in {IncidentStatus.SUSPENDED, IncidentStatus.REJECTED}:
        reasons.append("candidate_not_attachable")
    if best.score < settings.matching_auto_attach_above:
        reasons.append("score_below_auto_attach_threshold")
    if second and margin is not None and margin < settings.matching_min_margin:
        reasons.append("candidate_margin_too_low")

    if reasons:
        return MatchResult(
            decision=MatchDecision.REVIEW,
            best=best,
            second=second,
            margin=margin,
            review_reasons=tuple(reasons),
            candidate_overflow=candidate_overflow,
        )

    return MatchResult(
        decision=MatchDecision.ATTACH,
        best=best,
        second=second,
        margin=margin,
        review_reasons=(),
        candidate_overflow=candidate_overflow,
    )
