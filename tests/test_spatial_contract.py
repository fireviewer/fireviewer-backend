from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from pyproj import network
from sqlalchemy.exc import IntegrityError

from fire_viewer.db.models import (
    ManifestRevision,
    ModelAsset,
    SpatialZone,
    SpatialZoneRevision,
    ZoneArchiveSnapshot,
)
from fire_viewer.domain import spatial
from fire_viewer.domain.enums import AssetLod, AssetState, IncidentStatus
from fire_viewer.domain.schemas import ManifestFrame
from fire_viewer.domain.spatial import (
    METERS_PER_UNITY_UNIT,
    RAF20_GRID_SHA256,
    SpatialProfileError,
    derive_raf20_origin,
    ellipsoidal_to_raf20_orthometric,
    enu_to_gltf,
    enu_to_unity,
    enu_to_wgs84,
    gltf_to_enu,
    gltf_to_unity,
    raf20_orthometric_to_ellipsoidal,
    unity_to_enu,
    validate_france_continentale_origin,
    validate_raf20_derivation,
    verify_raf20_grid,
    wgs84_to_enu,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SPATIAL_CONTRACT_ROOT = BACKEND_ROOT / "contracts" / "vendor" / "spatial-v1"


def test_shared_spatial_contract_fixtures_validate_against_draft_2020_12_schema() -> None:
    schema = json.loads((SPATIAL_CONTRACT_ROOT / "spatial-contract.schema.json").read_text())
    validator = Draft202012Validator(schema)

    for fixture_path in sorted((SPATIAL_CONTRACT_ROOT / "fixtures").glob("*.json")):
        payload = json.loads(fixture_path.read_text())
        errors = list(validator.iter_errors(payload))
        assert not errors, f"{fixture_path.name}: {errors[0].message}"


def test_shared_enu_unity_points_exercise_the_canonical_transform_functions() -> None:
    fixture = json.loads((SPATIAL_CONTRACT_ROOT / "fixtures" / "enu-unity-points.json").read_text())
    metre_tolerance = fixture["tolerance"]["round_trip_m"]
    unity_tolerance = fixture["tolerance"]["round_trip_unity_units"]

    for point in fixture["points"]:
        enu = tuple(point["enu_m"])
        gltf = tuple(point["gltf_m"])
        unity = tuple(point["unity_units"])
        assert enu_to_gltf(enu) == pytest.approx(gltf, abs=metre_tolerance)
        assert gltf_to_enu(gltf) == pytest.approx(enu, abs=metre_tolerance)
        assert gltf_to_unity(gltf) == pytest.approx(unity, abs=unity_tolerance)
        assert unity_to_enu(unity) == pytest.approx(enu, abs=metre_tolerance)


def test_shared_zone_fixtures_are_semantically_consistent_with_raf20_and_snapshot_rules() -> None:
    fixtures_root = SPATIAL_CONTRACT_ROOT / "fixtures"
    registry = json.loads((fixtures_root / "zone-registry.json").read_text())
    revision = json.loads((fixtures_root / "zone-revision.json").read_text())
    snapshot = json.loads((fixtures_root / "spatial-snapshot.json").read_text())
    registered_zone = registry["zones"][0]
    revised_zone = revision["zone"]

    assert revised_zone["zone_id"] == registered_zone["zone_id"]
    assert revision["supersedes_zone_revision"] == registered_zone["zone_revision"]
    assert revised_zone["zone_revision"] == registered_zone["zone_revision"] + 1
    assert snapshot["zone"] == {
        "zone_id": revised_zone["zone_id"],
        "zone_revision": revised_zone["zone_revision"],
    }
    assert snapshot["local_frame"] == revised_zone["local_frame"]
    assert snapshot["vertical_conversion"] == revised_zone["vertical_conversion"]
    assert snapshot["archive_png"]["content_type"] == "image/png"
    assert ".glb" not in snapshot["archive_png"]["archive_uri"]

    for zone in (registered_zone, revised_zone):
        conversion = zone["vertical_conversion"]
        result = conversion["result_epsg4979"]
        frame = zone["local_frame"]
        assert result == frame["origin_epsg4979"]
        assert frame["origin_wgs84"] == [
            result["longitude_deg"],
            result["latitude_deg"],
            result["ellipsoidal_height_m"],
        ]
        assert result["ellipsoidal_height_m"] == pytest.approx(
            conversion["source_orthometric_height_m"] + conversion["geoid_undulation_m"],
            abs=0.001,
        )
        assert frame["frame_crs"] == "EPSG:4979"
        assert frame["frame_type"] == "ENU"
        assert frame["unity_units_per_meter"] == 100.0
        assert frame["viewer_manifest_meters_per_unit"] == 0.01
        assert conversion["raf20_grid"]["sha256"] == RAF20_GRID_SHA256
        assert conversion["raf20_grid"]["network_permitted"] is False
        validate_raf20_derivation(
            result["longitude_deg"],
            result["latitude_deg"],
            conversion["source_orthometric_height_m"],
            conversion["geoid_undulation_m"],
            result["ellipsoidal_height_m"],
        )


def test_fv003_assets_are_quarantined_with_full_legacy_provenance_on_upgrade(tmp_path) -> None:
    database_path = tmp_path / "legacy-fv003.db"
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path}")
    command.upgrade(config, "ab7fe6f3a550")

    now = "2026-07-12 08:20:00.000000"
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute(
            "INSERT INTO incident_series (id, fire_id, territory_code, sequence, canonical_name, "
            "reference_lon, reference_lat, horizontal_uncertainty_m, bbox_min_lon, bbox_max_lon, "
            "bbox_min_lat, bbox_max_lat, public_visibility, public_note, version, created_at, "
            "updated_at) VALUES (1, 'FR-83-00999', '83', 999, 'Legacy fixture', 6.0, 43.0, 100, "
            "5.9, 6.1, 42.9, 43.1, 'PUBLIC', NULL, 1, ?, ?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO episode (id, incident_id, episode_id, ordinal, status, review_required, "
            "is_current, confidence_policy, started_at, last_observed_at, validated_at, ended_at, "
            "version, created_at, updated_at) VALUES (1, 1, 'E01', 1, 'MONITORING', 0, 1, "
            "'fixture', ?, ?, NULL, NULL, 1, ?, ?)",
            (now, now, now, now),
        )
        connection.execute(
            "INSERT INTO model_asset (id, asset_id, incident_id, episode_id, version, lod, state, "
            "glb_url, sha256, size_bytes, origin_lon, origin_lat, origin_altitude_m, local_frame, "
            "meters_per_unit, vertical_datum, terrain_source_year, generated_at, published_at, "
            "superseded_at, created_at) VALUES (1, 'legacy-asset', 1, 1, 1, 'DESKTOP', "
            "'PUBLISHED', "
            "'https://example.invalid/legacy.glb', ?, 1, 6.0, 43.0, 412.7, 'ENU', 1.0, 'legacy', "
            "2024, ?, ?, NULL, ?)",
            ("a" * 64, now, now, now),
        )
        connection.commit()

    command.upgrade(config, "head")
    with closing(sqlite3.connect(database_path)) as connection:
        migrated = connection.execute(
            "SELECT state, legacy_incident_id, legacy_episode_id, legacy_origin_lon, "
            "legacy_origin_lat, legacy_origin_altitude_m, legacy_local_frame, "
            "legacy_meters_per_unit, legacy_vertical_datum, spatial_zone_revision_id "
            "FROM model_asset WHERE id = 1"
        ).fetchone()

    assert migrated == ("QUARANTINED", 1, 1, 6.0, 43.0, 412.7, "ENU", 1.0, "legacy", None)
    with pytest.raises(RuntimeError, match="Cannot downgrade FV-004 while model asset rows exist"):
        command.downgrade(config, "base")


