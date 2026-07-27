from __future__ import annotations

from datetime import date

import pytest

from fire_viewer.services.activity_zone_quality import compare_activity_zones


def _square(west: float, south: float, east: float, north: float) -> dict[str, object]:
    return {
        "type": "Polygon",
        "coordinates": [
            [[west, south], [east, south], [east, north], [west, north], [west, south]]
        ],
    }


def test_same_day_overlapping_zone_is_advisory_and_coherent() -> None:
    day = date(2026, 7, 9)
    result = compare_activity_zones(
        _square(5.36, 44.74, 5.39, 44.77),
        {"type": "Feature", "properties": {}, "geometry": _square(5.365, 44.745, 5.395, 44.775)},
        predicted_local_date=day,
        official_local_date=day,
    )

    assert result.assessment == "coherent"
    assert result.intersection_over_union > 0.5
    assert result.advisory_only is True


def test_distant_zone_requires_review() -> None:
    day = date(2026, 7, 9)
    result = compare_activity_zones(
        _square(5.36, 44.74, 5.37, 44.75),
        _square(6.36, 45.74, 6.37, 45.75),
        predicted_local_date=day,
        official_local_date=day,
    )

    assert result.assessment == "a_revoir"
    assert result.intersection_over_union == 0


def test_comparison_rejects_temporal_mixing() -> None:
    with pytest.raises(ValueError, match="same local date"):
        compare_activity_zones(
            _square(5.36, 44.74, 5.37, 44.75),
            _square(5.36, 44.74, 5.37, 44.75),
            predicted_local_date=date(2026, 7, 9),
            official_local_date=date(2026, 7, 10),
        )
