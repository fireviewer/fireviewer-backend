"""Safe, versioned public incident projection and anonymous report workflow."""

from __future__ import annotations

import hmac
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC

from sqlalchemy import func, inspect, or_, select
from sqlalchemy.orm import Session, selectinload

from fire_viewer.core.config import Settings
from fire_viewer.core.ids import new_prefixed_id
from fire_viewer.core.security import Actor
from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import (
    ActiveFireZoneRevision,
    AgentAnalysisWindow,
    AgentFactProposal,
    AgentMediaItem,
    AgentSituationReportRevision,
    AgentSpatialProposal,
    AgentValidationCampaignDay,
    AuditEvent,
    Episode,
    IncidentBulletinEntry,
    IncidentGalleryItem,
    IncidentMapCapture,
    IncidentOfficialResource,
    IncidentOperationalInformation,
    IncidentPublicReport,
    IncidentSeries,
    Observation,
    PublicContributionSubmission,
    Source,
)
from fire_viewer.db.transactions import begin_write_transaction
from fire_viewer.domain.enums import (
    ActiveFireZoneReviewState,
    ActorType,
    AgentConsentState,
    AgentProposalReviewState,
    AgentReportReviewState,
    AgentValidationCampaignDayState,
    EvidenceSpatialMode,
    PublicContributionState,
    PublicReportState,
    VerificationState,
)
from fire_viewer.domain.errors import ConflictError, NotFoundError
from fire_viewer.domain.geospatial import haversine_m
from fire_viewer.domain.hashing import sha256_hex
from fire_viewer.domain.schemas import (
    AdminPublicReportEnvelope,
    AdminPublicReportListResponse,
    AdminPublicReportReviewRequest,
    PublicActiveFireZone,
    PublicAgentEvidenceReference,
    PublicAgentFact,
    PublicAgentSituationReport,
    PublicAgentSpatialResult,
    PublicDailyIntelligence,
    PublicDownload,
    PublicEvidenceProjection,
    PublicIncidentGalleryItem,
    PublicIncidentMapCapture,
    PublicIncidentReport,
    PublicIncidentReportReceipt,
    PublicIncidentReportRequest,
    PublicIncidentView,
    PublicModelMetadata,
    PublicObservationSummary,
    PublicOfficialResource,
    PublicOperationalInformation,
    PublicSourceSummary,
    PublicTimelineEvent,
)
from fire_viewer.services.common import record_audit, record_operator_audit
from fire_viewer.services.queries import (
    _current_episode,
    _episode_summary,
    _load_incident,
    _public_location,
    _require_canonical_public_visibility,
    get_viewer_manifest,
)

_PUBLIC_AUDIT_LABELS = {
    "incident.created": ("incident", "Incident créé"),
    "incident.status.changed": ("incident", "Statut de l'incident mis à jour"),
    "episode.created": ("episode", "Épisode créé"),
    "episode.reactivation.created": ("episode", "Nouvel épisode de réactivation"),
    "episode.evidence.verified": ("episode", "Validation humaine enregistrée"),
    "incident.corroborated": ("incident", "Incident corroboré par plusieurs preuves"),
    "observation.review.resolved": ("observation", "Observation validée et rattachée"),
    "observation.processed": ("observation", "Observation validée reçue"),
}


def _public_model(
    session: Session, incident: IncidentSeries, settings: Settings
) -> PublicModelMetadata:
    manifest = get_viewer_manifest(session=session, fire_id=incident.fire_id, settings=settings)
    asset = manifest.asset
    limitations = ["La visualisation 3D ne remplace pas une information opérationnelle."]
    if manifest.model_state != "available":
        limitations.append(
            "Le modèle 3D n'est pas disponible pour cet incident dans l'état publié actuel."
        )
    return PublicModelMetadata(
        state=manifest.model_state,
        version=asset.version if asset else None,
        sha256=asset.sha256 if asset else None,
        size_bytes=asset.size_bytes if asset else None,
        lod=asset.lod if asset else None,
        terrain_source_year=manifest.freshness.terrain_source_year,
        generated_at=manifest.freshness.generated_at,
        public_download_available=False,
        limitations=limitations,
    )


def _verification_label(episode: Episode) -> str:
    if episode.verification_state == VerificationState.VERIFIED:
        return "verified"
    if episode.verification_state == VerificationState.CORROBORATED:
        return "corroborated"
    return "review_required"