def _new_zone_revision(
    session, *, revision: int, max_east_m: float = 2_500.0
) -> SpatialZoneRevision:
    zone = SpatialZone(zone_id="zone-fixture-rural-0001", label="Fictitious reusable rural zone")
    session.add(zone)
    session.flush()
    zone_revision = SpatialZoneRevision(
        spatial_zone_id=zone.id,
        revision=revision,
        origin_lon=6.0214,
        origin_lat=43.2897,
        source_orthometric_height_m=412.7,
        geoid_undulation_m=49.31100405064734,
        origin_ellipsoid_height_m=462.01100405064733,
        min_east_m=-2_500.0,
        max_east_m=max_east_m,
        min_north_m=-2_500.0,
        max_north_m=2_500.0,
        min_up_m=-500.0,
        max_up_m=2_000.0,
    )
    session.add(zone_revision)
    session.flush()
    return zone_revision


def _new_asset(session, spatial_zone_revision: SpatialZoneRevision) -> ModelAsset:
    generated_at = datetime(2026, 7, 12, 8, 20, tzinfo=UTC)
    asset = ModelAsset(
        asset_id="asset-spatial-fixture-0001",
        spatial_zone_revision_id=spatial_zone_revision.id,
        version=1,
        lod=AssetLod.DESKTOP,
        state=AssetState.PUBLISHED,
        glb_url="https://assets.example.invalid/fire-viewer/fixture.glb",
        sha256="a" * 64,
        size_bytes=123_456,
        terrain_source_year=2024,
        generated_at=generated_at,
        published_at=generated_at,
    )
    session.add(asset)
    session.flush()
    return asset


