from __future__ import annotations

from typing import Any

from shapely.geometry import shape
from sqlalchemy import select
from sqlalchemy.orm import Session

from fire_viewer.db.models import (
    ExternalArtifactRevision,
    ExternalClaim,
    IncidentCandidate,
    IncidentSeries,
)
from fire_viewer.domain.enums import (
    ActorType,
    ExternalArtifactStatus,
    ExternalSemanticRole,
)
from fire_viewer.domain.errors import BadRequestError, ConflictError, NotFoundError
from fire_viewer.domain.external_source_schemas import ExternalClaimInput
from fire_viewer.domain.hashing import json_safe, sha256_hex
from fire_viewer.services.common import record_audit
from fire_viewer.services.event_v2 import (
    create_private_incident_candidate_from_official_statement,
)

_SECRET_NAMES = frozenset(
    {
        "password",
        "secret",
        "token",
        "access_token",
        "refresh_token",
        "api_key",
        "apikey",
        "authorization",
        "credential",
        "credentials",
        "signature",
    }
)

_ASSERTIONS_BY_ROLE: dict[ExternalSemanticRole, frozenset[str]] = {
    ExternalSemanticRole.RAW_EARTH_OBSERVATION: frozenset(
        {"active_fire_point", "visible_front", "burned_area"}
    ),
    ExternalSemanticRole.SENSOR_DETECTION: frozenset({"thermal_hotspot"}),
    ExternalSemanticRole.INTERPRETED_OBSERVATION: frozenset(
        {"active_fire_point", "visible_front", "smoke_origin", "burned_area"}
    ),
    ExternalSemanticRole.OFFICIAL_INCIDENT_STATEMENT: frozenset(
        {"incident_declaration", "official_status"}
    ),
    ExternalSemanticRole.WEATHER_OBSERVATION: frozenset({"weather_observation"}),
    ExternalSemanticRole.WEATHER_FORECAST: frozenset({"weather_forecast"}),
    ExternalSemanticRole.GEOSPATIAL_REFERENCE: frozenset({"geospatial_reference"}),
    ExternalSemanticRole.HISTORICAL_REGISTRY: frozenset({"historical_record"}),
    ExternalSemanticRole.SIMULATION: frozenset({"simulation_output"}),
}


