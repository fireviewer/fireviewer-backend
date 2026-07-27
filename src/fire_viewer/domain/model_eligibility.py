"""Pure eligibility policy for requesting external terrain-model production."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

EligibilityReason = Literal["area_threshold", "established_evacuation"]


@dataclass(frozen=True, slots=True)
class ModelGenerationEligibility:
    eligible: bool
    reasons: tuple[EligibilityReason, ...]


def evaluate_model_generation_eligibility(
    *,
    estimated_area_ha: float | None,
    evacuation_established: bool,
    area_threshold_ha: float,
) -> ModelGenerationEligibility:
    reasons: list[EligibilityReason] = []
    if estimated_area_ha is not None and estimated_area_ha >= area_threshold_ha:
        reasons.append("area_threshold")
    if evacuation_established:
        reasons.append("established_evacuation")
    return ModelGenerationEligibility(eligible=bool(reasons), reasons=tuple(reasons))
