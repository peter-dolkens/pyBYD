"""Tests for pybyd._validators – GPS coordinate guard & realtime filters."""

from __future__ import annotations

import pytest

from pybyd._validators import (
    _has_valid_coordinates,
    apply_gps_filters,
    apply_realtime_filters,
    guard_gps_coordinates,
    keep_previous_when_zero,
)
from pybyd.models.gps import GpsInfo
from pybyd.models.realtime import VehicleRealtimeData

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _gps(
    lat: float | None = None,
    lon: float | None = None,
    speed: float | None = None,
) -> GpsInfo:
    return GpsInfo(latitude=lat, longitude=lon, speed=speed)


VALID = _gps(lat=48.8566, lon=2.3522, speed=60.0)  # Paris
VALID_B = _gps(lat=31.2304, lon=121.4737, speed=30.0)  # Shanghai
NONE_NONE = _gps()  # lat=None, lon=None
NULL_ISLAND = _gps(lat=0.001, lon=0.002)
PARTIAL_LAT = _gps(lat=48.8566, lon=None)
PARTIAL_LON = _gps(lat=None, lon=2.3522)


# ---------------------------------------------------------------------------
# _has_valid_coordinates
# ---------------------------------------------------------------------------


class TestHasValidCoordinates:
    """Unit tests for the _has_valid_coordinates helper."""

    def test_valid_coordinates(self) -> None:
        assert _has_valid_coordinates(VALID) is True

    def test_both_none(self) -> None:
        assert _has_valid_coordinates(NONE_NONE) is False

    def test_null_island(self) -> None:
        assert _has_valid_coordinates(NULL_ISLAND) is False

    def test_partial_lat_only(self) -> None:
        assert _has_valid_coordinates(PARTIAL_LAT) is False

    def test_partial_lon_only(self) -> None:
        assert _has_valid_coordinates(PARTIAL_LON) is False

    def test_exactly_at_threshold_boundary(self) -> None:
        """Coordinates at exactly the threshold edge are valid."""
        at_edge = _gps(lat=0.1, lon=0.1)
        assert _has_valid_coordinates(at_edge) is True

    def test_negative_valid(self) -> None:
        """Negative coordinates (Southern/Western hemispheres) are valid."""
        neg = _gps(lat=-33.8688, lon=151.2093)  # Sydney
        assert _has_valid_coordinates(neg) is True

    def test_one_coord_zero_other_large(self) -> None:
        """lat≈0 with large lon is fine (e.g. equatorial Africa)."""
        equatorial = _gps(lat=0.05, lon=32.5)
        assert _has_valid_coordinates(equatorial) is True

    def test_zero_zero_exact(self) -> None:
        """Exact (0, 0) is Null Island – invalid."""
        assert _has_valid_coordinates(_gps(lat=0.0, lon=0.0)) is False


# ---------------------------------------------------------------------------
# guard_gps_coordinates – first poll (previous=None)
# ---------------------------------------------------------------------------


class TestGuardGpsFirstPoll:
    """First poll: previous is None."""

    def test_valid_incoming_accepted(self) -> None:
        result = guard_gps_coordinates(None, VALID)
        assert result is VALID

    def test_none_incoming_returns_none(self) -> None:
        result = guard_gps_coordinates(None, None)
        assert result is None

    def test_both_none_coords_returns_none(self) -> None:
        result = guard_gps_coordinates(None, NONE_NONE)
        assert result is None

    def test_null_island_returns_none(self) -> None:
        result = guard_gps_coordinates(None, NULL_ISLAND)
        assert result is None

    def test_partial_lat_returns_none(self) -> None:
        result = guard_gps_coordinates(None, PARTIAL_LAT)
        assert result is None

    def test_partial_lon_returns_none(self) -> None:
        result = guard_gps_coordinates(None, PARTIAL_LON)
        assert result is None


# ---------------------------------------------------------------------------
# guard_gps_coordinates – subsequent polls (previous is set)
# ---------------------------------------------------------------------------