def test_raf20_grid_is_pinned_and_vertical_conversion_has_the_expected_direction() -> None:
    grid_path = verify_raf20_grid()

    assert grid_path.name == "fr_ign_RAF20.tif"
    assert grid_path.read_bytes()
    assert RAF20_GRID_SHA256 == "dc0cc2a38f0ea1029fe72cca3b5b7ed6dfe7e1db2a8d8482b7326ce3d6f25605"

    longitude, latitude, orthometric_height = 6.0214, 43.2897, 412.7
    ellipsoidal = raf20_orthometric_to_ellipsoidal(longitude, latitude, orthometric_height)
    derived = derive_raf20_origin(longitude, latitude, orthometric_height)
    round_trip = ellipsoidal_to_raf20_orthometric(*ellipsoidal)

    assert network.is_network_enabled() is False
    assert 43.0 < ellipsoidal[2] - orthometric_height < 56.0
    assert derived.ellipsoid_height_m == pytest.approx(ellipsoidal[2], abs=0.001)
    validate_raf20_derivation(
        longitude,
        latitude,
        derived.source_orthometric_height_m,
        derived.geoid_undulation_m,
        derived.ellipsoid_height_m,
    )
    with pytest.raises(SpatialProfileError, match="do not match the pinned local grid"):
        validate_raf20_derivation(
            longitude,
            latitude,
            derived.source_orthometric_height_m,
            derived.geoid_undulation_m + 0.01,
            derived.ellipsoid_height_m + 0.01,
        )
    assert round_trip[0] == pytest.approx(longitude, abs=1e-10)
    assert round_trip[1] == pytest.approx(latitude, abs=1e-10)
    assert round_trip[2] == pytest.approx(orthometric_height, abs=0.001)


def test_spatial_profile_rejects_corsica_and_non_finite_origins() -> None:
    with pytest.raises(SpatialProfileError, match="Corsica"):
        validate_france_continentale_origin((9.0, 42.1, 120.0))
    with pytest.raises(SpatialProfileError, match="finite"):
        validate_france_continentale_origin((6.0, 43.0, float("inf")))


def test_raf20_grid_must_be_local_and_match_its_pinned_checksum(tmp_path, monkeypatch) -> None:
    missing_grid = tmp_path / "fr_ign_RAF20.tif"
    monkeypatch.setattr(spatial, "raf20_grid_path", lambda: missing_grid)

    with pytest.raises(SpatialProfileError, match="network grid downloads are disabled"):
        verify_raf20_grid()

    missing_grid.write_bytes(b"not-the-raf20-grid")
    with pytest.raises(SpatialProfileError, match="SHA-256"):
        verify_raf20_grid()


def test_wgs84_enu_unity_and_gltf_axes_round_trip_within_one_centimetre() -> None:
    origin = (6.0214, 43.2897, 462.01100405064733)
    expected_enu = (1_250.0, -875.0, 36.5)
    position = enu_to_wgs84(expected_enu, origin)
    enu = wgs84_to_enu(position, origin)

    assert enu == pytest.approx(expected_enu, abs=0.01)
    assert enu_to_unity(enu) == pytest.approx((125_000.0, 3_650.0, -87_500.0), abs=1e-6)
    assert unity_to_enu((125_000.0, 3_650.0, -87_500.0)) == pytest.approx(expected_enu)
    assert METERS_PER_UNITY_UNIT == 0.01
    assert gltf_to_unity((1_250.0, 36.5, 875.0)) == (125_000.0, 3_650.0, -87_500.0)


