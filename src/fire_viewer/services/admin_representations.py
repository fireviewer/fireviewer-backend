"""Attach a reviewed local spatial package to one permanent incident identifier."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from fire_viewer.core.config import Settings
from fire_viewer.core.ids import new_asset_id
from fire_viewer.core.security import Actor
from fire_viewer.core.time import utcnow
from fire_viewer.db.models import (
    IncidentSeries,
    ManifestRevision,
    ModelAsset,
    SpatialPackage,
    SpatialPackageFile,
)
from fire_viewer.db.transactions import begin_write_transaction
from fire_viewer.domain.enums import (
    AssetLod,
    AssetState,
    SpatialPackageFileKind,
    SpatialPackageState,
)
from fire_viewer.domain.errors import ConflictError, NotFoundError
from fire_viewer.domain.hashing import sha256_hex
from fire_viewer.domain.schemas import (
    AdminIncidentRepresentationAttachRequest,
    AdminIncidentRepresentationAttachResponse,
)
from fire_viewer.services.admin_operational_map import model_profile
from fire_viewer.services.common import record_operator_audit
from fire_viewer.services.idempotency import find_replay, store_response


@dataclass(frozen=True, slots=True)
class RepresentationAttachmentOutcome:
    response: AdminIncidentRepresentationAttachResponse
    replayed: bool


def _profiles(files: list[SpatialPackageFile]) -> dict[str, SpatialPackageFile]:
    result: dict[str, SpatialPackageFile] = {}
    for file in files:
        if file.kind != SpatialPackageFileKind.GLB:
            continue
        profile = model_profile(str(file.provenance.get("catalog_path", "")))
        if profile == "unspecified":
            if sum(item.kind == SpatialPackageFileKind.GLB for item in files) == 1:
                profile = "local"
            else:
                raise ConflictError(
                    "spatial_package_model_profile_missing",
                    "Each GLB must declare close, local or extended in its catalog path.",
                )
        if profile in result:
            raise ConflictError(
                "spatial_package_model_profile_duplicate",
                f"The package contains more than one {profile} GLB.",
            )
        result[profile] = file
    if not result:
        raise ConflictError(
            "spatial_package_has_no_glb",
            "The package contains no GLB representation.",
        )
    if len(result) > 5:
        raise ConflictError(
            "spatial_package_has_too_many_glb_profiles",
            "A spatial package may expose at most five GLB profiles.",
        )
    return result


def _is_tiled_scene(files: list[SpatialPackageFile]) -> bool:
    kinds = {item.kind for item in files}
    if {
        SpatialPackageFileKind.FWTILE,
        SpatialPackageFileKind.FWTERRAIN,
    }.issubset(kinds):
        return True
    glb_files = [item for item in files if item.kind == SpatialPackageFileKind.GLB]
    return len(glb_files) > 5 or any(
        str(item.provenance.get("catalog_path", "")).startswith("vectors/") for item in glb_files
    )


def attach_incident_package_in_transaction(
    session: Session,
    *,
    incident: IncidentSeries,
    package: SpatialPackage,
    payload: AdminIncidentRepresentationAttachRequest,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> AdminIncidentRepresentationAttachResponse:
    """Attach a locked package to a locked incident without committing the transaction."""

    if incident.version != payload.expected_incident_version:
        raise ConflictError(
            "stale_incident_version",
            "The incident was modified by another operation.",
            extra={"current_version": incident.version},
        )
    episode = next((item for item in incident.episodes if item.is_current), None)
    if episode is None:
        raise ConflictError(
            "incident_has_no_current_episode",
            "The incident has no current episode.",
        )
    if package.state not in {SpatialPackageState.PREVIEWABLE, SpatialPackageState.PUBLISHED}:
        raise ConflictError(
            "spatial_package_not_attachable",
            "Only a previewable or published package can be attached to an incident.",
        )
    if package.spatial_zone_revision_id is None:
        raise ConflictError(
            "spatial_package_has_no_revision",
            "The package is not bound to a spatial zone revision.",
        )

    omniverse_scene = package.provenance.get("package_role") == "omniverse_map"
    tiled_scene = omniverse_scene or _is_tiled_scene(package.files)
    profiles = {} if tiled_scene else _profiles(package.files)
    primary_profile: str | None = None
    if profiles:
        primary_profile = payload.primary_profile
        if primary_profile not in profiles:
            primary_profile = next(
                profile
                for profile in ("local", "close", "extended", "mobile", "desktop")
                if profile in profiles
            )

    now = utcnow()
    asset_state = (
        AssetState.PUBLISHED
        if package.state == SpatialPackageState.PUBLISHED
        else AssetState.VALIDATED
    )
    assets: dict[str, ModelAsset] = {}
    for profile, file in profiles.items():
        existing = session.execute(
            select(ModelAsset).where(ModelAsset.spatial_package_file_id == file.id)
        ).scalar_one_or_none()
        if existing is not None:
            if (
                package.state == SpatialPackageState.PUBLISHED
                and existing.state == AssetState.VALIDATED
            ):
                existing.state = AssetState.PUBLISHED
                existing.published_at = now
                existing.purge_after = None
            assets[profile] = existing
            continue
        lod = AssetLod(profile)
        version = (
            int(
                session.execute(
                    select(func.max(ModelAsset.version)).where(
                        ModelAsset.spatial_zone_revision_id == package.spatial_zone_revision_id,
                        ModelAsset.lod == lod,
                    )
                ).scalar_one()
                or 0
            )
            + 1
        )
        asset = ModelAsset(
            asset_id=new_asset_id(),
            spatial_zone_revision_id=package.spatial_zone_revision_id,
            spatial_package_file_id=file.id,
            version=version,
            lod=lod,
            state=asset_state,
            glb_url=file.uri,
            sha256=file.sha256,
            size_bytes=file.size_bytes,
            generated_at=package.created_at,
            published_at=now if asset_state == AssetState.PUBLISHED else None,
            purge_after=(
                None
                if asset_state == AssetState.PUBLISHED
                else now + timedelta(days=settings.unpublished_model_retention_days)
            ),
        )
        session.add(asset)
        session.flush()
        assets[profile] = asset

    current_manifests = list(
        session.execute(
            select(ManifestRevision)
            .where(
                ManifestRevision.incident_id == incident.id,
                ManifestRevision.is_current.is_(True),
            )
            .with_for_update()
        ).scalars()
    )
    for manifest in current_manifests:
        manifest.is_current = False
    revision_number = (
        int(
            session.execute(
                select(func.max(ManifestRevision.revision)).where(
                    ManifestRevision.incident_id == incident.id
                )
            ).scalar_one()
            or 0
        )
        + 1
    )
    primary_asset = assets[primary_profile] if primary_profile is not None else None
    manifest = ManifestRevision(
        incident_id=incident.id,
        episode_id=episode.id,
        asset_id=primary_asset.id if primary_asset is not None else None,
        spatial_zone_revision_id=package.spatial_zone_revision_id,
        spatial_package_id=package.id,
        revision=revision_number,
        is_current=True,
        reason=payload.reason,
        actor_id=actor.actor_id,
    )
    session.add(manifest)
    incident.version += 1
    session.flush()

    response = AdminIncidentRepresentationAttachResponse(
        fire_id=incident.fire_id,
        episode_id=episode.episode_id,
        package_id=package.package_id,
        manifest_revision=manifest.revision,
        primary_asset_id=primary_asset.asset_id if primary_asset is not None else None,
        model_asset_ids=[assets[key].asset_id for key in sorted(assets)],
        incident_version=incident.version,
        trace_id=trace_id,
    )
    record_operator_audit(
        session,
        actor=actor,
        action="incident.spatial_package.attached",
        target_type="incident_series",
        target_id=incident.fire_id,
        reason=payload.reason,
        trace_id=trace_id,
        before={
            "incident_version": payload.expected_incident_version,
            "current_manifest_revisions": [item.revision for item in current_manifests],
        },
        after=response.model_dump(mode="json"),
        payload={
            "profiles": sorted(assets),
            "primary_profile": primary_profile,
            "scene_kind": (
                "omniverse_usd"
                if omniverse_scene
                else "tiled"
                if tiled_scene
                else "single_asset"
            ),
        },
    )
    return response


def attach_incident_package(
    session: Session,
    *,
    fire_id: str,
    payload: AdminIncidentRepresentationAttachRequest,
    idempotency_key: str,
    actor: Actor,
    trace_id: str,
    settings: Settings,
) -> RepresentationAttachmentOutcome:
    endpoint = f"POST /api/v2/admin/incidents/{fire_id}/representations"
    request_hash = sha256_hex(
        {
            "actor_id": actor.actor_id,
            "fire_id": fire_id,
            "payload": payload.model_dump(mode="json"),
        }
    )
    begin_write_transaction(session)
    replay = find_replay(
        session,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
    )
    if replay:
        session.rollback()
        return RepresentationAttachmentOutcome(
            AdminIncidentRepresentationAttachResponse.model_validate(replay.response_body),
            True,
        )

    incident = session.execute(
        select(IncidentSeries)
        .where(IncidentSeries.fire_id == fire_id)
        .options(selectinload(IncidentSeries.episodes))
        .with_for_update()
    ).scalar_one_or_none()
    if incident is None:
        raise NotFoundError("incident", fire_id)
    package = session.execute(
        select(SpatialPackage)
        .where(SpatialPackage.package_id == payload.package_id)
        .options(
            selectinload(SpatialPackage.files),
            selectinload(SpatialPackage.spatial_zone_revision),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if package is None:
        raise NotFoundError("spatial_package", payload.package_id)
    response = attach_incident_package_in_transaction(
        session,
        incident=incident,
        package=package,
        payload=payload,
        actor=actor,
        trace_id=trace_id,
        settings=settings,
    )
    store_response(
        session,
        endpoint=endpoint,
        idempotency_key=idempotency_key,
        request_hash=request_hash,
        response_status=200,
        response_body=response.model_dump(mode="json"),
        trace_id=trace_id,
        settings=settings,
    )
    session.commit()
    return RepresentationAttachmentOutcome(response, False)