def _evidence_projections(
    rows: list[tuple[Observation, Source, Episode]],
) -> list[PublicEvidenceProjection]:
    projections: list[PublicEvidenceProjection] = []
    generalized_by_episode: dict[int, list[Observation]] = defaultdict(list)
    episode_ids: dict[int, str] = {}
    for observation, _source, episode in rows:
        if (
            observation.verification_state == VerificationState.VERIFIED
            and observation.public_spatial_mode == EvidenceSpatialMode.EXACT
        ):
            projections.append(
                PublicEvidenceProjection(
                    projection_id=f"marker-{observation.observation_id}",
                    episode_id=episode.episode_id,
                    kind="validated_marker",
                    verification_state=VerificationState.VERIFIED,
                    center={
                        "coordinates": (observation.longitude, observation.latitude),
                        "horizontal_uncertainty_m": observation.horizontal_uncertainty_m,
                    },
                    radius_m=observation.horizontal_uncertainty_m,
                    label="Observation validée et autorisée à la publication",
                    observed_at=as_utc(observation.observed_at),
                )
            )
        elif (
            observation.verification_state == VerificationState.CORROBORATED
            and observation.public_spatial_mode == EvidenceSpatialMode.GENERALIZED
        ):
            generalized_by_episode[episode.id].append(observation)
            episode_ids[episode.id] = episode.episode_id

    for episode_key, observations in sorted(
        generalized_by_episode.items(), key=lambda item: episode_ids[item[0]]
    ):
        center_lon = round(sum(item.longitude for item in observations) / len(observations), 2)
        center_lat = round(sum(item.latitude for item in observations) / len(observations), 2)
        radius_m = max(
            1_500.0,
            *(
                haversine_m(center_lon, center_lat, item.longitude, item.latitude)
                + item.horizontal_uncertainty_m
                for item in observations
            ),
        )
        if radius_m > 100_000.0:
            # A projection this broad would be operationally misleading. The
            # source summaries remain public, but no synthetic area is drawn.
            continue
        projections.append(
            PublicEvidenceProjection(
                projection_id=f"area-{episode_ids[episode_key]}",
                episode_id=episode_ids[episode_key],
                kind="generalized_area",
                verification_state=VerificationState.CORROBORATED,
                center={
                    "coordinates": (center_lon, center_lat),
                    "horizontal_uncertainty_m": min(radius_m, 50_000.0),
                },
                radius_m=radius_m,
                label=("Zone généralisée issue de preuves corroborantes, sans validation humaine"),
            )
        )
    return projections


def _public_agent_evidence(
    proposal: AgentFactProposal | AgentSpatialProposal,
) -> PublicAgentEvidenceReference:
    media = proposal.source_media_item
    consent = media.consent
    annotation = proposal.source_annotation if isinstance(proposal, AgentSpatialProposal) else None
    source_reference_url = None
    license_identifier = None
    if consent is not None and consent.state == AgentConsentState.GRANTED:
        # A public source URL is provenance, not permission to republish the
        # downloaded media. User uploads have no source URL here and therefore
        # remain linked only inside the private review workspace.
        source_reference_url = consent.source_reference_url
        license_identifier = consent.license_identifier
    return PublicAgentEvidenceReference(
        evidence_kind=(
            proposal.evidence_kind
            if isinstance(proposal, AgentFactProposal)
            else annotation.evidence_kind
            if annotation is not None
            else media.media_type.value
        ),
        evidence_id=(
            proposal.evidence_id
            if isinstance(proposal, AgentFactProposal)
            else annotation.evidence_id
            if annotation is not None
            else proposal.proposal_id
        ),
        source_annotation_id=annotation.annotation_id if annotation is not None else None,
        source_reference_url=source_reference_url,
        license_identifier=license_identifier,
    )