def test_manifest_frame_accepts_only_the_canonical_spatial_profile() -> None:
    payload = {
        "origin_wgs84": [6.0214, 43.2897, 462.01100405064733],
        "local_frame": "ENU",
        "meters_per_unit": 0.01,
        "vertical_datum": "EPSG:4979",
    }

    assert ManifestFrame.model_validate(payload).model_dump(mode="json") == payload
    for field, invalid_value in (
        ("meters_per_unit", 1.0),
        ("meters_per_unit", 100.0),
        ("vertical_datum", "NGF-IGN69"),
    ):
        invalid = dict(payload)
        invalid[field] = invalid_value
        with pytest.raises(ValidationError):
            ManifestFrame.model_validate(invalid)

    invalid_origin = dict(payload)
    invalid_origin["origin_wgs84"] = [float("inf"), 43.2897, 462.0]
    with pytest.raises(ValidationError):
        ManifestFrame.model_validate(invalid_origin)


def test_database_rejects_non_finite_bounds_and_invalid_vertical_arithmetic(session) -> None:
    zone = SpatialZone(zone_id="zone-constraint-fixture-0001")
    session.add(zone)
    session.commit()
    invalid_derivation = SpatialZoneRevision(
        spatial_zone_id=zone.id,
        revision=1,
        origin_lon=6.0214,
        origin_lat=43.2897,
        source_orthometric_height_m=412.7,
        geoid_undulation_m=49.31100405064734,
        origin_ellipsoid_height_m=462.5,
        min_east_m=-2_500.0,
        max_east_m=2_500.0,
        min_north_m=-2_500.0,
        max_north_m=2_500.0,
        min_up_m=-500.0,
        max_up_m=2_000.0,
    )
    session.add(invalid_derivation)
    with pytest.raises(IntegrityError, match="ck_spatial_zone_vertical_derivation"):
        session.commit()
    session.rollback()

    infinite_bound = SpatialZoneRevision(
        spatial_zone_id=zone.id,
        revision=1,
        origin_lon=6.0214,
        origin_lat=43.2897,
        source_orthometric_height_m=412.7,
        geoid_undulation_m=49.31100405064734,
        origin_ellipsoid_height_m=462.01100405064733,
        min_east_m=-2_500.0,
        max_east_m=float("inf"),
        min_north_m=-2_500.0,
        max_north_m=2_500.0,
        min_up_m=-500.0,
        max_up_m=2_000.0,
    )
    session.add(infinite_bound)
    with pytest.raises(IntegrityError, match="ck_spatial_zone_bounds_finite"):
        session.commit()
    session.rollback()


def test_public_manifest_fails_closed_when_zone_h_n_h_values_disagree_with_raf20(
    client, session, seed_incident
) -> None:
    incident, episode = seed_incident(fire_id="FR-83-00100", sequence=100, lon=6.0214, lat=43.2897)
    zone = SpatialZone(zone_id="zone-grid-validation-fixture-0001")
    session.add(zone)
    session.flush()
    mismatched_grid_revision = SpatialZoneRevision(
        spatial_zone_id=zone.id,
        revision=1,
        origin_lon=6.0214,
        origin_lat=43.2897,
        source_orthometric_height_m=412.7,
        geoid_undulation_m=50.0,
        origin_ellipsoid_height_m=462.7,
        min_east_m=-2_500.0,
        max_east_m=2_500.0,
        min_north_m=-2_500.0,
        max_north_m=2_500.0,
        min_up_m=-500.0,
        max_up_m=2_000.0,
    )
    session.add(mismatched_grid_revision)
    session.flush()
    asset = _new_asset(session, mismatched_grid_revision)
    session.add(
        ManifestRevision(
            incident_id=incident.id,
            episode_id=episode.id,
            asset_id=asset.id,
            spatial_zone_revision_id=mismatched_grid_revision.id,
            revision=1,
            is_current=True,
            reason="Fictitious invalid RAF20 provenance fixture.",
            actor_id="spatial-contract-test",
        )
    )
    session.commit()

    manifest = client.get(f"/api/v1/incident/{incident.fire_id}/manifest")

    assert manifest.status_code == 200
    assert manifest.json()["model_state"] == "not_available"
    assert manifest.json()["asset"] is None
    assert manifest.json()["frame"] is None


