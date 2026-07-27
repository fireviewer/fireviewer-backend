from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from fire_viewer.domain.agent_schemas import (
    AgentBatchCreateRequestV2,
    ReportSectionV2,
    SpatialProposalV2,
    WorkerInputV2,
    WorkerOutputV2,
)

EXAMPLES = (
    Path(__file__).resolve().parents[1]
    / "contracts"
    / "vendor"
    / "agent-worker"
    / "v2"
    / "examples"
)


def _example(name: str) -> dict[str, object]:
    value = json.loads((EXAMPLES / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_worker_v2_shared_examples_are_valid() -> None:
    worker_input = WorkerInputV2.model_validate(_example("valid-input.json"))
    worker_output = WorkerOutputV2.model_validate(_example("valid-output.json"))

    assert worker_input.schema_version == "2.0"
    assert worker_input.analysis_window.fire_id == "FR-99-00001"
    assert worker_output.items[0].spatial_proposals[0].geometry_origin == "CAMERA_RAYCAST"
    assert worker_output.report_draft is not None
    assert worker_output.report_draft.sections[0].fact_ids == ["FACT-1"]


def test_agent_batch_v2_preserves_consent_before_worker_dispatch() -> None:
    worker_payload = _example("valid-input.json")
    item = deepcopy(worker_payload["items"][0])
    assert isinstance(item, dict)
    item.update(
        {
            "media_sha256": "d" * 64,
            "size_bytes": 123_456,
            "consent": {
                "basis": "source_license",
                "scopes": [
                    "temporary_storage",
                    "agent_analysis",
                    "human_review",
                    "retain_evidence",
                ],
                "terms_version": "press-license-v1",
                "evidence_sha256": "e" * 64,
                "source_reference_url": "https://press.example/incendie-die",
                "license_identifier": "authorized-free-press-use",
                "granted_at": "2026-07-09T18:00:00Z",
            },
        }
    )
    payload = {
        **worker_payload,
        "purge_after": "2026-08-09T22:00:00Z",
        "items": [item],
    }

    batch = AgentBatchCreateRequestV2.model_validate(payload)

    assert batch.items[0].consent.basis.value == "source_license"
    assert batch.items[0].media_sha256 == "d" * 64


def test_satellite_media_requires_georeferencing_metadata() -> None:
    payload = _example("valid-input.json")
    payload["batch_type"] = "satellite_media"
    item = payload["items"][0]
    assert isinstance(item, dict)
    item["media_type"] = "satellite_image"
    item.pop("camera")

    with pytest.raises(ValidationError, match="satellite metadata"):
        WorkerInputV2.model_validate(payload)


def test_ground_point_requires_deterministic_geometry_provenance() -> None:
    with pytest.raises(ValidationError, match="sourced coordinates"):
        SpatialProposalV2.model_validate(
            {
                "proposal_id": "SP-INVALID",
                "annotation_id": "ANN-1",
                "status": "ground_point",
                "longitude": 5.382,
                "latitude": 44.759,
            }
        )


def test_insufficient_geometry_cannot_smuggle_coordinates() -> None:
    with pytest.raises(ValidationError, match="cannot contain projected coordinates"):
        SpatialProposalV2.model_validate(
            {
                "proposal_id": "SP-ABSTAIN",
                "annotation_id": "ANN-1",
                "status": "insufficient_geometry",
                "longitude": 5.382,
                "latitude": 44.759,
                "uncertainty_codes": ["camera_pose_missing"],
            }
        )


def test_insufficient_geometry_can_abstain_without_a_fake_annotation() -> None:
    proposal = SpatialProposalV2.model_validate(
        {
            "proposal_id": "SP-NO-ANCHOR",
            "status": "insufficient_geometry",
            "uncertainty_codes": ["anchor_not_visible"],
        }
    )

    assert proposal.annotation_id is None


def test_report_limitations_can_explain_an_abstention_without_inventing_a_fact() -> None:
    section = ReportSectionV2.model_validate(
        {
            "key": "limitations",
            "heading": "Limites",
            "body": "La pose caméra ne permet pas une projection fiable.",
            "basis_codes": ["camera_pose_missing"],
        }
    )

    assert section.fact_ids == []


def test_report_cannot_reference_a_fact_absent_from_worker_output() -> None:
    payload = _example("valid-output.json")
    report = payload["report_draft"]
    assert isinstance(report, dict)
    sections = report["sections"]
    assert isinstance(sections, list)
    assert isinstance(sections[0], dict)
    sections[0]["fact_ids"] = ["FACT-UNKNOWN"]

    with pytest.raises(ValidationError, match="unknown fact"):
        WorkerOutputV2.model_validate(payload)
