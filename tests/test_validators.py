"""Tests for value-quality validators."""

from __future__ import annotations

import pytest

from pybyd._validators import guard_gps_coordinates, keep_previous_when_zero
from pybyd.models.gps import GpsInfo

# ------------------------------------------------------------------
# guard_gps_coordinates
# ------------------------------------------------------------------


class TestGuardGpsCoordinates:
    """Tests for the GPS Null Island / None coordinate guard."""

    def _make_gps(self, lat: float | None = None, lon: float | None = None) -> GpsInfo:
        return GpsInfo.model_validate({"latitude": lat, "longitude": lon})

    def test_incoming_none_returns_previous(self) -> None:
        prev = self._make_gps(48.0, 11.0)
        assert guard_gps_coordinates(prev, None) is prev

    def test_previous_none_returns_incoming(self) -> None:
        incoming = self._make_gps(48.0, 11.0)
        assert guard_gps_coordinates(None, incoming) is incoming

    def test_both_none_returns_none(self) -> None:
        assert guard_gps_coordinates(None, None) is None

    def test_valid_coordinates_accepted(self) -> None:
        prev = self._make_gps(48.0, 11.0)
        incoming = self._make_gps(49.0, 12.0)
        assert guard_gps_coordinates(prev, incoming) is incoming

    def test_null_island_rejected(self) -> None:
        prev = self._make_gps(48.0, 11.0)
        incoming = self._make_gps(0.0, 0.0)
        assert guard_gps_coordinates(prev, incoming) is prev

    def test_near_null_island_rejected(self) -> None:
        prev = self._make_gps(48.0, 11.0)
        incoming = self._make_gps(0.05, 0.05)
        assert guard_gps_coordinates(prev, incoming) is prev

    def test_none_coordinates_rejected(self) -> None:
        prev = self._make_gps(48.0, 11.0)
        incoming = self._make_gps(None, None)
        assert guard_gps_coordinates(prev, incoming) is prev

    def test_only_lat_none_accepted(self) -> None:
        """Only lat=None but lon present — not Null Island, accept."""
        prev = self._make_gps(48.0, 11.0)
        incoming = self._make_gps(None, 12.0)
        assert guard_gps_coordinates(prev, incoming) is incoming

    def test_first_startup_null_island_accepted(self) -> None:
        """On first startup (no previous), always accept incoming."""
        incoming = self._make_gps(0.0, 0.0)
        assert guard_gps_coordinates(None, incoming) is incoming


# ------------------------------------------------------------------
# keep_previous_when_zero
# ------------------------------------------------------------------


class TestKeepPreviousWhenZero:
    """Tests for the zero-SOC spike guard."""

    def test_zero_incoming_with_previous_returns_previous(self) -> None:
        assert keep_previous_when_zero(85.0, 0.0) == 85.0

    def test_zero_incoming_no_previous_returns_zero(self) -> None:
        assert keep_previous_when_zero(None, 0.0) == 0.0

    def test_nonzero_incoming_accepted(self) -> None:
        assert keep_previous_when_zero(85.0, 42.0) == 42.0

    def test_none_incoming_accepted(self) -> None:
        assert keep_previous_when_zero(85.0, None) is None

    def test_both_none(self) -> None:
        assert keep_previous_when_zero(None, None) is None

    @pytest.mark.parametrize("value", [1.0, 50.0, 100.0])
    def test_nonzero_values_pass_through(self, value: float) -> None:
        assert keep_previous_when_zero(85.0, value) == value
