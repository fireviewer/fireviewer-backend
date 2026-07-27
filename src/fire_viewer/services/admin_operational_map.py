"""Read-only projection for the incident-centred national administration map."""

from __future__ import annotations

import re
from collections import defaultdict

from sqlalchemy import case, or_, select
from sqlalchemy.orm import Session, selectinload

from fire_viewer.core.time import as_utc, utcnow
from fire_viewer.db.models import (
    Episode,
    IncidentSeries,
    ManifestRevision,
    ModelAsset,
    Observation,
    SpatialPackage,
    SpatialPackageFile,
    SpatialZoneRevision,
    ZonePublication,
)
from fire_viewer.domain.enums import (
    IncidentStatus,
    SpatialPackageFileKind,
    VerificationState,
)
from fire_viewer.domain.schemas import (
    AdminOperationalMapIncident,
    AdminOperationalMapModel,
    AdminOperationalMapResponse,
    AdminOperationalMapSignal,
    AdminOperationalMapSummary,
)

_PROFILE_TOKEN_RE = re.compile(r"[^a-z0-9]+")
_PROFILE_ALIASES: tuple[tuple[str, frozenset[str]], ...] = (
    ("close", frozenset({"close", "near", "nearby", "rapproche", "rapprochee"})),
    ("local", frozenset({"local", "sector", "secteur", "standard"})),
    ("extended", frozenset({"extended", "wide", "overview", "etendu", "etendue"})),
    ("mobile", frozenset({"mobile"})),
    ("desktop", frozenset({"desktop"})),
)


def model_profile(value: str | None) -> str:
    tokens = set(_PROFILE_TOKEN_RE.split((value or "").casefold()))
    for profile, aliases in _PROFILE_ALIASES:
        if tokens.intersection(aliases):
            return profile
    return "unspecified"


def _current_episode(incident: IncidentSeries) -> Episode:
    return next(episode for episode in incident.episodes if episode.is_current)


def _current_manifest(incident: IncidentSeries) -> ManifestRevision | None:
    return next((revision for revision in incident.manifest_revisions if revision.is_current), None)


def _package_model(
    package: SpatialPackage,
    file: SpatialPackageFile,
    *,
    is_current: bool,
) -> AdminOperationalMapModel:
    catalog_path = str(file.provenance.get("catalog_path", ""))
    return AdminOperationalMapModel(
        profile=model_profile(catalog_path),
        source="spatial_package",
        state=package.state.value,
        package_id=package.package_id,
        package_file_id=file.id,
        sha256=file.sha256,
        size_bytes=file.size_bytes,
        is_current=is_current,
        access_path=f"/api/v2/admin/packages/{package.package_id}/files/{file.id}",
    )


def _asset_model(asset: ModelAsset, *, current_asset_id: int | None) -> AdminOperationalMapModel:
    controlled = asset.glb_url.startswith(("local://", "vercel-blob://"))
    return AdminOperationalMapModel(
        profile=model_profile(asset.lod.value),
        source="model_asset",
        state=asset.state.value,
        version=asset.version,
        asset_id=asset.asset_id,
        sha256=asset.sha256,
        size_bytes=asset.size_bytes,
        is_current=asset.id == current_asset_id,
        access_path=f"/api/v2/admin/assets/{asset.asset_id}" if controlled else None,
    )