class TestGuardGpsSubsequentPoll:
    """Subsequent polls: previous has valid data."""

    def test_valid_incoming_replaces_previous(self) -> None:
        result = guard_gps_coordinates(VALID, VALID_B)
        assert result is VALID_B

    def test_none_incoming_keeps_previous(self) -> None:
        result = guard_gps_coordinates(VALID, None)
        assert result is VALID

    def test_both_none_coords_keeps_previous(self) -> None:
        result = guard_gps_coordinates(VALID, NONE_NONE)
        assert result is VALID

    def test_null_island_keeps_previous(self) -> None:
        result = guard_gps_coordinates(VALID, NULL_ISLAND)
        assert result is VALID

    def test_partial_lat_keeps_previous(self) -> None:
        result = guard_gps_coordinates(VALID, PARTIAL_LAT)
        assert result is VALID

    def test_partial_lon_keeps_previous(self) -> None:
        result = guard_gps_coordinates(VALID, PARTIAL_LON)
        assert result is VALID


# ---------------------------------------------------------------------------
# apply_gps_filters (thin wrapper – ensure delegation)
# ---------------------------------------------------------------------------


class TestApplyGpsFilters:
    """apply_gps_filters delegates to guard_gps_coordinates."""

    def test_delegates_valid(self) -> None:
        assert apply_gps_filters(None, VALID) is VALID

    def test_delegates_invalid_first_poll(self) -> None:
        assert apply_gps_filters(None, NONE_NONE) is None

    def test_delegates_preserves_previous(self) -> None:
        assert apply_gps_filters(VALID, NONE_NONE) is VALID


# ---------------------------------------------------------------------------
# keep_previous_when_zero (SOC guard)
# ---------------------------------------------------------------------------


class TestKeepPreviousWhenZero:
    """SOC zero-spike guard."""

    def test_nonzero_incoming_accepted(self) -> None:
        assert keep_previous_when_zero(50.0, 48.0) == 48.0

    def test_zero_incoming_with_previous_keeps_previous(self) -> None:
        assert keep_previous_when_zero(50.0, 0) == 50.0

    def test_zero_incoming_without_previous_returns_zero(self) -> None:
        assert keep_previous_when_zero(None, 0) == 0

    def test_none_incoming_returns_none(self) -> None:
        assert keep_previous_when_zero(50.0, None) is None


class TestApplyRealtimeZeroSmoothing:
    """Zero-value smoothing for selected realtime telemetry fields."""

    @pytest.mark.parametrize(
        "field_name,previous_value",
        [
            ("left_front_tire_pressure", 2.4),
            ("right_front_tire_pressure", 2.5),
            ("left_rear_tire_pressure", 2.6),
            ("right_rear_tire_pressure", 2.7),
            ("endurance_mileage", 320.0),
            ("ev_endurance", 310.0),
            ("endurance_mileage_v2", 305.0),
            ("oil_endurance", 420.0),
        ],
    )
    def test_zero_incoming_with_previous_keeps_previous(self, field_name: str, previous_value: float) -> None:
        previous = VehicleRealtimeData.model_validate({field_name: previous_value})
        incoming = VehicleRealtimeData.model_validate({field_name: 0})

        filtered = apply_realtime_filters(previous, incoming)

        assert getattr(filtered, field_name) == previous_value

    @pytest.mark.parametrize(
        "field_name",
        [
            "left_front_tire_pressure",
            "right_front_tire_pressure",
            "left_rear_tire_pressure",
            "right_rear_tire_pressure",
            "endurance_mileage",
            "ev_endurance",
            "endurance_mileage_v2",
            "oil_endurance",
        ],
    )
    def test_zero_incoming_without_previous_kept(self, field_name: str) -> None:
        incoming = VehicleRealtimeData.model_validate({field_name: 0})

        filtered = apply_realtime_filters(None, incoming)

        assert getattr(filtered, field_name) == 0

    @pytest.mark.parametrize(
        "field_name,previous_value,incoming_value",
        [
            ("left_front_tire_pressure", 2.4, 2.3),
            ("right_front_tire_pressure", 2.5, 2.4),
            ("left_rear_tire_pressure", 2.6, 2.5),
            ("right_rear_tire_pressure", 2.7, 2.6),
            ("endurance_mileage", 320.0, 315.0),
            ("ev_endurance", 310.0, 300.0),
            ("endurance_mileage_v2", 305.0, 299.0),
            ("oil_endurance", 420.0, 410.0),
        ],
    )
    def test_nonzero_incoming_replaces_previous(
        self,
        field_name: str,
        previous_value: float,
        incoming_value: float,
    ) -> None:
        previous = VehicleRealtimeData.model_validate({field_name: previous_value})
        incoming = VehicleRealtimeData.model_validate({field_name: incoming_value})

        filtered = apply_realtime_filters(previous, incoming)

        assert getattr(filtered, field_name) == incoming_value