def _public_daily_intelligence(
    session: Session,
    incident: IncidentSeries,
) -> list[PublicDailyIntelligence]:
    """Return only reviewed outputs whose campaign day crossed publication."""

    days = list(
        session.scalars(
            select(AgentValidationCampaignDay)
            .join(
                AgentAnalysisWindow,
                AgentAnalysisWindow.id == AgentValidationCampaignDay.analysis_window_id,
            )
            .where(
                AgentAnalysisWindow.incident_id == incident.id,
                AgentValidationCampaignDay.state == AgentValidationCampaignDayState.PUBLISHED,
            )
            .options(selectinload(AgentValidationCampaignDay.analysis_window))
            .order_by(
                AgentAnalysisWindow.local_date.asc(),
                AgentValidationCampaignDay.ordinal.asc(),
            )
            .limit(500)
        )
    )
    if not days:
        return []
    window_ids = [day.analysis_window_id for day in days]
    reports = list(
        session.scalars(
            select(AgentSituationReportRevision)
            .where(
                AgentSituationReportRevision.analysis_window_id.in_(window_ids),
                AgentSituationReportRevision.review_state == AgentReportReviewState.VALIDATED,
            )
            .order_by(
                AgentSituationReportRevision.analysis_window_id.asc(),
                AgentSituationReportRevision.revision.desc(),
            )
        )
    )
    reports_by_window: dict[int, AgentSituationReportRevision] = {}
    for loaded_report in reports:
        reports_by_window.setdefault(loaded_report.analysis_window_id, loaded_report)
    facts = list(
        session.scalars(
            select(AgentFactProposal)
            .where(
                AgentFactProposal.analysis_window_id.in_(window_ids),
                AgentFactProposal.review_state == AgentProposalReviewState.VALIDATED,
            )
            .options(
                selectinload(AgentFactProposal.source_media_item).selectinload(
                    AgentMediaItem.consent
                )
            )
            .order_by(AgentFactProposal.as_of.asc(), AgentFactProposal.fact_id.asc())
        )
    )
    facts_by_window: dict[int, list[AgentFactProposal]] = defaultdict(list)
    for fact in facts:
        facts_by_window[fact.analysis_window_id].append(fact)
    spatial = list(
        session.scalars(
            select(AgentSpatialProposal)
            .where(
                AgentSpatialProposal.analysis_window_id.in_(window_ids),
                AgentSpatialProposal.review_state == AgentProposalReviewState.VALIDATED,
                AgentSpatialProposal.status == "projected_geometry",
                AgentSpatialProposal.proposal_kind.in_(
                    [
                        "active_fire_point",
                        "smoke_origin_point",
                        "visible_fire_front",
                        "probable_activity_envelope",
                        "burned_area_polygon",
                    ]
                ),
            )
            .options(
                selectinload(AgentSpatialProposal.source_media_item).selectinload(
                    AgentMediaItem.consent
                ),
                selectinload(AgentSpatialProposal.source_annotation),
            )
            .order_by(
                AgentSpatialProposal.observed_at.asc(),
                AgentSpatialProposal.proposal_id.asc(),
            )
        )
    )
    spatial_by_window: dict[int, list[AgentSpatialProposal]] = defaultdict(list)
    for proposal in spatial:
        spatial_by_window[proposal.analysis_window_id].append(proposal)
    episode_ids = {episode.id: episode.episode_id for episode in incident.episodes}
    result: list[PublicDailyIntelligence] = []
    for day in days:
        window = day.analysis_window
        report = reports_by_window.get(window.id)
        if report is None or report.reviewed_at is None or day.finished_at is None:
            # A published day must normally have both. Fail closed if a legacy
            # or manually edited row does not satisfy the public contract.
            continue
        result.append(
            PublicDailyIntelligence(
                analysis_id=window.analysis_id,
                episode_id=episode_ids[window.episode_id],
                local_date=window.local_date,
                published_at=as_utc(day.finished_at),
                report=PublicAgentSituationReport(
                    report_revision_id=report.report_revision_id,
                    revision=report.revision,
                    title=report.title,
                    body_markdown=report.body_markdown,
                    reviewed_at=as_utc(report.reviewed_at),
                ),
                facts=[
                    PublicAgentFact(
                        fact_id=fact.fact_id,
                        category=fact.category,
                        fact_key=fact.fact_key,
                        as_of=as_utc(fact.as_of),
                        certainty=fact.certainty,
                        summary=fact.summary,
                        value_number=fact.value_number,
                        value_text=fact.value_text,
                        value_boolean=fact.value_boolean,
                        unit=fact.unit,
                        evidence=_public_agent_evidence(fact),
                    )
                    for fact in facts_by_window[window.id]
                ],
                spatial_results=[
                    PublicAgentSpatialResult(
                        proposal_id=proposal.proposal_id,
                        kind=proposal.proposal_kind,
                        observed_at=as_utc(proposal.observed_at),
                        geometry_geojson=proposal.geometry_geojson,
                        geometry_origin=proposal.geometry_origin,
                        horizontal_accuracy_m=proposal.horizontal_accuracy_m,
                        evidence=_public_agent_evidence(proposal),
                    )
                    for proposal in spatial_by_window[window.id]
                    if proposal.proposal_kind is not None
                    and proposal.observed_at is not None
                    and proposal.geometry_geojson is not None
                    and proposal.geometry_origin is not None
                    and proposal.horizontal_accuracy_m is not None
                ],
            )
        )
    return result


