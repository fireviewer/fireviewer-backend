"""Private overlays on the current 3D scene; never generates or replaces its GLB asset."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from shapely.geometry import MultiPolygon, Polygon, mapping, shape
from shapely.ops import unary_union
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload
from sqlalchemy.sql.elements import ColumnElement

from fire_viewer.core.ids import new_prefixed_id
from fire_viewer.core.security import Actor
from fire_viewer.core.time import as_utc, ensure_utc, utcnow
from fire_viewer.db.models import (
    ActiveFireZoneRevision,
    AgentAnalysisWindow,
    AgentFactProposal,
    AgentMediaBatch,
    AgentMediaItem,
    AgentReviewTask,
    AgentSituationReportFact,
    AgentSituationReportRevision,
    AgentSpatialProposal,
    Episode,
    IncidentMapCapture,
    IncidentSeries,
    IncidentSpatialMarker,
    ManifestRevision,
    ModelAsset,
    Observation,
    SpatialPackage,
    SpatialZone,
    SpatialZoneRevision,
    ZonePublication,
)
from fire_viewer.domain.enums import (
    ActiveFireZoneReviewState,
    AgentProposalReviewState,
    AgentReportReviewState,
    AgentReviewState,
    EvidenceSpatialMode,
    IncidentMarkerReviewState,
    VerificationState,
)
from fire_viewer.domain.errors import BadRequestError, ConflictError, NotFoundError
from fire_viewer.domain.hashing import json_safe
from fire_viewer.domain.incident_spatial_schemas import (
    ActiveFireZoneMergeRequest,
    ActiveFireZoneReviewRequest,
    ActiveFireZoneRevisionCreateRequest,
    AdminActiveFireZoneRevision,
    AdminAgentEvidenceReference,
    AdminAgentFactProposal,
    AdminAgentReviewPackage,
    AdminAgentSituationReport,
    AdminDailyIntelligenceReview,
    AdminIncidentScene,
    AdminIncidentSpatialMarker,
    AdminIncidentSpatialReviewWorkspace,
    AgentFactReviewRequest,
    AgentReviewResolutionRequest,
    AgentSituationReportReviewRequest,
    IncidentGltfPickRequest,
    IncidentGltfPickResponse,
    IncidentMarkerReviewRequest,
)
from fire_viewer.domain.spatial import (
    enu_to_gltf,
    enu_to_wgs84,
    gltf_to_enu,
    wgs84_to_enu,
)
from fire_viewer.services.agent_validation_campaigns import (
    refresh_campaign_day_publication_state,
)
from fire_viewer.services.common import record_operator_audit
from fire_viewer.services.incident_gallery import map_capture_response


def _reviewable_agent_point() -> ColumnElement[bool]:
    """Reuse the existing marker review for legacy and native V2 point proposals."""

    return or_(
        AgentSpatialProposal.status == "ground_point",
        and_(
            AgentSpatialProposal.status == "projected_geometry",
            AgentSpatialProposal.proposal_kind.in_(["active_fire_point", "smoke_origin_point"]),
        ),
    )


def _incident_and_episode(session: Session, fire_id: str) -> tuple[IncidentSeries, Episode]:
    incident = session.execute(
        select(IncidentSeries)
        .where(IncidentSeries.fire_id == fire_id)
        .options(selectinload(IncidentSeries.episodes))
    ).scalar_one_or_none()
    if incident is None:
        raise NotFoundError("incident", fire_id)
    episode = next((item for item in incident.episodes if item.is_current), None)
    if episode is None:
        raise ConflictError("incident_without_current_episode", "Incident has no current episode.")
    return incident, episode


def _scene(
    session: Session, incident: IncidentSeries, episode: Episode
) -> tuple[AdminIncidentScene | None, tuple[float, float, float] | None]:
    manifest = session.execute(
        select(ManifestRevision)
        .where(
            ManifestRevision.incident_id == incident.id,
            ManifestRevision.episode_id == episode.id,
            ManifestRevision.is_current.is_(True),
        )
        .options(
            selectinload(ManifestRevision.asset).selectinload(ModelAsset.spatial_zone_revision),
            selectinload(ManifestRevision.package).selectinload(SpatialPackage.files),
            selectinload(ManifestRevision.spatial_zone_revision),
        )
    ).scalar_one_or_none()
    if manifest is None or (manifest.asset is None and manifest.package is None):
        return None, None
    spatial_revision: SpatialZoneRevision | None = manifest.spatial_zone_revision or (
        manifest.asset.spatial_zone_revision if manifest.asset is not None else None
    )
    if spatial_revision is None:
        return None, None
    zone_id = session.execute(
        select(SpatialZone.zone_id).where(SpatialZone.id == spatial_revision.spatial_zone_id)
    ).scalar_one()
    publication = (
        session.execute(
            select(ZonePublication)
            .where(
                ZonePublication.spatial_package_id == manifest.package.id,
                ZonePublication.spatial_zone_revision_id == spatial_revision.id,
            )
            .order_by(ZonePublication.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if manifest.package is not None
        else None
    )
    origin = (
        spatial_revision.origin_lon,
        spatial_revision.origin_lat,
        spatial_revision.origin_ellipsoid_height_m,
    )
    return (
        AdminIncidentScene(
            asset_url=manifest.asset.glb_url if manifest.asset is not None else None,
            asset_version=manifest.asset.version if manifest.asset is not None else None,
            sha256=manifest.asset.sha256 if manifest.asset is not None else None,
            package_id=manifest.package.package_id if manifest.package is not None else None,
            zone_id=zone_id,
            zone_revision=spatial_revision.revision,
            package_state=manifest.package.state.value if manifest.package is not None else None,
            publication_id=publication.publication_id if publication is not None else None,
            publication_state=publication.state.value if publication is not None else None,
            publication_active=publication.is_active if publication is not None else False,
            catalog_url=(
                f"/api/v1/admin/zones/{zone_id}/revisions/{spatial_revision.revision}/preview/"
                f"packages/{manifest.package.package_id}/catalog"
                if manifest.package is not None
                else None
            ),
            files=(
                {
                    str(item.provenance.get("catalog_path")): (
                        f"/api/v2/admin/packages/{manifest.package.package_id}/files/{item.id}"
                    )
                    for item in manifest.package.files
                    if item.provenance.get("catalog_path")
                }
                if manifest.package is not None
                else {}
            ),
            origin_wgs84=origin,
        ),
        origin,
    )


def _gltf_position(
    longitude: float,
    latitude: float,
    altitude_m: float | None,
    origin: tuple[float, float, float] | None,
) -> tuple[float, float, float] | None:
    if origin is None:
        return None
    return enu_to_gltf(wgs84_to_enu((longitude, latitude, altitude_m or origin[2]), origin))


def _project_geometry(
    geometry: dict[str, Any], origin: tuple[float, float, float] | None
) -> list[list[list[tuple[float, float, float]]]]:
    if origin is None:
        return []
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list):
        return []
    projected: list[list[list[tuple[float, float, float]]]] = []

    def project(point: list[float]) -> tuple[float, float, float]:
        value = _gltf_position(float(point[0]), float(point[1]), None, origin)
        assert value is not None
        return value

    for polygon in coordinates:
        projected_polygon: list[list[tuple[float, float, float]]] = []
        for ring in polygon:
            projected_polygon.append([project(point) for point in ring])
        projected.append(projected_polygon)
    return projected


def _normalize_geometry(payload: dict[str, Any]) -> tuple[dict[str, Any], MultiPolygon]:
    try:
        geometry = shape(payload)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise BadRequestError(
            "active_zone_geometry_invalid", "GeoJSON geometry is invalid."
        ) from exc
    if isinstance(geometry, Polygon):
        geometry = MultiPolygon([geometry])
    if not isinstance(geometry, MultiPolygon) or geometry.is_empty or geometry.area <= 0:
        raise BadRequestError(
            "active_zone_geometry_invalid", "Geometry must be a non-empty Polygon or MultiPolygon."
        )
    if not geometry.is_valid:
        raise BadRequestError(
            "active_zone_geometry_invalid",
            "Geometry is topologically invalid; repair it explicitly in the editor.",
        )
    min_lon, min_lat, max_lon, max_lat = geometry.bounds
    if min_lon < -180 or max_lon > 180 or min_lat < -90 or max_lat > 90:
        raise BadRequestError(
            "active_zone_geometry_out_of_bounds", "Geometry coordinates are outside WGS84 bounds."
        )
    normalized = json_safe(mapping(geometry))
    assert isinstance(normalized, dict)
    return normalized, geometry


def _observation_review_state(state: VerificationState) -> IncidentMarkerReviewState:
    if state in {VerificationState.CORROBORATED, VerificationState.VERIFIED}:
        return IncidentMarkerReviewState.VALIDATED
    if state == VerificationState.REJECTED:
        return IncidentMarkerReviewState.REJECTED
    return IncidentMarkerReviewState.PENDING


def _markers(
    session: Session,
    incident: IncidentSeries,
    episode: Episode,
    origin: tuple[float, float, float] | None,
) -> list[AdminIncidentSpatialMarker]:
    observations = session.execute(
        select(Observation)
        .where(
            or_(
                Observation.attached_incident_id == incident.id,
                Observation.proposed_incident_id == incident.id,
            )
        )
        .order_by(Observation.observed_at.asc(), Observation.observation_id.asc())
        .limit(1_000)
    ).scalars()
    persisted = session.execute(
        select(IncidentSpatialMarker)
        .where(
            IncidentSpatialMarker.incident_id == incident.id,
            IncidentSpatialMarker.episode_id == episode.id,
        )
        .order_by(IncidentSpatialMarker.observed_at.asc(), IncidentSpatialMarker.marker_id.asc())
        .limit(1_000)
    ).scalars()
    agent_proposals = session.execute(
        select(AgentSpatialProposal)
        .join(
            AgentAnalysisWindow,
            AgentAnalysisWindow.id == AgentSpatialProposal.analysis_window_id,
        )
        .where(
            AgentAnalysisWindow.incident_id == incident.id,
            AgentAnalysisWindow.episode_id == episode.id,
            _reviewable_agent_point(),
        )
        .order_by(AgentSpatialProposal.observed_at.asc(), AgentSpatialProposal.proposal_id.asc())
        .limit(1_000)
    ).scalars()
    result = [
        AdminIncidentSpatialMarker(
            marker_id=f"observation:{item.observation_id}",
            source_kind="observation",
            marker_type="reported_location",
            longitude=item.longitude,
            latitude=item.latitude,
            altitude_m=item.altitude_m,
            horizontal_accuracy_m=item.horizontal_uncertainty_m,
            geometry_origin="HUMAN_CONFIRMED"
            if item.verification_state == VerificationState.VERIFIED
            else "USER_DECLARED",
            review_state=_observation_review_state(item.verification_state).value,
            observed_at=as_utc(item.observed_at),
            spatial_display_allowed=item.public_spatial_mode == EvidenceSpatialMode.EXACT,
            gltf_position=_gltf_position(item.longitude, item.latitude, item.altitude_m, origin),
            version=item.version,
        )
        for item in observations
    ]
    result.extend(
        AdminIncidentSpatialMarker(
            marker_id=item.marker_id,
            source_kind="agent_media",
            marker_type=item.marker_type,
            longitude=item.longitude,
            latitude=item.latitude,
            altitude_m=item.altitude_m,
            horizontal_accuracy_m=item.horizontal_accuracy_m,
            geometry_origin=item.geometry_origin,
            review_state=item.review_state.value,
            observed_at=as_utc(item.observed_at) if item.observed_at else None,
            spatial_display_allowed=item.spatial_display_allowed,
            gltf_position=_gltf_position(item.longitude, item.latitude, item.altitude_m, origin),
            version=item.version,
        )
        for item in persisted
    )
    result.extend(
        AdminIncidentSpatialMarker(
            marker_id=f"proposal:{item.proposal_id}",
            source_kind="agent_media",
            marker_type=item.proposal_kind or "active_fire_point",
            longitude=float(item.longitude),
            latitude=float(item.latitude),
            altitude_m=item.altitude_m,
            horizontal_accuracy_m=item.horizontal_accuracy_m,
            geometry_origin=item.geometry_origin or "EXPLICIT_SOURCE_GEOMETRY",
            review_state=(
                IncidentMarkerReviewState.VALIDATED.value
                if item.review_state == AgentProposalReviewState.VALIDATED
                else IncidentMarkerReviewState.REJECTED.value
                if item.review_state
                in {AgentProposalReviewState.REJECTED, AgentProposalReviewState.INVALIDATED}
                else IncidentMarkerReviewState.PENDING.value
            ),
            observed_at=as_utc(item.observed_at) if item.observed_at else None,
            spatial_display_allowed=False,
            gltf_position=_gltf_position(
                float(item.longitude), float(item.latitude), item.altitude_m, origin
            ),
            version=item.version,
        )
        for item in agent_proposals
        if item.longitude is not None and item.latitude is not None
    )
    return result


def _zone_response(
    item: ActiveFireZoneRevision,
    *,
    origin: tuple[float, float, float] | None,
    revisions_by_id: dict[int, ActiveFireZoneRevision],
) -> AdminActiveFireZoneRevision:
    supersedes = revisions_by_id.get(item.supersedes_revision_id or -1)
    return AdminActiveFireZoneRevision(
        zone_revision_id=item.zone_revision_id,
        revision=item.revision,
        valid_at=as_utc(item.valid_at),
        geometry_geojson=item.geometry_geojson,
        gltf_polygons=_project_geometry(item.geometry_geojson, origin),
        geometry_origin=item.geometry_origin,
        analysis_id=item.analysis_window.analysis_id if item.analysis_window is not None else None,
        supporting_marker_ids=list(item.supporting_marker_ids),
        source_revision_ids=list(item.source_revision_ids),
        review_state=item.review_state,
        supersedes_zone_revision_id=supersedes.zone_revision_id if supersedes else None,
        reason=item.reason,
        created_by=item.created_by,
        reviewed_by=item.reviewed_by,
        reviewed_at=as_utc(item.reviewed_at) if item.reviewed_at else None,
        review_reason=item.review_reason,
        created_at=as_utc(item.created_at),
    )


def _fact_response(item: AgentFactProposal) -> AdminAgentFactProposal:
    source = item.source_media_item
    return AdminAgentFactProposal(
        fact_id=item.fact_id,
        category=item.category,
        fact_key=item.fact_key,
        as_of=as_utc(item.as_of),
        certainty=item.certainty,
        summary=item.summary,
        value_number=item.value_number,
        value_text=item.value_text,
        value_boolean=item.value_boolean,
        unit=item.unit,
        conflict_group_id=item.conflict_group_id,
        review_state=item.review_state,
        version=item.version,
        source=AdminAgentEvidenceReference(
            batch_id=source.batch.batch_id,
            input_id=source.input_id,
            media_type=source.media_type.value,
            media_sha256=source.media_sha256,
            evidence_kind=item.evidence_kind,
            evidence_id=item.evidence_id,
        ),
    )


def _report_response(item: AgentSituationReportRevision) -> AdminAgentSituationReport:
    return AdminAgentSituationReport(
        report_revision_id=item.report_revision_id,
        revision=item.revision,
        title=item.title,
        body_markdown=item.body_markdown,
        review_state=item.review_state,
        reviewed_by=item.reviewed_by,
        reviewed_at=as_utc(item.reviewed_at) if item.reviewed_at else None,
        review_reason=item.review_reason,
        created_at=as_utc(item.created_at),
    )


def _daily_intelligence(
    session: Session,
    incident: IncidentSeries,
    episode: Episode,
) -> list[AdminDailyIntelligenceReview]:
    windows = list(
        session.scalars(
            select(AgentAnalysisWindow)
            .where(
                AgentAnalysisWindow.incident_id == incident.id,
                AgentAnalysisWindow.episode_id == episode.id,
            )
            .order_by(
                AgentAnalysisWindow.local_date.desc(),
                AgentAnalysisWindow.id.desc(),
            )
            .limit(500)
        )
    )
    if not windows:
        return []
    reports = list(
        session.scalars(
            select(AgentSituationReportRevision)
            .where(
                AgentSituationReportRevision.analysis_window_id.in_(
                    [window.id for window in windows]
                )
            )
            .options(
                selectinload(AgentSituationReportRevision.fact_links)
                .selectinload(AgentSituationReportFact.fact)
                .selectinload(AgentFactProposal.source_media_item)
                .selectinload(AgentMediaItem.batch)
            )
            .order_by(
                AgentSituationReportRevision.analysis_window_id.asc(),
                AgentSituationReportRevision.revision.desc(),
            )
        )
    )
    latest_by_window: dict[int, AgentSituationReportRevision] = {}
    for loaded_report in reports:
        latest_by_window.setdefault(loaded_report.analysis_window_id, loaded_report)
    result: list[AdminDailyIntelligenceReview] = []
    for window in windows:
        latest_report = latest_by_window.get(window.id)
        metadata = next(
            (
                section
                for section in (
                    latest_report.sections_payload if latest_report is not None else []
                )
                if isinstance(section, dict) and section.get("key") == "_daily_consolidation"
            ),
            {},
        )
        facts = (
            sorted(
                (link.fact for link in latest_report.fact_links),
                key=lambda item: (item.as_of, item.fact_id),
            )
            if latest_report is not None
            else []
        )
        result.append(
            AdminDailyIntelligenceReview(
                analysis_id=window.analysis_id,
                local_date=window.local_date,
                window_state=window.state.value,
                report=_report_response(latest_report) if latest_report is not None else None,
                facts=[_fact_response(fact) for fact in facts],
                operation_outcomes=dict(metadata.get("operation_outcomes") or {}),
                spatial_counts={
                    str(key): int(value)
                    for key, value in dict(metadata.get("spatial_counts") or {}).items()
                },
                contradictions=[
                    item
                    for item in list(metadata.get("contradictions") or [])
                    if isinstance(item, dict)
                ],
            )
        )
    return result


def get_spatial_review_workspace(
    session: Session, *, fire_id: str
) -> AdminIncidentSpatialReviewWorkspace:
    incident, episode = _incident_and_episode(session, fire_id)
    scene, origin = _scene(session, incident, episode)
    revisions = list(
        session.execute(
            select(ActiveFireZoneRevision)
            .where(
                ActiveFireZoneRevision.incident_id == incident.id,
                ActiveFireZoneRevision.episode_id == episode.id,
            )
            .order_by(ActiveFireZoneRevision.revision.asc())
            .limit(500)
        ).scalars()
    )
    batches = list(
        session.execute(
            select(AgentMediaBatch)
            .where(
                AgentMediaBatch.incident_id == incident.id,
                AgentMediaBatch.episode_id == episode.id,
            )
            .options(
                selectinload(AgentMediaBatch.review_task),
                selectinload(AgentMediaBatch.dispatch),
            )
            .order_by(AgentMediaBatch.created_at.desc())
            .limit(200)
        ).scalars()
    )
    map_gallery = list(
        session.scalars(
            select(IncidentMapCapture)
            .where(
                IncidentMapCapture.incident_id == incident.id,
                IncidentMapCapture.episode_id == episode.id,
            )
            .options(selectinload(IncidentMapCapture.active_zone_revision))
            .order_by(
                IncidentMapCapture.local_date.desc(),
                IncidentMapCapture.captured_at.desc(),
            )
            .limit(1_000)
        )
    )
    return AdminIncidentSpatialReviewWorkspace(
        fire_id=incident.fire_id,
        episode_id=episode.episode_id,
        scene=scene,
        markers=_markers(session, incident, episode, origin),
        zone_revisions=[
            _zone_response(item, origin=origin, revisions_by_id={row.id: row for row in revisions})
            for item in revisions
        ],
        map_gallery=[map_capture_response(item, fire_id) for item in map_gallery],
        agent_reviews=[
            AdminAgentReviewPackage(
                review_id=batch.review_task.review_id,
                batch_id=batch.batch_id,
                state=batch.review_task.state.value,
                reason_codes=list(batch.review_task.reason_codes),
                completed_at=as_utc(batch.completed_at) if batch.completed_at else None,
                result=batch.dispatch.raw_output if batch.dispatch else None,
            )
            for batch in batches
            if batch.review_task is not None
        ],
        daily_intelligence=_daily_intelligence(session, incident, episode),
    )


def project_gltf_pick(
    session: Session, *, fire_id: str, payload: IncidentGltfPickRequest
) -> IncidentGltfPickResponse:
    incident, episode = _incident_and_episode(session, fire_id)
    _, origin = _scene(session, incident, episode)
    if origin is None:
        raise ConflictError(
            "incident_spatial_scene_unavailable",
            "The incident has no current georeferenced 3D scene.",
        )
    longitude, latitude, altitude_m = enu_to_wgs84(gltf_to_enu(payload.gltf_position), origin)
    return IncidentGltfPickResponse(
        longitude=longitude,
        latitude=latitude,
        altitude_m=altitude_m,
    )


def review_marker(
    session: Session,
    *,
    fire_id: str,
    marker_id: str,
    payload: IncidentMarkerReviewRequest,
    actor: Actor,
    trace_id: str,
) -> AdminIncidentSpatialMarker:
    incident, episode = _incident_and_episode(session, fire_id)
    marker = session.execute(
        select(IncidentSpatialMarker).where(
            IncidentSpatialMarker.marker_id == marker_id,
            IncidentSpatialMarker.incident_id == incident.id,
            IncidentSpatialMarker.episode_id == episode.id,
        )
    ).scalar_one_or_none()
    if marker is None and marker_id.startswith("proposal:"):
        proposal_id = marker_id.removeprefix("proposal:")
        proposal = session.execute(
            select(AgentSpatialProposal)
            .join(
                AgentAnalysisWindow,
                AgentAnalysisWindow.id == AgentSpatialProposal.analysis_window_id,
            )
            .where(
                AgentSpatialProposal.proposal_id == proposal_id,
                AgentAnalysisWindow.incident_id == incident.id,
                AgentAnalysisWindow.episode_id == episode.id,
                _reviewable_agent_point(),
            )
        ).scalar_one_or_none()
        if proposal is None:
            raise NotFoundError("agent_spatial_proposal", proposal_id)
        if proposal.version != payload.expected_version:
            raise ConflictError("marker_version_conflict", "Marker changed since it was loaded.")
        proposal.review_state = (
            AgentProposalReviewState.VALIDATED
            if payload.action == "validate"
            else AgentProposalReviewState.REJECTED
        )
        proposal.reviewed_by = actor.actor_id
        proposal.reviewed_at = utcnow()
        proposal.review_reason = payload.reason
        proposal.version += 1
        record_operator_audit(
            session,
            actor=actor,
            action=f"incident.agent_spatial_proposal_{payload.action}",
            target_type="agent_spatial_proposal",
            target_id=proposal.proposal_id,
            reason=payload.reason,
            trace_id=trace_id,
            after={"review_state": proposal.review_state.value, "version": proposal.version},
        )
        session.commit()
        _, origin = _scene(session, incident, episode)
        return next(
            item
            for item in _markers(session, incident, episode, origin)
            if item.marker_id == marker_id
        )
    if marker is None:
        raise NotFoundError("incident_spatial_marker", marker_id)
    if marker.version != payload.expected_version:
        raise ConflictError("marker_version_conflict", "Marker changed since it was loaded.")
    marker.review_state = (
        IncidentMarkerReviewState.VALIDATED
        if payload.action == "validate"
        else IncidentMarkerReviewState.REJECTED
    )
    marker.reviewed_by = actor.actor_id
    marker.reviewed_at = utcnow()
    marker.review_reason = payload.reason
    marker.version += 1
    record_operator_audit(
        session,
        actor=actor,
        action=f"incident.marker_{payload.action}",
        target_type="incident_spatial_marker",
        target_id=marker.marker_id,
        reason=payload.reason,
        trace_id=trace_id,
        after={"review_state": marker.review_state.value, "version": marker.version},
    )
    session.commit()
    _, origin = _scene(session, incident, episode)
    return next(
        item
        for item in _markers(session, incident, episode, origin)
        if item.marker_id == marker.marker_id
    )


def _latest_revision(session: Session, incident: IncidentSeries, episode: Episode) -> int:
    return int(
        session.scalar(
            select(func.max(ActiveFireZoneRevision.revision)).where(
                ActiveFireZoneRevision.incident_id == incident.id,
                ActiveFireZoneRevision.episode_id == episode.id,
            )
        )
        or 0
    )


def _validate_supporting_markers(
    session: Session,
    incident: IncidentSeries,
    episode: Episode,
    marker_ids: Iterable[str],
    *,
    require_validated: bool,
) -> None:
    requested = set(marker_ids)
    if not requested:
        return
    observations = {
        f"observation:{observation_id}": state
        for observation_id, state in session.execute(
            select(Observation.observation_id, Observation.verification_state).where(
                or_(
                    and_(
                        Observation.attached_incident_id == incident.id,
                        Observation.attached_episode_id == episode.id,
                    ),
                    and_(
                        Observation.proposed_incident_id == incident.id,
                        Observation.proposed_episode_id == episode.id,
                    ),
                )
            )
        )
    }
    persisted = {
        marker_id: state
        for marker_id, state in session.execute(
            select(IncidentSpatialMarker.marker_id, IncidentSpatialMarker.review_state).where(
                IncidentSpatialMarker.incident_id == incident.id,
                IncidentSpatialMarker.episode_id == episode.id,
            )
        )
    }
    proposals = {
        f"proposal:{proposal_id}": state
        for proposal_id, state in session.execute(
            select(AgentSpatialProposal.proposal_id, AgentSpatialProposal.review_state)
            .join(
                AgentAnalysisWindow,
                AgentAnalysisWindow.id == AgentSpatialProposal.analysis_window_id,
            )
            .where(
                AgentAnalysisWindow.incident_id == incident.id,
                AgentAnalysisWindow.episode_id == episode.id,
                _reviewable_agent_point(),
            )
        )
    }
    known = set(observations) | set(persisted) | set(proposals)
    if unknown := requested - known:
        raise BadRequestError(
            "active_zone_unknown_marker", f"Unknown supporting marker: {sorted(unknown)[0]}"
        )
    if require_validated:
        invalid = {
            marker_id
            for marker_id in requested
            if (
                marker_id in observations
                and observations[marker_id]
                not in {VerificationState.CORROBORATED, VerificationState.VERIFIED}
            )
            or (
                marker_id in persisted
                and persisted[marker_id] != IncidentMarkerReviewState.VALIDATED
            )
            or (
                marker_id in proposals
                and proposals[marker_id] != AgentProposalReviewState.VALIDATED
            )
        }
        if invalid:
            raise ConflictError(
                "active_zone_unvalidated_marker",
                f"Supporting marker is not validated: {sorted(invalid)[0]}",
            )


def _create_revision(
    session: Session,
    *,
    incident: IncidentSeries,
    episode: Episode,
    expected_latest_revision: int,
    valid_at: datetime,
    analysis_window: AgentAnalysisWindow | None,
    geometry_geojson: dict[str, Any],
    geometry_origin: str,
    supporting_marker_ids: list[str],
    source_revision_ids: list[str],
    reason: str,
    actor: Actor,
    trace_id: str,
) -> ActiveFireZoneRevision:
    latest = _latest_revision(session, incident, episode)
    if latest != expected_latest_revision:
        raise ConflictError(
            "active_zone_revision_conflict", "Active zone changed since the editor was loaded."
        )
    normalized, _ = _normalize_geometry(geometry_geojson)
    _validate_supporting_markers(
        session, incident, episode, supporting_marker_ids, require_validated=False
    )
    try:
        normalized_valid_at = (
            as_utc(analysis_window.window_end_at)
            if analysis_window is not None
            else ensure_utc(valid_at)
        )
    except ValueError as exc:
        raise BadRequestError("active_zone_datetime_timezone_required", str(exc)) from exc
    supersedes = session.execute(
        select(ActiveFireZoneRevision).where(
            ActiveFireZoneRevision.incident_id == incident.id,
            ActiveFireZoneRevision.episode_id == episode.id,
            ActiveFireZoneRevision.revision == latest,
        )
    ).scalar_one_or_none()
    revision = ActiveFireZoneRevision(
        zone_revision_id=new_prefixed_id("azr"),
        incident_id=incident.id,
        episode_id=episode.id,
        analysis_window_id=analysis_window.id if analysis_window is not None else None,
        revision=latest + 1,
        valid_at=normalized_valid_at,
        geometry_geojson=normalized,
        geometry_origin=geometry_origin,
        supporting_marker_ids=sorted(set(supporting_marker_ids)),
        source_revision_ids=source_revision_ids,
        review_state=ActiveFireZoneReviewState.DRAFT,
        supersedes_revision_id=supersedes.id if supersedes else None,
        created_by=actor.actor_id,
        reason=reason,
    )
    session.add(revision)
    session.flush()
    record_operator_audit(
        session,
        actor=actor,
        action="incident.active_zone_revision_created",
        target_type="active_fire_zone_revision",
        target_id=revision.zone_revision_id,
        reason=reason,
        trace_id=trace_id,
        after={
            "revision": revision.revision,
            "review_state": revision.review_state.value,
            "analysis_id": analysis_window.analysis_id if analysis_window is not None else None,
        },
    )
    session.commit()
    return revision


def create_zone_revision(
    session: Session,
    *,
    fire_id: str,
    payload: ActiveFireZoneRevisionCreateRequest,
    actor: Actor,
    trace_id: str,
) -> AdminActiveFireZoneRevision:
    incident, episode = _incident_and_episode(session, fire_id)
    analysis_window = None
    if payload.analysis_id is not None:
        analysis_window = session.execute(
            select(AgentAnalysisWindow).where(
                AgentAnalysisWindow.analysis_id == payload.analysis_id,
                AgentAnalysisWindow.incident_id == incident.id,
                AgentAnalysisWindow.episode_id == episode.id,
            )
        ).scalar_one_or_none()
        if analysis_window is None:
            raise BadRequestError(
                "active_zone_analysis_window_unknown",
                "The selected analysis day does not belong to this incident episode.",
            )
    revision = _create_revision(
        session,
        incident=incident,
        episode=episode,
        expected_latest_revision=payload.expected_latest_revision,
        valid_at=payload.valid_at,
        analysis_window=analysis_window,
        geometry_geojson=payload.geometry_geojson,
        geometry_origin=payload.geometry_origin,
        supporting_marker_ids=payload.supporting_marker_ids,
        source_revision_ids=[],
        reason=payload.reason,
        actor=actor,
        trace_id=trace_id,
    )
    _, origin = _scene(session, incident, episode)
    return _zone_response(revision, origin=origin, revisions_by_id={revision.id: revision})


def merge_zone_revisions(
    session: Session,
    *,
    fire_id: str,
    payload: ActiveFireZoneMergeRequest,
    actor: Actor,
    trace_id: str,
) -> AdminActiveFireZoneRevision:
    incident, episode = _incident_and_episode(session, fire_id)
    requested_ids = list(dict.fromkeys(payload.source_revision_ids))
    if len(requested_ids) < 2:
        raise BadRequestError("active_zone_merge_sources", "Two distinct revisions are required.")
    sources = list(
        session.execute(
            select(ActiveFireZoneRevision).where(
                ActiveFireZoneRevision.zone_revision_id.in_(requested_ids),
                ActiveFireZoneRevision.incident_id == incident.id,
                ActiveFireZoneRevision.episode_id == episode.id,
                ActiveFireZoneRevision.review_state != ActiveFireZoneReviewState.REJECTED,
            )
        ).scalars()
    )
    if len(sources) != len(requested_ids):
        raise BadRequestError(
            "active_zone_merge_sources", "Every source revision must exist and be non-rejected."
        )
    geometries = [_normalize_geometry(item.geometry_geojson)[1] for item in sources]
    merged = unary_union(geometries)
    if isinstance(merged, Polygon):
        merged = MultiPolygon([merged])
    normalized = json_safe(mapping(merged))
    assert isinstance(normalized, dict)
    analysis_window_ids = {item.analysis_window_id for item in sources}
    analysis_window = None
    if len(analysis_window_ids) == 1:
        analysis_window_id = next(iter(analysis_window_ids))
        if analysis_window_id is not None:
            analysis_window = session.get(AgentAnalysisWindow, analysis_window_id)
    revision = _create_revision(
        session,
        incident=incident,
        episode=episode,
        expected_latest_revision=payload.expected_latest_revision,
        valid_at=payload.valid_at,
        analysis_window=analysis_window,
        geometry_geojson=normalized,
        geometry_origin="DETERMINISTIC_UNION",
        supporting_marker_ids=payload.supporting_marker_ids,
        source_revision_ids=requested_ids,
        reason=payload.reason,
        actor=actor,
        trace_id=trace_id,
    )
    _, origin = _scene(session, incident, episode)
    return _zone_response(revision, origin=origin, revisions_by_id={revision.id: revision})


def review_zone_revision(
    session: Session,
    *,
    fire_id: str,
    zone_revision_id: str,
    payload: ActiveFireZoneReviewRequest,
    actor: Actor,
    trace_id: str,
) -> AdminActiveFireZoneRevision:
    incident, episode = _incident_and_episode(session, fire_id)
    revision = session.execute(
        select(ActiveFireZoneRevision).where(
            ActiveFireZoneRevision.zone_revision_id == zone_revision_id,
            ActiveFireZoneRevision.incident_id == incident.id,
            ActiveFireZoneRevision.episode_id == episode.id,
        )
    ).scalar_one_or_none()
    if revision is None:
        raise NotFoundError("active_fire_zone_revision", zone_revision_id)
    if revision.review_state.value != payload.expected_state:
        raise ConflictError(
            "active_zone_review_conflict", "Zone revision state changed before the review."
        )
    if payload.action == "approve" and revision.review_state != ActiveFireZoneReviewState.DRAFT:
        raise ConflictError(
            "active_zone_review_conflict", "Only a draft zone revision can be approved."
        )
    if payload.action == "approve":
        _validate_supporting_markers(
            session, incident, episode, revision.supporting_marker_ids, require_validated=True
        )
        revision.review_state = ActiveFireZoneReviewState.READY_FOR_PUBLICATION
    else:
        revision.review_state = ActiveFireZoneReviewState.REJECTED
    revision.reviewed_by = actor.actor_id
    revision.reviewed_at = utcnow()
    revision.review_reason = payload.reason
    record_operator_audit(
        session,
        actor=actor,
        action=f"incident.active_zone_{payload.action}",
        target_type="active_fire_zone_revision",
        target_id=revision.zone_revision_id,
        reason=payload.reason,
        trace_id=trace_id,
        after={"review_state": revision.review_state.value},
    )
    if revision.analysis_window_id is not None:
        refresh_campaign_day_publication_state(
            session,
            analysis_window_id=revision.analysis_window_id,
        )
    session.commit()
    _, origin = _scene(session, incident, episode)
    return _zone_response(revision, origin=origin, revisions_by_id={revision.id: revision})


def review_fact_proposal(
    session: Session,
    *,
    fire_id: str,
    fact_id: str,
    payload: AgentFactReviewRequest,
    actor: Actor,
    trace_id: str,
) -> AdminAgentFactProposal:
    incident, episode = _incident_and_episode(session, fire_id)
    fact = session.scalar(
        select(AgentFactProposal)
        .join(
            AgentAnalysisWindow,
            AgentAnalysisWindow.id == AgentFactProposal.analysis_window_id,
        )
        .where(
            AgentFactProposal.fact_id == fact_id,
            AgentAnalysisWindow.incident_id == incident.id,
            AgentAnalysisWindow.episode_id == episode.id,
        )
        .options(
            selectinload(AgentFactProposal.source_media_item).selectinload(AgentMediaItem.batch)
        )
    )
    if fact is None:
        raise NotFoundError("agent_fact_proposal", fact_id)
    if fact.version != payload.expected_version:
        raise ConflictError(
            "agent_fact_version_conflict",
            "The sourced fact changed since it was loaded.",
        )
    if fact.review_state != AgentProposalReviewState.PENDING:
        raise ConflictError(
            "agent_fact_review_state_conflict",
            "The sourced fact has already been reviewed.",
        )
    fact.review_state = (
        AgentProposalReviewState.VALIDATED
        if payload.action == "validate"
        else AgentProposalReviewState.REJECTED
    )
    fact.reviewed_by = actor.actor_id
    fact.reviewed_at = utcnow()
    fact.review_reason = payload.reason
    fact.version += 1
    record_operator_audit(
        session,
        actor=actor,
        action=f"incident.agent_fact_{payload.action}",
        target_type="agent_fact_proposal",
        target_id=fact.fact_id,
        reason=payload.reason,
        trace_id=trace_id,
        after={"review_state": fact.review_state.value, "version": fact.version},
    )
    session.commit()
    return _fact_response(fact)


def review_situation_report(
    session: Session,
    *,
    fire_id: str,
    report_revision_id: str,
    payload: AgentSituationReportReviewRequest,
    actor: Actor,
    trace_id: str,
) -> AdminAgentSituationReport:
    incident, episode = _incident_and_episode(session, fire_id)
    report = session.scalar(
        select(AgentSituationReportRevision)
        .where(
            AgentSituationReportRevision.report_revision_id == report_revision_id,
            AgentSituationReportRevision.incident_id == incident.id,
            AgentSituationReportRevision.episode_id == episode.id,
        )
    )
    if report is None:
        raise NotFoundError("agent_situation_report_revision", report_revision_id)
    if report.revision != payload.expected_revision:
        raise ConflictError(
            "agent_report_revision_conflict",
            "The situation report changed since it was loaded.",
        )
    if report.review_state.value != payload.expected_state:
        raise ConflictError(
            "agent_report_review_state_conflict",
            "The situation report has already been reviewed.",
        )
    report.review_state = (
        AgentReportReviewState.VALIDATED
        if payload.action == "validate"
        else AgentReportReviewState.REJECTED
    )
    report.reviewed_by = actor.actor_id
    report.reviewed_at = utcnow()
    report.review_reason = payload.reason
    record_operator_audit(
        session,
        actor=actor,
        action=f"incident.agent_situation_report_{payload.action}",
        target_type="agent_situation_report_revision",
        target_id=report.report_revision_id,
        reason=payload.reason,
        trace_id=trace_id,
        after={
            "review_state": report.review_state.value,
            "revision": report.revision,
            "published": False,
        },
    )
    if payload.action == "validate":
        refresh_campaign_day_publication_state(
            session,
            analysis_window_id=report.analysis_window_id,
        )
    session.commit()
    return _report_response(report)


def resolve_agent_review(
    session: Session,
    *,
    fire_id: str,
    review_id: str,
    payload: AgentReviewResolutionRequest,
    actor: Actor,
    trace_id: str,
) -> AdminAgentReviewPackage:
    incident, episode = _incident_and_episode(session, fire_id)
    task = session.execute(
        select(AgentReviewTask)
        .join(AgentMediaBatch, AgentMediaBatch.id == AgentReviewTask.batch_id)
        .where(
            AgentReviewTask.review_id == review_id,
            AgentMediaBatch.incident_id == incident.id,
            AgentMediaBatch.episode_id == episode.id,
        )
        .options(selectinload(AgentReviewTask.batch).selectinload(AgentMediaBatch.dispatch))
    ).scalar_one_or_none()
    if task is None:
        raise NotFoundError("agent_review_task", review_id)
    if task.state.value != payload.expected_state:
        raise ConflictError(
            "agent_review_state_conflict", "Review task changed since it was loaded."
        )
    raw_output = task.batch.dispatch.raw_output if task.batch.dispatch else None
    if payload.action == "approve" and (
        not isinstance(raw_output, dict) or raw_output.get("status") != "succeeded"
    ):
        raise ConflictError(
            "agent_review_incomplete_output",
            "Only a complete succeeded worker output can be approved.",
        )
    task.state = (
        AgentReviewState.RESOLVED if payload.action == "approve" else AgentReviewState.REJECTED
    )
    task.resolved_at = utcnow()
    task.resolution = payload.reason
    record_operator_audit(
        session,
        actor=actor,
        action=f"agent.review_{payload.action}",
        target_type="agent_review_task",
        target_id=task.review_id,
        reason=payload.reason,
        trace_id=trace_id,
        after={"state": task.state.value, "batch_id": task.batch.batch_id},
    )
    session.commit()
    return AdminAgentReviewPackage(
        review_id=task.review_id,
        batch_id=task.batch.batch_id,
        state=task.state.value,
        reason_codes=list(task.reason_codes),
        completed_at=as_utc(task.batch.completed_at) if task.batch.completed_at else None,
        result=raw_output,
    )
