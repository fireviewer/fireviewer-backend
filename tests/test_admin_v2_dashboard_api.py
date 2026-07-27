from __future__ import annotations

from datetime import UTC, datetime

from fire_viewer.db.models import (
    IncidentPublicReport,
    Job,
    SpatialPackage,
    SpatialZone,
    SpatialZoneRevision,
)
from fire_viewer.domain.enums import (
    IncidentStatus,
    JobKind,
    JobState,
    PublicReportCategory,
    PublicReportState,
    SpatialPackageState,
)
from fire_viewer.domain.spatial import derive_raf20_origin


def test_dashboard_projects_persisted_priorities(client, session, seed_incident) -> None:
    incident, episode = seed_incident(
        fire_id="FR-83-00301",
        sequence=301,
        lon=6.0214,
        lat=43.2897,
        canonical_name="Massif des Maures",
        status=IncidentStatus.ACTIVE_CONFIRMED,
    )
    episode.review_required = True
    now = datetime(2026, 7, 15, 16, 42, tzinfo=UTC)
    session.add(
        IncidentPublicReport(
            report_id="REPORT-PRIVACY-301",
            incident_id=incident.id,
            category=PublicReportCategory.PRIVACY,
            message="Une donnée personnelle apparaît dans la fiche publique.",
            origin_fingerprint="a" * 64,
            content_hash="b" * 64,
            submitted_day="2026-07-15",
            state=PublicReportState.PENDING,
            submitted_at=now,
            version=1,
        )
    )
    session.add(
        Job(
            job_id="JOB-QUARANTINED-301",
            kind=JobKind.ASSET_PUBLICATION,
            state=JobState.QUARANTINED,
            incident_id=incident.id,
            episode_id=episode.id,
            input_hash="c" * 64,
            input_payload={"package_id": "PKG-DASHBOARD-301"},
            output_payload={},
            attempt=1,
            max_attempts=3,
            last_error="Le checksum du résultat ne correspond pas.",
            trace_id="trace-dashboard-301",
            idempotency_key="dashboard-job-301",
        )
    )
    zone = SpatialZone(zone_id="ZONE-DASHBOARD", label="Zone du tableau de bord")
    session.add(zone)
    session.flush()
    origin = derive_raf20_origin(6.0214, 43.2897, 400.0)
    revision = SpatialZoneRevision(
        spatial_zone_id=zone.id,
        revision=1,
        origin_lon=6.0214,
        origin_lat=43.2897,
        source_orthometric_height_m=origin.source_orthometric_height_m,
        geoid_undulation_m=origin.geoid_undulation_m,
        origin_ellipsoid_height_m=origin.ellipsoid_height_m,
        min_east_m=-1_000.0,
        max_east_m=1_000.0,
        min_north_m=-1_000.0,
        max_north_m=1_000.0,
        min_up_m=-100.0,
        max_up_m=1_000.0,
    )
    session.add(revision)
    session.flush()
    package = SpatialPackage(
        package_id="PKG-DASHBOARD-301",
        manifest_uri="local://dashboard/package-manifest.json",
        manifest_sha256="d" * 64,
        manifest_size_bytes=128,
        storage_uri="local://dashboard",
        state=SpatialPackageState.DRAFT,
        provenance={"zone_id": "ZONE-DASHBOARD"},
        verification_report={"status": "imported"},
        created_by="dashboard-test",
        created_at=now,
    )
    session.add(package)
    session.flush()
    package.state = SpatialPackageState.VERIFIED
    package.verification_report = {"status": "passed"}
    package.verified_at = now
    session.flush()
    package.spatial_zone_revision_id = revision.id
    session.flush()
    package.state = SpatialPackageState.PREVIEWABLE
    session.flush()
    session.commit()

    response = client.get("/api/v2/admin/dashboard")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    body = response.json()
    assert body["queue"] == {
        "total": 4,
        "critical": 2,
        "high": 2,
        "medium": 0,
        "observations_pending": 0,
        "reports_pending": 1,
        "incidents_requiring_review": 1,
        "jobs_quarantined": 1,
        "models_to_review": 1,
    }
    assert {item["kind"] for item in body["priorities"]} == {
        "incident",
        "job",
        "model_package",
        "report",
    }
    privacy = next(item for item in body["priorities"] if item["kind"] == "report")
    assert privacy["priority"] == "critical"
    assert privacy["fire_id"] == incident.fire_id
    assert body["watchlist"][0]["fire_id"] == incident.fire_id
    assert body["map_summary"]["active_incidents"] == 1
    assert body["system"]["database"]["reachable"] is True
    assert "local://" not in response.text


def test_versioned_admin_read_routes_share_the_same_persisted_state(client, seed_incident) -> None:
    incident, _ = seed_incident(
        fire_id="FR-83-00302",
        sequence=302,
        lon=6.1214,
        lat=43.3897,
        status=IncidentStatus.UNDER_REVIEW,
    )

    incidents = client.get("/api/v2/admin/incidents")
    queue = client.get("/api/v2/admin/work-queue?category=incident&limit=1")

    assert incidents.status_code == 200
    assert incidents.headers["Cache-Control"] == "no-store"
    assert incidents.json()["incidents"][0]["fire_id"] == incident.fire_id
    assert queue.status_code == 200
    assert queue.headers["Cache-Control"] == "no-store"
    assert queue.json()["incidents"][0]["fire_id"] == incident.fire_id
    assert queue.json()["generated_at"] is not None
    assert queue.json()["summary"]["total"] >= 1
    assert queue.json()["summary"]["by_category"]["incident"] == 1
    assert queue.json()["page"] == {
        "limit": 1,
        "returned": 1,
        "next_cursor": None,
        "total_filtered": 1,
    }
    decision = next(item for item in queue.json()["items"] if item["category"] == "incident")
    assert decision["fire_id"] == incident.fire_id
    assert "payload" not in decision