def get_operational_map(session: Session) -> AdminOperationalMapResponse:
    incidents = list(
        session.execute(
            select(IncidentSeries)
            .options(
                selectinload(IncidentSeries.episodes),
                selectinload(IncidentSeries.observations),
                selectinload(IncidentSeries.manifest_revisions).selectinload(
                    ManifestRevision.asset
                ),
                selectinload(IncidentSeries.manifest_revisions)
                .selectinload(ManifestRevision.package)
                .selectinload(SpatialPackage.files),
                selectinload(IncidentSeries.manifest_revisions)
                .selectinload(ManifestRevision.spatial_zone_revision)
                .selectinload(SpatialZoneRevision.zone),
            )
            .order_by(IncidentSeries.updated_at.desc(), IncidentSeries.fire_id.asc())
            .limit(5_000)
        )
        .scalars()
        .unique()
    )
    observations = list(
        session.execute(
            select(Observation)
            .where(Observation.verification_state != VerificationState.REJECTED)
            .options(
                selectinload(Observation.source),
                selectinload(Observation.proposed_incident),
                selectinload(Observation.attached_incident),
            )
            .order_by(
                case(
                    (
                        or_(
                            Observation.attached_incident_id.is_(None),
                            Observation.verification_state == VerificationState.PENDING_REVIEW,
                        ),
                        0,
                    ),
                    else_=1,
                ),
                Observation.observed_at.desc(),
                Observation.observation_id.asc(),
            )
            .limit(5_000)
        )
        .scalars()
        .unique()
    )
    manifests = {
        incident.id: manifest
        for incident in incidents
        if (manifest := _current_manifest(incident)) is not None
    }
    revision_ids = {
        manifest.spatial_zone_revision_id
        for manifest in manifests.values()
        if manifest.spatial_zone_revision_id is not None
    }

    publications_by_revision: dict[int, ZonePublication] = {}
    assets_by_revision: dict[int, list[ModelAsset]] = defaultdict(list)
    if revision_ids:
        publications = list(
            session.execute(
                select(ZonePublication)
                .where(
                    ZonePublication.spatial_zone_revision_id.in_(revision_ids),
                    ZonePublication.is_active.is_(True),
                )
                .options(selectinload(ZonePublication.package).selectinload(SpatialPackage.files))
            )
            .scalars()
            .unique()
        )
        publications_by_revision = {
            publication.spatial_zone_revision_id: publication for publication in publications
        }
        for asset in session.execute(
            select(ModelAsset).where(ModelAsset.spatial_zone_revision_id.in_(revision_ids))
        ).scalars():
            assert asset.spatial_zone_revision_id is not None
            assets_by_revision[asset.spatial_zone_revision_id].append(asset)

    rows: list[AdminOperationalMapIncident] = []
    for incident in incidents:
        episode = _current_episode(incident)
        manifest = manifests.get(incident.id)
        revision = manifest.spatial_zone_revision if manifest else None
        current_asset = manifest.asset if manifest else None
        current_package = manifest.package if manifest else None
        models: list[AdminOperationalMapModel] = []
        active_package_id: str | None = None

        if revision is not None:
            publication = publications_by_revision.get(revision.id)
            packages: list[SpatialPackage] = []
            if current_package is not None:
                packages.append(current_package)
            if publication is not None:
                active_package_id = publication.package.package_id
                if current_package is None or publication.package.id != current_package.id:
                    packages.append(publication.package)
            for package in packages:
                models.extend(
                    _package_model(
                        package,
                        file,
                        is_current=current_package is not None and package.id == current_package.id,
                    )
                    for file in package.files
                    if file.kind == SpatialPackageFileKind.GLB
                )
            package_hashes = {model.sha256 for model in models}
            models.extend(
                _asset_model(asset, current_asset_id=current_asset.id if current_asset else None)
                for asset in assets_by_revision.get(revision.id, [])
                if asset.sha256 not in package_hashes
            )
        elif current_asset is not None:
            models.append(_asset_model(current_asset, current_asset_id=current_asset.id))

        models.sort(key=lambda item: (item.profile, item.source, item.sha256))
        model_update_available = bool(active_package_id) and (
            current_package is None or current_package.package_id != active_package_id
        )
        rows.append(
            AdminOperationalMapIncident(
                fire_id=incident.fire_id,
                canonical_name=incident.canonical_name,
                territory_code=incident.territory_code,
                longitude=incident.reference_lon,
                latitude=incident.reference_lat,
                horizontal_uncertainty_m=incident.horizontal_uncertainty_m,
                status=episode.status,
                verification_state=episode.verification_state,
                visibility=incident.public_visibility,
                current_episode_id=episode.episode_id,
                last_observed_at=as_utc(episode.last_observed_at),
                review_required=episode.review_required,
                pending_observation_count=sum(
                    observation.verification_state == VerificationState.PENDING_REVIEW
                    for observation in incident.observations
                ),
                spatial_zone_id=revision.zone.zone_id if revision else None,
                spatial_zone_revision=revision.revision if revision else None,
                current_package_id=current_package.package_id if current_package else None,
                active_package_id=active_package_id,
                models=models,
                model_update_available=model_update_available,
            )
        )

    signal_rows = [
        AdminOperationalMapSignal(
            observation_id=observation.observation_id,
            source_key=observation.source.source_key,
            source_type=observation.source.source_type,
            longitude=observation.longitude,
            latitude=observation.latitude,
            horizontal_uncertainty_m=observation.horizontal_uncertainty_m,
            territory_code=observation.territory_code,
            canonical_name_hint=observation.canonical_name_hint,
            observed_at=as_utc(observation.observed_at),
            received_at=as_utc(observation.received_at),
            verification_state=observation.verification_state,
            match_decision=observation.match_decision,
            state=(
                "pending"
                if observation.attached_incident is None
                or observation.verification_state == VerificationState.PENDING_REVIEW
                else "attached"
            ),
            proposed_fire_id=(
                observation.proposed_incident.fire_id
                if observation.proposed_incident is not None
                else None
            ),
            attached_fire_id=(
                observation.attached_incident.fire_id
                if observation.attached_incident is not None
                else None
            ),
            version=observation.version,
        )
        for observation in observations
    ]

    return AdminOperationalMapResponse(
        generated_at=utcnow(),
        summary=AdminOperationalMapSummary(
            total_incidents=len(rows),
            active_incidents=sum(row.status == IncidentStatus.ACTIVE_CONFIRMED for row in rows),
            monitoring_incidents=sum(row.status == IncidentStatus.MONITORING for row in rows),
            archived_incidents=sum(
                row.status in {IncidentStatus.EXTINGUISHED, IncidentStatus.CLOSED} for row in rows
            ),
            incidents_requiring_review=sum(row.review_required for row in rows),
            pending_signals=sum(row.state == "pending" for row in signal_rows),
            attached_signals=sum(row.state == "attached" for row in signal_rows),
            incidents_with_models=sum(bool(row.models) for row in rows),
            model_updates_available=sum(row.model_update_available for row in rows),
        ),
        incidents=rows,
        signals=signal_rows,
    )