def get_public_incident_view(
    session: Session, *, fire_id: str, settings: Settings
) -> PublicIncidentView:
    incident = _load_incident(session, fire_id)
    current = _current_episode(incident)
    _require_canonical_public_visibility(incident, current)
    if _public_location(incident, current) is None:
        return PublicIncidentView(
            fire_id=incident.fire_id,
            canonical_name=None,
            public_note=None,
            status=current.status,
            verification=_verification_label(current),
            freshness_at=as_utc(current.last_observed_at),
            last_human_validation_at=None,
            participatory_observation_count=None,
            participatory_published_count=None,
            participatory_received_count=None,
            location=None,
            facts=[],
            limitations=["Les données détaillées de cet incident ne sont pas publiées."],
            episodes=[],
            observations=[],
            evidence_projections=[],
            active_fire_zone=None,
            active_fire_zones=[],
            daily_intelligence=[],
            map_gallery=[],
            gallery=[],
            official_resources=[],
            operational_information=[],
            sources=[],
            timeline=[],
            model=PublicModelMetadata(
                state="withheld",
                public_download_available=False,
                limitations=["Les données spatiales et le modèle ne sont pas publiés."],
            ),
            downloads=[],
        )

    rows = session.execute(
        select(Observation, Source, Episode)
        .join(Source, Source.id == Observation.source_id)
        .join(Episode, Episode.id == Observation.attached_episode_id)
        .where(
            Observation.attached_incident_id == incident.id,
            Observation.verification_state.in_(
                [VerificationState.CORROBORATED, VerificationState.VERIFIED]
            ),
            Source.enabled.is_(True),
        )
        .order_by(Observation.observed_at.desc())
    ).all()
    observations: list[PublicObservationSummary] = []
    source_counts: dict[int, int] = defaultdict(int)
    source_rows: dict[int, Source] = {}
    for observation, source, episode in rows:
        observations.append(
            PublicObservationSummary(
                observation_id=observation.observation_id,
                episode_id=episode.episode_id,
                type=source.source_type,
                observed_at=as_utc(observation.observed_at),
                received_at=as_utc(observation.received_at),
                uncertainty_m=(
                    max(observation.horizontal_uncertainty_m, 1_500.0)
                    if observation.verification_state == VerificationState.CORROBORATED
                    else observation.horizontal_uncertainty_m
                ),
                area_label=(
                    incident.canonical_name
                    if current.verification_state == VerificationState.VERIFIED
                    else "Zone généralisée de l'incident"
                ),
                verification_state=observation.verification_state,
                spatial_mode=observation.public_spatial_mode,
            )
        )
        source_counts[source.id] += 1
        source_rows[source.id] = source
    sources = [
        PublicSourceSummary(
            source_id=source.source_key,
            type=source.source_type,
            name=source.public_display_name,
            trust=source.trust,
            license=source.public_license,
            external_reference=source.public_reference_url,
            transformations=list(source.public_transformations),
            observation_count=source_counts[source_id],
        )
        for source_id, source in sorted(source_rows.items(), key=lambda item: item[1].source_key)
    ]
    bulletin_entries: Sequence[IncidentBulletinEntry] = []
    bulletin_sources: Sequence[Source] = []
    # ``d8f3a1c5b720`` was inserted into an already deployed migration chain.
    # A database stamped at the later historical head can therefore legitimately
    # lack this additive table until the forward repair migration is applied.
    # Keep the existing public bulletin readable during that controlled rollout:
    # an absent optional table means "no administrator-authored entries", never a
    # failed incident page or an invented replacement value.
    if inspect(session.get_bind()).has_table(IncidentBulletinEntry.__tablename__):
        bulletin_entries = (
            session.execute(
                select(IncidentBulletinEntry)
                .where(
                    IncidentBulletinEntry.incident_id == incident.id,
                    IncidentBulletinEntry.state == "PUBLISHED",
                )
                .order_by(
                    IncidentBulletinEntry.effective_at.desc(),
                    IncidentBulletinEntry.entry_id.asc(),
                )
            )
            .scalars()
            .all()
        )
        bulletin_sources = (
            session.execute(
                select(Source)
                .join(IncidentBulletinEntry, IncidentBulletinEntry.source_id == Source.id)
                .where(
                    IncidentBulletinEntry.incident_id == incident.id,
                    IncidentBulletinEntry.state == "PUBLISHED",
                    Source.enabled.is_(True),
                )
            )
            .scalars()
            .all()
        )
    for source in bulletin_sources:
        source_rows.setdefault(source.id, source)
        source_counts.setdefault(source.id, 0)
    sources = [
        PublicSourceSummary(
            source_id=source.source_key,
            type=source.source_type,
            name=source.public_display_name,
            trust=source.trust,
            license=source.public_license,
            external_reference=source.public_reference_url,
            transformations=list(source.public_transformations),
            observation_count=source_counts[source_id],
        )
        for source_id, source in sorted(source_rows.items(), key=lambda item: item[1].source_key)
    ]
    # A contribution remains private unless an operator accepts it.  The public view
    # intentionally exposes only this aggregate: no pending/rejected/withdrawn receipt,
    # consent detail, media, description, or location is projected here.
    reviewed_contribution_count = session.scalar(
        select(func.count(PublicContributionSubmission.id)).where(
            PublicContributionSubmission.incident_id == incident.id,
            PublicContributionSubmission.state == PublicContributionState.ACCEPTED,
        )
    )
    participatory_observation_count = reviewed_contribution_count or None
    participatory_received_count = None

    evidence_projections = _evidence_projections(
        [(observation, source, episode) for observation, source, episode in rows]
    )
    official_resources = (
        session.execute(
            select(IncidentOfficialResource)
            .where(
                IncidentOfficialResource.incident_id == incident.id,
                IncidentOfficialResource.state == "PUBLISHED",
            )
            .order_by(
                IncidentOfficialResource.published_at.desc(),
                IncidentOfficialResource.resource_id.asc(),
            )
        )
        .scalars()
        .all()
    )
    gallery_items = (
        session.execute(
            select(IncidentGalleryItem)
            .where(
                IncidentGalleryItem.incident_id == incident.id,
                IncidentGalleryItem.state == "PUBLISHED",
            )
            .order_by(
                IncidentGalleryItem.captured_at.desc(),
                IncidentGalleryItem.published_at.desc(),
                IncidentGalleryItem.gallery_item_id.asc(),
            )
        )
        .scalars()
        .all()
    )
    operational_information = (
        session.execute(
            select(IncidentOperationalInformation)
            .where(
                IncidentOperationalInformation.incident_id == incident.id,
                IncidentOperationalInformation.state == "PUBLISHED",
            )
            .order_by(
                IncidentOperationalInformation.effective_at.desc(),
                IncidentOperationalInformation.information_id.asc(),
            )
        )
        .scalars()
        .all()
    )
    episode_public_ids = {episode.id: episode.episode_id for episode in incident.episodes}
    episode_target_ids = {episode.episode_id: episode.episode_id for episode in incident.episodes}
    episode_target_ids.update(
        {
            f"{incident.fire_id}/{episode.episode_id}": episode.episode_id
            for episode in incident.episodes
        }
    )
    audit_events = (
        session.execute(
            select(AuditEvent)
            .where(AuditEvent.target_id.in_([incident.fire_id, *episode_target_ids.keys()]))
            .where(AuditEvent.action.in_(list(_PUBLIC_AUDIT_LABELS)))
            .order_by(AuditEvent.occurred_at.desc())
            .limit(80)
        )
        .scalars()
        .all()
    )
    timeline = [
        PublicTimelineEvent(
            occurred_at=as_utc(event.occurred_at),
            kind=_PUBLIC_AUDIT_LABELS[event.action][0],
            label=_PUBLIC_AUDIT_LABELS[event.action][1],
            episode_id=episode_target_ids.get(event.target_id),
        )
        for event in audit_events
    ]
    timeline.extend(
        PublicTimelineEvent(
            occurred_at=as_utc(item.effective_at or item.published_at or item.proposed_at),
            kind="operational",
            label=(
                f"{item.title} : {item.value_text}"
                if item.value_text is not None
                else f"{item.title} : {item.value_number:g} {item.unit or ''}".strip()
            ),
            episode_id=(
                episode_public_ids.get(item.episode_id) if item.episode_id is not None else None
            ),
        )
        for item in operational_information
    )
    timeline.extend(
        PublicTimelineEvent(
            occurred_at=as_utc(item.effective_at),
            kind="incident",
            label=item.body,
            episode_id=(
                episode_public_ids.get(item.episode_id) if item.episode_id is not None else None
            ),
        )
        for item in bulletin_entries
        if item.kind == "timeline"
    )
    timeline.sort(key=lambda item: item.occurred_at, reverse=True)
    model = _public_model(session, incident, settings)
    daily_intelligence = _public_daily_intelligence(session, incident)
    published_window_ids = select(AgentValidationCampaignDay.analysis_window_id).where(
        AgentValidationCampaignDay.state == AgentValidationCampaignDayState.PUBLISHED
    )
    active_zones = list(
        session.scalars(
            select(ActiveFireZoneRevision)
            .where(
                ActiveFireZoneRevision.incident_id == incident.id,
                ActiveFireZoneRevision.episode_id == current.id,
                ActiveFireZoneRevision.review_state == ActiveFireZoneReviewState.READY_FOR_PUBLICATION,
                or_(
                    ActiveFireZoneRevision.analysis_window_id.is_(None),
                    ActiveFireZoneRevision.analysis_window_id.in_(published_window_ids),
                ),
            )
            .order_by(
                ActiveFireZoneRevision.valid_at.asc(),
                ActiveFireZoneRevision.revision.asc(),
            )
            .limit(500)
        )
    )
    active_zone = active_zones[-1] if active_zones else None
    map_captures = list(
        session.scalars(
            select(IncidentMapCapture)
            .join(
                ActiveFireZoneRevision,
                ActiveFireZoneRevision.id == IncidentMapCapture.active_zone_revision_id,
            )
            .where(
                IncidentMapCapture.incident_id == incident.id,
                IncidentMapCapture.episode_id == current.id,
                ActiveFireZoneRevision.review_state
                == ActiveFireZoneReviewState.READY_FOR_PUBLICATION,
                or_(
                    ActiveFireZoneRevision.analysis_window_id.is_(None),
                    ActiveFireZoneRevision.analysis_window_id.in_(published_window_ids),
                ),
            )
            .options(selectinload(IncidentMapCapture.active_zone_revision))
            .order_by(
                IncidentMapCapture.local_date.asc(),
                IncidentMapCapture.captured_at.asc(),
            )
        )
    )
    if current.verification_state == VerificationState.VERIFIED:
        facts = ["Une validation humaine de cet épisode a été enregistrée."]
    else:
        facts = [
            f"{current.corroborating_source_count} preuves indépendantes corroborent cet épisode."
        ]
    if incident.public_note:
        facts.append(incident.public_note)
    facts.extend(item.body for item in bulletin_entries if item.kind == "fact")
    limitations = [
        "Les positions et périmètres peuvent être estimés.",
        "Cette fiche ne remplace pas les consignes des services d'urgence.",
    ]
    if current.review_required:
        limitations.append("Une revue complémentaire est requise pour l'épisode courant.")
    if current.verification_state == VerificationState.CORROBORATED:
        limitations.append(
            "Cette fiche n'a pas encore reçu de validation humaine ; "
            "les positions sont volontairement généralisées."
        )
    return PublicIncidentView(
        fire_id=incident.fire_id,
        canonical_name=(
            incident.canonical_name
            if current.verification_state == VerificationState.VERIFIED
            else None
        ),
        public_note=incident.public_note,
        status=current.status,
        verification=_verification_label(current),
        freshness_at=as_utc(current.last_observed_at),
        last_human_validation_at=(
            as_utc(current.validated_at)
            if current.verification_state == VerificationState.VERIFIED and current.validated_at
            else None
        ),
        participatory_observation_count=participatory_observation_count,
        participatory_published_count=participatory_observation_count,
        participatory_received_count=participatory_received_count,
        location=_public_location(incident, current),
        facts=facts,
        limitations=limitations,
        episodes=[
            _episode_summary(episode, settings)
            for episode in sorted(incident.episodes, key=lambda value: value.ordinal, reverse=True)
        ],
        observations=observations,
        evidence_projections=evidence_projections,
        active_fire_zone=(
            PublicActiveFireZone(
                zone_revision_id=active_zone.zone_revision_id,
                revision=active_zone.revision,
                valid_at=as_utc(active_zone.valid_at),
                analysis_id=(
                    active_zone.analysis_window.analysis_id
                    if active_zone.analysis_window is not None
                    else None
                ),
                geometry_geojson=active_zone.geometry_geojson,
            )
            if active_zone is not None
            else None
        ),
        active_fire_zones=[
            PublicActiveFireZone(
                zone_revision_id=zone.zone_revision_id,
                revision=zone.revision,
                valid_at=as_utc(zone.valid_at),
                analysis_id=(
                    zone.analysis_window.analysis_id
                    if zone.analysis_window is not None
                    else None
                ),
                geometry_geojson=zone.geometry_geojson,
            )
            for zone in active_zones
        ],
        daily_intelligence=daily_intelligence,
        map_gallery=[
            PublicIncidentMapCapture(
                capture_id=item.capture_id,
                zone_revision_id=item.active_zone_revision.zone_revision_id,
                local_date=item.local_date,
                captured_at=as_utc(item.captured_at),
                image_url=(f"/api/v1/incident/{incident.fire_id}/map-gallery/{item.capture_id}"),
                width_px=item.width_px,
                height_px=item.height_px,
            )
            for item in map_captures
        ],
        gallery=[
            PublicIncidentGalleryItem(
                gallery_item_id=item.gallery_item_id,
                title=item.title,
                caption=item.caption,
                alt_text=item.alt_text,
                media_url=item.media_url,
                media_kind=item.media_kind,
                credit=item.credit,
                license_label=item.license_label,
                captured_at=as_utc(item.captured_at) if item.captured_at else None,
                published_at=as_utc(item.published_at) if item.published_at else None,
                episode_id=(
                    episode_public_ids.get(item.episode_id)
                    if item.episode_id is not None
                    else None
                ),
            )
            for item in gallery_items
        ],
        official_resources=[
            PublicOfficialResource(
                resource_id=item.resource_id,
                kind=item.kind,
                title=item.title,
                publisher=item.publisher,
                url=item.url,
                published_at=as_utc(item.published_at) if item.published_at else None,
                episode_id=(
                    episode_public_ids.get(item.episode_id)
                    if item.episode_id is not None
                    else None
                ),
            )
            for item in official_resources
        ],
        operational_information=[
            PublicOperationalInformation(
                information_id=item.information_id,
                kind=item.kind,
                title=item.title,
                value_text=item.value_text,
                value_number=item.value_number,
                unit=item.unit,
                locality=item.locality,
                authority_kind=item.authority_kind,
                authority_name=item.authority_name,
                source_url=item.source_url,
                effective_at=as_utc(item.effective_at) if item.effective_at else None,
                published_at=as_utc(item.published_at) if item.published_at else None,
                episode_id=(
                    episode_public_ids.get(item.episode_id)
                    if item.episode_id is not None
                    else None
                ),
            )
            for item in operational_information
        ],
        sources=sources,
        timeline=timeline,
        model=model,
        downloads=[
            PublicDownload(
                id="incident-json",
                label="Fiche publique JSON",
                media_type="application/json",
                url=f"/api/v1/incident/{incident.fire_id}/public-view/export.json",
            ),
            PublicDownload(
                id="timeline-csv",
                label="Chronologie publique CSV",
                media_type="text/csv",
                url=f"/api/v1/incident/{incident.fire_id}/public-view/timeline.csv",
            ),
        ],
    )


