"""Tests for capability classes — projection specs and command dispatch."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from pybyd._capabilities.battery_heat import BatteryHeatCapability
from pybyd._capabilities.finder import FinderCapability
from pybyd._capabilities.hvac import HvacCapability
from pybyd._capabilities.lock import LockCapability
from pybyd._capabilities.seat import SeatCapability, SeatLevel, SeatPosition
from pybyd._capabilities.steering import SteeringCapability
from pybyd._capabilities.windows import WindowsCapability
from pybyd._state_engine import ProjectionSpec, VehicleSnapshot, VehicleStateEngine
from pybyd.models.hvac import HvacOverallStatus, HvacStatus
from pybyd.models.realtime import LockState, SeatHeatVentState, StearingWheelHeat, VehicleRealtimeData
from pybyd.models.vehicle import Vehicle

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


@pytest.fixture()
def vehicle() -> Vehicle:
    return Vehicle.model_validate({"vin": "TEST123", "modelName": "Seal"})


@pytest.fixture()
def engine(vehicle: Vehicle) -> VehicleStateEngine:
    return VehicleStateEngine("TEST123", vehicle)


def _capture_execute() -> tuple[AsyncMock, list[list[ProjectionSpec]]]:
    """Create a mock _execute_command that captures projection specs."""
    captured: list[list[ProjectionSpec]] = []

    async def _mock_execute(
        command_fn: Any,
        projections: list[ProjectionSpec],
    ) -> None:
        captured.append(projections)
        await command_fn()

    mock = AsyncMock(side_effect=_mock_execute)
    return mock, captured


# ------------------------------------------------------------------
# Lock
# ------------------------------------------------------------------


class TestLockCapability:
    async def test_lock_projections(self) -> None:
        mock_lock = AsyncMock()
        execute, captured = _capture_execute()
        cap = LockCapability(
            lock_fn=mock_lock,
            unlock_fn=AsyncMock(),
            vin="TEST123",
            execute_command=execute,
        )
        await cap.lock()

        assert len(captured) == 1
        specs = captured[0]
        assert len(specs) == 4
        for spec in specs:
            assert spec.section == "realtime"
            assert spec.expected_value == LockState.LOCKED
            assert "door_lock" in spec.field_name
        mock_lock.assert_awaited_once_with("TEST123")

    async def test_unlock_projections(self) -> None:
        mock_unlock = AsyncMock()
        execute, captured = _capture_execute()
        cap = LockCapability(
            lock_fn=AsyncMock(),
            unlock_fn=mock_unlock,
            vin="TEST123",
            execute_command=execute,
        )
        await cap.unlock()

        specs = captured[0]
        for spec in specs:
            assert spec.expected_value == LockState.UNLOCKED
        mock_unlock.assert_awaited_once_with("TEST123")


# ------------------------------------------------------------------
# HVAC
# ------------------------------------------------------------------


class TestHvacCapability:
    async def test_start_projections(self) -> None:
        mock_start = AsyncMock()
        execute, captured = _capture_execute()
        cap = HvacCapability(
            start_fn=mock_start,
            stop_fn=AsyncMock(),
            schedule_fn=AsyncMock(),
            vin="TEST123",
            execute_command=execute,
        )
        await cap.start(temperature=22.0, duration=20)

        specs = captured[0]
        assert any(s.field_name == "status" and s.expected_value == HvacOverallStatus.ON for s in specs)
        assert any(s.field_name == "main_setting_temp_new" and s.expected_value == 22.0 for s in specs)
        mock_start.assert_awaited_once()

    async def test_stop_projections(self) -> None:
        execute, captured = _capture_execute()
        cap = HvacCapability(
            start_fn=AsyncMock(),
            stop_fn=AsyncMock(),
            schedule_fn=AsyncMock(),
            vin="TEST123",
            execute_command=execute,
        )
        await cap.stop()

        specs = captured[0]
        assert any(s.field_name == "status" and s.expected_value == HvacOverallStatus.OFF for s in specs)
        # Should also project seat states to OFF
        assert any(s.field_name == "main_seat_heat_state" and s.expected_value == SeatHeatVentState.OFF for s in specs)
        assert any(
            s.field_name == "steering_wheel_heat_state" and s.expected_value == StearingWheelHeat.OFF for s in specs
        )

    async def test_schedule_no_projections(self) -> None:
        from pybyd.models.control import ClimateScheduleParams

        execute, captured = _capture_execute()
        cap = HvacCapability(
            start_fn=AsyncMock(),
            stop_fn=AsyncMock(),
            schedule_fn=AsyncMock(),
            vin="TEST123",
            execute_command=execute,
        )
        params = ClimateScheduleParams(temperature=21.0, time_span=3, remote_mode=1)
        await cap.schedule(params)

        specs = captured[0]
        assert len(specs) == 0


# ------------------------------------------------------------------
# Seat
# ------------------------------------------------------------------


class TestSeatCapability:
    def _make_snapshot(self, vehicle: Vehicle) -> VehicleSnapshot:
        rt = VehicleRealtimeData.model_validate(
            {
                "mainSeatHeatState": 1,  # OFF
                "copilotSeatHeatState": 1,
                "mainSeatVentilationState": 1,
                "copilotSeatVentilationState": 1,
                "steeringWheelHeatState": 1,  # OFF
            }
        )
        hvac = HvacStatus.model_validate(
            {
                "mainSeatHeatState": 1,
                "copilotSeatHeatState": 1,
                "mainSeatVentilationState": 1,
                "copilotSeatVentilationState": 1,
                "steeringWheelHeatState": 1,
            }
        )
        return VehicleSnapshot(vehicle=vehicle, realtime=rt, hvac=hvac)

    async def test_heat_driver_high(self, vehicle: Vehicle) -> None:
        mock_set = AsyncMock()
        execute, captured = _capture_execute()
        snapshot = self._make_snapshot(vehicle)
        cap = SeatCapability(
            set_seat_climate_fn=mock_set,
            vin="TEST123",
            get_state=lambda: snapshot,
            execute_command=execute,
        )
        await cap.heat(SeatPosition.DRIVER, SeatLevel.HIGH)

        specs = captured[0]
        assert any(s.field_name == "main_seat_heat_state" and s.expected_value == SeatHeatVentState.HIGH for s in specs)
        # Activating seat heat must also project HVAC ON
        assert any(s.field_name == "status" and s.expected_value == HvacOverallStatus.ON for s in specs)
        mock_set.assert_awaited_once()
        call_args = mock_set.call_args
        assert call_args[0][0] == "TEST123"  # vin

    async def test_ventilation_copilot_low(self, vehicle: Vehicle) -> None:
        mock_set = AsyncMock()
        execute, captured = _capture_execute()
        snapshot = self._make_snapshot(vehicle)
        cap = SeatCapability(
            set_seat_climate_fn=mock_set,
            vin="TEST123",
            get_state=lambda: snapshot,
            execute_command=execute,
        )
        await cap.ventilation(SeatPosition.COPILOT, SeatLevel.LOW)

        specs = captured[0]
        assert any(
            s.field_name == "copilot_seat_ventilation_state" and s.expected_value == SeatHeatVentState.LOW
            for s in specs
        )
        # Activating seat ventilation must also project HVAC ON
        assert any(s.field_name == "status" and s.expected_value == HvacOverallStatus.ON for s in specs)

    async def test_heat_off_no_hvac_status_projection(self, vehicle: Vehicle) -> None:
        """Turning seat heat OFF must NOT project HVAC status (car keeps A/C running)."""
        execute, captured = _capture_execute()
        snapshot = self._make_snapshot(vehicle)
        cap = SeatCapability(
            set_seat_climate_fn=AsyncMock(),
            vin="TEST123",
            get_state=lambda: snapshot,
            execute_command=execute,
        )
        await cap.heat(SeatPosition.DRIVER, SeatLevel.OFF)

        specs = captured[0]
        assert any(s.field_name == "main_seat_heat_state" and s.expected_value == SeatHeatVentState.OFF for s in specs)
        assert not any(s.field_name == "status" for s in specs)

    async def test_ventilation_off_no_hvac_status_projection(self, vehicle: Vehicle) -> None:
        """Turning seat ventilation OFF must NOT project HVAC status."""
        execute, captured = _capture_execute()
        snapshot = self._make_snapshot(vehicle)
        cap = SeatCapability(
            set_seat_climate_fn=AsyncMock(),
            vin="TEST123",
            get_state=lambda: snapshot,
            execute_command=execute,
        )
        await cap.ventilation(SeatPosition.COPILOT, SeatLevel.OFF)

        specs = captured[0]
        assert any(
            s.field_name == "copilot_seat_ventilation_state" and s.expected_value == SeatHeatVentState.OFF
            for s in specs
        )
        assert not any(s.field_name == "status" for s in specs)

    async def test_seat_level_to_command_value(self) -> None:
        assert SeatLevel.OFF.to_command_value() == 3
        assert SeatLevel.LOW.to_command_value() == 2
        assert SeatLevel.HIGH.to_command_value() == 1

    async def test_seat_level_to_status_value(self) -> None:
        assert SeatLevel.OFF.to_status_value() == SeatHeatVentState.OFF
        assert SeatLevel.LOW.to_status_value() == SeatHeatVentState.LOW
        assert SeatLevel.HIGH.to_status_value() == SeatHeatVentState.HIGH


# ------------------------------------------------------------------
# Steering
# ------------------------------------------------------------------


class TestSteeringCapability:
    def _make_snapshot(self, vehicle: Vehicle) -> VehicleSnapshot:
        rt = VehicleRealtimeData.model_validate(
            {
                "steeringWheelHeatState": 1,  # OFF
            }
        )
        return VehicleSnapshot(vehicle=vehicle, realtime=rt)

    async def test_heat_on_projections(self, vehicle: Vehicle) -> None:
        mock_set = AsyncMock()
        execute, captured = _capture_execute()
        snapshot = self._make_snapshot(vehicle)
        cap = SteeringCapability(
            set_seat_climate_fn=mock_set,
            vin="TEST123",
            get_state=lambda: snapshot,
            execute_command=execute,
        )
        await cap.heat(on=True)

        specs = captured[0]
        assert any(
            s.field_name == "steering_wheel_heat_state" and s.expected_value == StearingWheelHeat.ON for s in specs
        )
        # Activating steering wheel heat must also project HVAC ON
        assert any(s.field_name == "status" and s.expected_value == HvacOverallStatus.ON for s in specs)

    async def test_heat_off_projections(self, vehicle: Vehicle) -> None:
        execute, captured = _capture_execute()
        snapshot = self._make_snapshot(vehicle)
        cap = SteeringCapability(
            set_seat_climate_fn=AsyncMock(),
            vin="TEST123",
            get_state=lambda: snapshot,
            execute_command=execute,
        )
        await cap.heat(on=False)

        specs = captured[0]
        assert any(
            s.field_name == "steering_wheel_heat_state" and s.expected_value == StearingWheelHeat.OFF for s in specs
        )
        # Turning steering wheel heat OFF must NOT project HVAC status
        assert not any(s.field_name == "status" for s in specs)


# ------------------------------------------------------------------
# Battery heat
# ------------------------------------------------------------------


class TestBatteryHeatCapability:
    async def test_heat_on(self) -> None:
        mock_set = AsyncMock()
        execute, captured = _capture_execute()
        cap = BatteryHeatCapability(
            set_battery_heat_fn=mock_set,
            vin="TEST123",
            execute_command=execute,
        )
        await cap.heat(on=True)

        specs = captured[0]
        assert any(s.field_name == "battery_heat_state" and s.expected_value == 1 for s in specs)

    async def test_heat_off(self) -> None:
        execute, captured = _capture_execute()
        cap = BatteryHeatCapability(
            set_battery_heat_fn=AsyncMock(),
            vin="TEST123",
            execute_command=execute,
        )
        await cap.heat(on=False)

        specs = captured[0]
        assert any(s.field_name == "battery_heat_state" and s.expected_value == 0 for s in specs)


# ------------------------------------------------------------------
# Finder
# ------------------------------------------------------------------


class TestFinderCapability:
    async def test_find(self) -> None:
        mock_find = AsyncMock()
        execute, captured = _capture_execute()
        cap = FinderCapability(
            find_fn=mock_find,
            flash_fn=AsyncMock(),
            vin="TEST123",
            execute_command=execute,
        )
        await cap.find()

        specs = captured[0]
        assert len(specs) == 0
        mock_find.assert_awaited_once_with("TEST123")

    async def test_flash_lights(self) -> None:
        mock_flash = AsyncMock()
        execute, captured = _capture_execute()
        cap = FinderCapability(
            find_fn=AsyncMock(),
            flash_fn=mock_flash,
            vin="TEST123",
            execute_command=execute,
        )
        await cap.flash_lights()

        specs = captured[0]
        assert len(specs) == 0
        mock_flash.assert_awaited_once_with("TEST123")


# ------------------------------------------------------------------
# Windows
# ------------------------------------------------------------------


class TestWindowsCapability:
    async def test_close(self) -> None:
        mock_close = AsyncMock()
        execute, captured = _capture_execute()
        cap = WindowsCapability(
            close_fn=mock_close,
            vin="TEST123",
            execute_command=execute,
        )
        await cap.close()

        specs = captured[0]
        assert len(specs) == 0
        mock_close.assert_awaited_once_with("TEST123")