def _assert_no_secrets(value: object, *, path: str = "assertion_payload") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).strip().casefold().replace("-", "_")
            if normalized in _SECRET_NAMES or any(
                normalized.endswith(suffix)
                for suffix in ("_password", "_secret", "_token", "_credential", "_signature")
            ):
                raise BadRequestError(
                    "external_claim_secret_forbidden",
                    f"Secret-like metadata is forbidden at {path}.{key}.",
                )
            _assert_no_secrets(child, path=f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, child in enumerate(value):
            _assert_no_secrets(child, path=f"{path}[{index}]")


def register_external_claim(
    session: Session,
    *,
    payload: ExternalClaimInput,
    actor_id: str,
    trace_id: str,
) -> ExternalClaim:
    _assert_no_secrets(payload.assertion_payload)
    artifact = session.execute(
        select(ExternalArtifactRevision).where(
            ExternalArtifactRevision.artifact_revision_id == payload.artifact_revision_id
        )
    ).scalar_one_or_none()
    if artifact is None:
        raise NotFoundError("external_artifact_revision", payload.artifact_revision_id)
    if artifact.status == ExternalArtifactStatus.RETRACTED:
        raise ConflictError(
            "external_artifact_retracted",
            "Claims cannot be extracted from a retracted external artifact.",
        )
    if payload.assertion_kind not in _ASSERTIONS_BY_ROLE[artifact.semantic_role]:
        raise BadRequestError(
            "external_claim_semantic_mismatch",
            "The assertion kind is incompatible with the artifact semantic role.",
        )
    incident_db_id: int | None = None
    if payload.incident_id is not None:
        incident = session.execute(
            select(IncidentSeries).where(IncidentSeries.fire_id == payload.incident_id)
        ).scalar_one_or_none()
        if incident is None:
            raise NotFoundError("incident", payload.incident_id)
        incident_db_id = incident.id

    canonical_payload = json_safe(payload.assertion_payload)
    claim_digest = sha256_hex(
        {
            "artifact_revision_id": artifact.artifact_revision_id,
            "incident_id": payload.incident_id,
            "assertion_kind": payload.assertion_kind,
            "assertion_payload": canonical_payload,
            "geometry": payload.geometry_geojson,
            "horizontal_accuracy_m": payload.horizontal_accuracy_m,
            "confidence": payload.confidence,
        }
    )
    claim_id = f"ECL-{claim_digest[:60]}"
    existing = session.execute(
        select(ExternalClaim).where(ExternalClaim.claim_id == claim_id)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    stored_payload: dict[str, Any] = dict(canonical_payload)
    if payload.horizontal_accuracy_m is not None:
        stored_payload["horizontal_accuracy_m"] = payload.horizontal_accuracy_m
    family_key = artifact.evidence_family_key or f"content:{artifact.content_hash}"
    row = ExternalClaim(
        claim_id=claim_id,
        artifact_revision_id=artifact.id,
        incident_id=incident_db_id,
        assertion_kind=payload.assertion_kind,
        assertion_payload=stored_payload,
        geometry_geojson=json_safe(payload.geometry_geojson),
        confidence=payload.confidence,
        independent_family_key=family_key,
    )
    session.add(row)
    record_audit(
        session,
        actor_type=ActorType.SERVICE,
        actor_id=actor_id,
        action="external_claim.registered",
        target_type="external_claim",
        target_id=claim_id,
        reason="Structured assertion registered against one immutable external revision.",
        trace_id=trace_id,
        after={
            "artifact_revision_id": artifact.artifact_revision_id,
            "assertion_kind": payload.assertion_kind,
            "independent_family_key": family_key,
        },
    )
    session.commit()
    return row


def create_private_incident_candidate_from_external_claim(
    session: Session,
    *,
    claim_id: str,
    actor_id: str,
    trace_id: str,
) -> IncidentCandidate:
    claim = session.execute(
        select(ExternalClaim).where(ExternalClaim.claim_id == claim_id)
    ).scalar_one_or_none()
    if claim is None:
        raise NotFoundError("external_claim", claim_id)
    artifact = session.get(ExternalArtifactRevision, claim.artifact_revision_id)
    if artifact is None:
        raise RuntimeError("External claim artifact invariant is broken")
    if (
        artifact.semantic_role != ExternalSemanticRole.OFFICIAL_INCIDENT_STATEMENT
        or claim.assertion_kind != "incident_declaration"
    ):
        raise BadRequestError(
            "official_incident_declaration_required",
            "Only an official incident declaration may seed a private incident candidate.",
        )
    if claim.incident_id is not None:
        raise ConflictError(
            "official_claim_already_attached",
            "A claim already attached to an incident cannot seed another dossier.",
        )

    longitude: float | None = None
    latitude: float | None = None
    accuracy: float | None = None
    if claim.geometry_geojson is not None:
        representative = shape(claim.geometry_geojson).representative_point()
        longitude = float(representative.x)
        latitude = float(representative.y)
        stored_accuracy = claim.assertion_payload.get("horizontal_accuracy_m")
        if not isinstance(stored_accuracy, int | float) or isinstance(stored_accuracy, bool):
            raise ConflictError(
                "official_claim_accuracy_missing",
                "A spatial official declaration requires an explicit horizontal accuracy.",
            )
        accuracy = float(stored_accuracy)

    candidate = create_private_incident_candidate_from_official_statement(
        session,
        artifact_revision_id=artifact.id,
        actor_id=actor_id,
        longitude=longitude,
        latitude=latitude,
        accuracy_m=accuracy,
        trace_id=trace_id,
    )
    session.commit()
    return candidate
