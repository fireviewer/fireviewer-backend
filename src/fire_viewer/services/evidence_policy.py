"""Deterministic evidence policy for one incident episode.

Corroboration is deliberately not an AI score.  It is the maximum set of attached
observations whose enabled sources and evidence hashes are both distinct.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from fire_viewer.core.time import as_utc
from fire_viewer.db.models import Episode, IncidentSeries, Observation, Source
from fire_viewer.domain.enums import EvidenceSpatialMode, VerificationState
from fire_viewer.domain.public_visibility import canonical_public_visibility

_ELIGIBLE_STATES = frozenset(
    {
        VerificationState.PENDING_REVIEW,
        VerificationState.CORROBORATED,
        VerificationState.VERIFIED,
    }
)
_STATE_PRIORITY = {
    VerificationState.VERIFIED: 0,
    VerificationState.CORROBORATED: 1,
    VerificationState.PENDING_REVIEW: 2,
}


@dataclass(frozen=True, slots=True)
class EvidencePolicyResult:
    previous_state: VerificationState
    verification_state: VerificationState
    independent_proof_count: int
    selected_observation_ids: tuple[str, ...]
    episode_changed: bool
    incident_visibility_changed: bool

    @property
    def became_corroborated(self) -> bool:
        return (
            self.previous_state != VerificationState.CORROBORATED
            and self.verification_state == VerificationState.CORROBORATED
        )


def _maximum_independent_set(observations: list[Observation]) -> list[Observation]:
    """Return a maximum matching between source identities and evidence hashes."""

    by_source: dict[int, list[Observation]] = {}
    for observation in sorted(
        observations,
        key=lambda item: (
            _STATE_PRIORITY[item.verification_state],
            as_utc(item.observed_at),
            item.observation_id,
        ),
    ):
        source_rows = by_source.setdefault(observation.source_id, [])
        if all(row.evidence_hash != observation.evidence_hash for row in source_rows):
            source_rows.append(observation)

    matched_by_hash: dict[str, Observation] = {}

    def augment(source_id: int, visited_hashes: set[str]) -> bool:
        for observation in by_source[source_id]:
            if observation.evidence_hash in visited_hashes:
                continue
            visited_hashes.add(observation.evidence_hash)
            current = matched_by_hash.get(observation.evidence_hash)
            if current is None or augment(current.source_id, visited_hashes):
                matched_by_hash[observation.evidence_hash] = observation
                return True
        return False

    for source_id in sorted(by_source):
        augment(source_id, set())
    return sorted(matched_by_hash.values(), key=lambda item: item.observation_id)


def recalculate_episode_evidence(
    session: Session,
    *,
    incident: IncidentSeries,
    episode: Episode,
    threshold: int,
    now: datetime,
    human_validated_at: datetime | None = None,
) -> EvidencePolicyResult:
    """Recalculate public evidence state and mutate only explicit derived fields."""

    observations = list(
        session.execute(
            select(Observation)
            .join(Source, Source.id == Observation.source_id)
            .where(
                Observation.attached_episode_id == episode.id,
                Observation.verification_state.in_(_ELIGIBLE_STATES),
                Source.enabled.is_(True),
            )
            .order_by(Observation.observed_at.asc(), Observation.observation_id.asc())
        ).scalars()
    )
    selected = _maximum_independent_set(observations)
    selected_ids = {observation.id for observation in selected}
    verified_exists = any(
        observation.verification_state == VerificationState.VERIFIED for observation in observations
    )
    previous_state = episode.verification_state
    if verified_exists:
        next_state = VerificationState.VERIFIED
    elif len(selected) >= threshold:
        next_state = VerificationState.CORROBORATED
    else:
        next_state = VerificationState.UNVERIFIED

    observations_changed = False
    if next_state == VerificationState.CORROBORATED:
        for observation in observations:
            next_observation_state = (
                VerificationState.CORROBORATED
                if observation.id in selected_ids
                else VerificationState.PENDING_REVIEW
            )
            next_spatial_mode = (
                EvidenceSpatialMode.GENERALIZED
                if observation.id in selected_ids
                else EvidenceSpatialMode.WITHHELD
            )
            if (
                observation.verification_state != next_observation_state
                or observation.public_spatial_mode != next_spatial_mode
            ):
                observation.verification_state = next_observation_state
                observation.public_spatial_mode = next_spatial_mode
                observation.version += 1
                observations_changed = True
    elif next_state == VerificationState.UNVERIFIED:
        for observation in observations:
            if observation.verification_state == VerificationState.CORROBORATED:
                observation.verification_state = VerificationState.PENDING_REVIEW
                observation.public_spatial_mode = EvidenceSpatialMode.WITHHELD
                observation.version += 1
                observations_changed = True

    episode_changed = observations_changed
    if episode.verification_state != next_state:
        episode.verification_state = next_state
        episode_changed = True
    if episode.corroborating_source_count != len(selected):
        episode.corroborating_source_count = len(selected)
        episode_changed = True
    if human_validated_at is not None:
        episode.validated_at = human_validated_at
        episode.evidence_basis_at = human_validated_at
        episode_changed = True
    if next_state in {VerificationState.CORROBORATED, VerificationState.VERIFIED}:
        if episode.evidence_basis_at is None:
            episode.evidence_basis_at = now
            episode_changed = True
        latest_observation = max((as_utc(item.observed_at) for item in selected), default=None)
        if latest_observation is not None and latest_observation > as_utc(episode.last_observed_at):
            episode.last_observed_at = latest_observation
            episode_changed = True
    elif previous_state == VerificationState.CORROBORATED and episode.evidence_basis_at is not None:
        episode.evidence_basis_at = None
        episode_changed = True

    if episode_changed:
        episode.version += 1

    next_visibility = canonical_public_visibility(episode.status, episode.verification_state)
    visibility_changed = incident.public_visibility != next_visibility
    if visibility_changed:
        incident.public_visibility = next_visibility
        incident.version += 1

    return EvidencePolicyResult(
        previous_state=previous_state,
        verification_state=next_state,
        independent_proof_count=len(selected),
        selected_observation_ids=tuple(item.observation_id for item in selected),
        episode_changed=episode_changed,
        incident_visibility_changed=visibility_changed,
    )