def _report_response(report: IncidentPublicReport) -> PublicIncidentReport:
    return PublicIncidentReport(
        report_id=report.report_id,
        fire_id=report.incident.fire_id,
        category=report.category,
        message=report.message,
        state=report.state,
        submitted_at=as_utc(report.submitted_at),
        reviewed_at=as_utc(report.reviewed_at) if report.reviewed_at else None,
        closure_reason=report.closure_reason,
        version=report.version,
    )


def submit_public_report(
    session: Session,
    *,
    fire_id: str,
    payload: PublicIncidentReportRequest,
    origin: str,
    trace_id: str,
    settings: Settings,
) -> PublicIncidentReportReceipt:
    incident = _load_incident(session, fire_id)
    current = _current_episode(incident)
    _require_canonical_public_visibility(incident, current)
    now = utcnow()
    day = now.astimezone(UTC).date().isoformat()
    origin_fingerprint = hmac.digest(
        settings.public_report_hash_secret.encode(), f"{origin}:{day}".encode(), "sha256"
    ).hex()
    content_hash = sha256_hex(
        {"category": payload.category.value, "message": payload.message.strip()}
    )
    begin_write_transaction(session)
    duplicate = session.execute(
        select(IncidentPublicReport).where(
            IncidentPublicReport.incident_id == incident.id,
            IncidentPublicReport.origin_fingerprint == origin_fingerprint,
            IncidentPublicReport.content_hash == content_hash,
            IncidentPublicReport.submitted_day == day,
        )
    ).scalar_one_or_none()
    if duplicate is not None:
        session.rollback()
        return PublicIncidentReportReceipt(
            receipt_id=duplicate.report_id,
            submitted_at=as_utc(duplicate.submitted_at),
            replayed=True,
        )
    count = session.execute(
        select(func.count(IncidentPublicReport.id)).where(
            IncidentPublicReport.origin_fingerprint == origin_fingerprint,
            IncidentPublicReport.submitted_day == day,
        )
    ).scalar_one()
    if count >= settings.public_report_rate_limit_per_day:
        raise ConflictError("public_report_rate_limited", "Daily anonymous report limit reached.")
    report = IncidentPublicReport(
        report_id=new_prefixed_id("R"),
        incident_id=incident.id,
        category=payload.category,
        message=payload.message.strip(),
        origin_fingerprint=origin_fingerprint,
        content_hash=content_hash,
        submitted_day=day,
    )
    session.add(report)
    session.flush()
    record_audit(
        session,
        actor_type=ActorType.PUBLIC_SOURCE,
        actor_id="anonymous-report",
        action="incident.public_report.submitted",
        target_type="incident",
        target_id=incident.fire_id,
        reason="anonymous public correction request",
        trace_id=trace_id,
        payload={
            "report_id": report.report_id,
            "category": report.category.value,
            "content_hash": content_hash,
        },
    )
    session.commit()
    return PublicIncidentReportReceipt(
        receipt_id=report.report_id, submitted_at=as_utc(report.submitted_at)
    )