def test_zones_are_reusable_revisions_are_immutable_and_archives_are_png_snapshots(
    client, session, seed_incident
) -> None:
    first_incident, first_episode = seed_incident(
        fire_id="FR-83-00101",
        sequence=101,
        lon=6.0214,
        lat=43.2897,
        status=IncidentStatus.CLOSED,
    )
    second_incident, second_episode = seed_incident(
        fire_id="FR-83-00102",
        sequence=102,
        lon=6.0214,
        lat=43.2897,
        status=IncidentStatus.CLOSED,
    )
    zone_revision = _new_zone_revision(session, revision=1)
    asset = _new_asset(session, zone_revision)
    first_manifest = ManifestRevision(
        incident_id=first_incident.id,
        episode_id=first_episode.id,
        asset_id=asset.id,
        spatial_zone_revision_id=zone_revision.id,
        revision=1,
        is_current=True,
        reason="Fictitious first incident publication.",
        actor_id="spatial-contract-test",
    )
    second_manifest = ManifestRevision(
        incident_id=second_incident.id,
        episode_id=second_episode.id,
        asset_id=asset.id,
        spatial_zone_revision_id=zone_revision.id,
        revision=1,
        is_current=True,
        reason="Fictitious reused-zone publication.",
        actor_id="spatial-contract-test",
    )
    session.add_all([first_manifest, second_manifest])
    session.commit()

    assert first_manifest.asset_id == second_manifest.asset_id == asset.id
    assert first_manifest.spatial_zone_revision_id == second_manifest.spatial_zone_revision_id

    cross_incident_episode = ManifestRevision(
        incident_id=first_incident.id,
        episode_id=second_episode.id,
        asset_id=asset.id,
        spatial_zone_revision_id=zone_revision.id,
        revision=2,
        is_current=False,
        reason="A manifest must not cross incident episode boundaries.",
        actor_id="spatial-contract-test",
    )
    session.add(cross_incident_episode)
    with pytest.raises(IntegrityError, match="manifest episode must belong to its incident"):
        session.commit()
    session.rollback()

    extended_revision = SpatialZoneRevision(
        spatial_zone_id=zone_revision.spatial_zone_id,
        revision=2,
        origin_lon=zone_revision.origin_lon,
        origin_lat=zone_revision.origin_lat,
        source_orthometric_height_m=zone_revision.source_orthometric_height_m,
        geoid_undulation_m=zone_revision.geoid_undulation_m,
        origin_ellipsoid_height_m=zone_revision.origin_ellipsoid_height_m,
        min_east_m=-2_500.0,
        max_east_m=5_000.0,
        min_north_m=-2_500.0,
        max_north_m=2_500.0,
        min_up_m=-500.0,
        max_up_m=2_000.0,
    )
    session.add(extended_revision)
    session.commit()
    assert extended_revision.id != zone_revision.id

    zone_revision.max_east_m = 3_000.0
    with pytest.raises(IntegrityError, match="spatial zone revisions are immutable"):
        session.commit()
    session.rollback()

    first_manifest.spatial_zone_revision_id = extended_revision.id
    with pytest.raises(
        IntegrityError, match="manifest asset and spatial zone revision are immutable"
    ):
        session.commit()
    session.rollback()

    mismatched_manifest = ManifestRevision(
        incident_id=first_incident.id,
        episode_id=first_episode.id,
        asset_id=asset.id,
        spatial_zone_revision_id=extended_revision.id,
        revision=2,
        is_current=False,
        reason="This mismatch must be rejected.",
        actor_id="spatial-contract-test",
    )
    session.add(mismatched_manifest)
    with pytest.raises(IntegrityError, match="manifest asset and spatial zone revision must match"):
        session.commit()
    session.rollback()

    archive = ZoneArchiveSnapshot(
        archive_id="archive-spatial-fixture-0001",
        incident_id=first_incident.id,
        manifest_revision_id=first_manifest.id,
        asset_id=asset.id,
        spatial_zone_revision_id=zone_revision.id,
        image_url="https://archives.example.invalid/fire-viewer/FR-83-00101-zone.png",
        sha256="b" * 64,
        asset_sha256=asset.sha256,
        render_profile="unity-eun-100-v1/png-capture-v1",
        rendered_at=datetime(2026, 7, 12, 9, 0, tzinfo=UTC),
    )
    session.add(archive)
    session.commit()
    assert archive.media_type == "image/png"
    assert not hasattr(archive, "glb_url")
    archived_manifest = client.get(f"/api/v1/incident/{first_incident.fire_id}/manifest")
    assert archived_manifest.status_code == 200
    assert archived_manifest.json()["model_state"] == "not_available"
    assert archived_manifest.json()["asset"] is None
    assert archived_manifest.json()["frame"] is None
    assert "image_url" not in archived_manifest.json()
    assert "archived incident" in archived_manifest.json()["public_notice"]

    archive.image_url = "https://archives.example.invalid/fire-viewer/rewritten.png"
    with pytest.raises(IntegrityError, match="zone archive snapshots are immutable"):
        session.commit()
    session.rollback()

    invalid_archive = ZoneArchiveSnapshot(
        archive_id="archive-spatial-fixture-0002",
        incident_id=second_incident.id,
        manifest_revision_id=second_manifest.id,
        asset_id=asset.id,
        spatial_zone_revision_id=extended_revision.id,
        image_url="https://archives.example.invalid/fire-viewer/FR-83-00102-zone.png",
        sha256="c" * 64,
        asset_sha256=asset.sha256,
        render_profile="unity-eun-100-v1/png-capture-v1",
        rendered_at=datetime(2026, 7, 12, 9, 0, tzinfo=UTC),
    )
    session.add(invalid_archive)
    with pytest.raises(IntegrityError, match="archive snapshot must match"):
        session.commit()
    session.rollback()

    third_incident, third_episode = seed_incident(
        fire_id="FR-83-00103",
        sequence=103,
        lon=6.0214,
        lat=43.2897,
        status=IncidentStatus.CLOSED,
    )
    third_manifest = ManifestRevision(
        incident_id=third_incident.id,
        episode_id=third_episode.id,
        asset_id=asset.id,
        spatial_zone_revision_id=zone_revision.id,
        revision=1,
        is_current=True,
        reason="Fictitious publication for archive media validation.",
        actor_id="spatial-contract-test",
    )
    session.add(third_manifest)
    session.commit()
    invalid_glb_archive = ZoneArchiveSnapshot(
        archive_id="archive-spatial-fixture-0003",
        incident_id=third_incident.id,
        manifest_revision_id=third_manifest.id,
        asset_id=asset.id,
        spatial_zone_revision_id=zone_revision.id,
        image_url="https://archives.example.invalid/fire-viewer/not-an-archive.glb",
        sha256="d" * 64,
        asset_sha256=asset.sha256,
        render_profile="unity-eun-100-v1/png-capture-v1",
        rendered_at=datetime(2026, 7, 12, 9, 0, tzinfo=UTC),
    )
    session.add(invalid_glb_archive)
    with pytest.raises(IntegrityError, match=r"ck_zone_archive_(not_glb|png_url)"):
        session.commit()
    session.rollback()

    invalid_digest_archive = ZoneArchiveSnapshot(
        archive_id="archive-spatial-fixture-0003a",
        incident_id=third_incident.id,
        manifest_revision_id=third_manifest.id,
        asset_id=asset.id,
        spatial_zone_revision_id=zone_revision.id,
        image_url="https://archives.example.invalid/fire-viewer/FR-83-00103-zone.png",
        sha256="z" * 64,
        asset_sha256=asset.sha256,
        render_profile="unity-eun-100-v1/png-capture-v1",
        rendered_at=datetime(2026, 7, 12, 9, 0, tzinfo=UTC),
    )
    session.add(invalid_digest_archive)
    with pytest.raises(IntegrityError, match="ck_zone_archive_sha256"):
        session.commit()
    session.rollback()

    active_incident, active_episode = seed_incident(
        fire_id="FR-83-00104", sequence=104, lon=6.0214, lat=43.2897
    )
    active_manifest = ManifestRevision(
        incident_id=active_incident.id,
        episode_id=active_episode.id,
        asset_id=asset.id,
        spatial_zone_revision_id=zone_revision.id,
        revision=1,
        is_current=True,
        reason="Fictitious active publication cannot be archived yet.",
        actor_id="spatial-contract-test",
    )
    session.add(active_manifest)
    session.commit()
    active_archive = ZoneArchiveSnapshot(
        archive_id="archive-spatial-fixture-0004",
        incident_id=active_incident.id,
        manifest_revision_id=active_manifest.id,
        asset_id=asset.id,
        spatial_zone_revision_id=zone_revision.id,
        image_url="https://archives.example.invalid/fire-viewer/FR-83-00104-zone.png",
        sha256="e" * 64,
        asset_sha256=asset.sha256,
        render_profile="unity-eun-100-v1/png-capture-v1",
        rendered_at=datetime(2026, 7, 12, 9, 0, tzinfo=UTC),
    )
    session.add(active_archive)
    with pytest.raises(IntegrityError, match="requires a CLOSED episode"):
        session.commit()
    session.rollback()