def list_public_reports(
    session: Session, *, state: PublicReportState | None = None
) -> AdminPublicReportListResponse:
    statement = (
        select(IncidentPublicReport)
        .join(IncidentSeries)
        .order_by(IncidentPublicReport.submitted_at.desc())
    )
    if state is not None:
        statement = statement.where(IncidentPublicReport.state == state)
    reports = session.execute(statement).scalars().all()
    return AdminPublicReportListResponse(reports=[_report_response(report) for report in reports])


def review_public_report(
    session: Session,
    *,
    report_id: str,
    payload: AdminPublicReportReviewRequest,
    actor: Actor,
    trace_id: str,
) -> AdminPublicReportEnvelope:
    begin_write_transaction(session)
    report = session.execute(
        select(IncidentPublicReport)
        .where(IncidentPublicReport.report_id == report_id)
        .with_for_update()
    ).scalar_one_or_none()
    if report is None:
        raise NotFoundError("public_report", report_id)
    if report.version != payload.expected_version:
        raise ConflictError(
            "public_report_version_conflict", "The report has changed since it was loaded."
        )
    if report.state != PublicReportState.PENDING:
        raise ConflictError(
            "public_report_already_reviewed", "The report has already been reviewed."
        )
    before = {"state": report.state.value, "version": report.version}
    report.state = payload.state
    report.closure_reason = payload.reason
    report.reviewed_by = actor.actor_id
    report.reviewed_at = utcnow()
    report.version += 1
    session.flush()
    record_operator_audit(
        session,
        actor=actor,
        action="incident.public_report.reviewed",
        target_type="public_report",
        target_id=report.report_id,
        reason=payload.reason,
        trace_id=trace_id,
        before=before,
        after={"state": report.state.value, "version": report.version},
        payload={"fire_id": report.incident.fire_id},
    )
    session.commit()
    return AdminPublicReportEnvelope(report=_report_response(report), trace_id=trace_id)
