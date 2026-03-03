"""Tests for BydCar — aggregate lifecycle, MQTT routing, error handling."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from pybyd._state_engine import VehicleSnapshot
from pybyd.car import BydCar
from pybyd.exceptions import BydRemoteControlError
from pybyd.models.gps import GpsInfo
from pybyd.models.hvac import HvacStatus
from pybyd.models.realtime import LockState, VehicleRealtimeData
from pybyd.models.vehicle import Vehicle

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


@pytest.fixture()
def vehicle() -> Vehicle:
    return Vehicle.model_validate({"vin": "TEST123", "modelName": "Seal"})


def _mock_client() -> MagicMock:
    """Build a mock BydClient with all needed async methods."""
    client = MagicMock()
    client.lock = AsyncMock()
    client.unlock = AsyncMock()
    client.start_climate = AsyncMock()
    client.stop_climate = AsyncMock()
    client.schedule_climate = AsyncMock()
    client.set_seat_climate = AsyncMock()
    client.set_battery_heat = AsyncMock()
    client.find_car = AsyncMock()
    client.flash_lights = AsyncMock()
    client.close_windows = AsyncMock()
    client.get_vehicle_realtime = AsyncMock(return_value=VehicleRealtimeData.model_validate({"elecPercent": 80.0}))
    client.get_hvac_status = AsyncMock(return_value=HvacStatus.model_validate({"status": 2}))
    client.get_gps_info = AsyncMock(return_value=GpsInfo.model_validate({"latitude": 48.0, "longitude": 11.0}))
    from pybyd.models.charging import ChargingStatus

    client.get_charging_status = AsyncMock(return_value=ChargingStatus.model_validate({"vin": "TEST123", "soc": 80}))
    return client


@pytest.fixture()
def car(vehicle: Vehicle) -> BydCar:
    client = _mock_client()
    return BydCar(client, "TEST123", vehicle)


# ------------------------------------------------------------------
# Basic properties
# ------------------------------------------------------------------


class TestBydCarProperties:
    def test_vin(self, car: BydCar) -> None:
        assert car.vin == "TEST123"

    def test_initial_state(self, car: BydCar, vehicle: Vehicle) -> None:
        state = car.state
        assert state.vehicle is vehicle
        assert state.realtime is None
        assert state.hvac is None
        assert state.gps is None
        assert state.charging is None

    def test_capability_namespaces_exist(self, car: BydCar) -> None:
        assert car.lock is not None
        assert car.hvac is not None
        assert car.seat is not None
        assert car.steering is not None
        assert car.battery is not None
        assert car.finder is not None
        assert car.windows is not None


# ------------------------------------------------------------------
# Data fetch methods
# ------------------------------------------------------------------


class TestDataFetch:
    async def test_update_realtime(self, car: BydCar) -> None:
        data = await car.update_realtime()
        assert data.elec_percent == 80.0
        assert car.state.realtime is not None
        assert car.state.realtime.elec_percent == 80.0
        car._client.get_vehicle_realtime.assert_awaited_once_with("TEST123")

    async def test_update_hvac(self, car: BydCar) -> None:
        await car.update_hvac()
        assert car.state.hvac is not None
        car._client.get_hvac_status.assert_awaited_once_with("TEST123")

    async def test_update_gps(self, car: BydCar) -> None:
        await car.update_gps()
        assert car.state.gps is not None
        assert car.state.gps.latitude == 48.0
        car._client.get_gps_info.assert_awaited_once_with("TEST123")

    async def test_update_charging(self, car: BydCar) -> None:
        await car.update_charging()
        assert car.state.charging is not None
        car._client.get_charging_status.assert_awaited_once_with("TEST123")


# ------------------------------------------------------------------
# Command execution
# ------------------------------------------------------------------


class TestCommandExecution:
    async def test_lock_command(self, car: BydCar) -> None:
        await car.lock.lock()
        car._client.lock.assert_awaited_once_with("TEST123")

    async def test_unlock_command(self, car: BydCar) -> None:
        await car.lock.unlock()
        car._client.unlock.assert_awaited_once_with("TEST123")

    async def test_hvac_start(self, car: BydCar) -> None:
        await car.hvac.start(temperature=22.0, duration=20)
        car._client.start_climate.assert_awaited_once()

    async def test_hvac_stop(self, car: BydCar) -> None:
        await car.hvac.stop()
        car._client.stop_climate.assert_awaited_once()

    async def test_battery_heat(self, car: BydCar) -> None:
        await car.battery.heat(on=True)
        car._client.set_battery_heat.assert_awaited_once()

    async def test_finder_find(self, car: BydCar) -> None:
        await car.finder.find()
        car._client.find_car.assert_awaited_once_with("TEST123")

    async def test_finder_flash(self, car: BydCar) -> None:
        await car.finder.flash_lights()
        car._client.flash_lights.assert_awaited_once_with("TEST123")

    async def test_windows_close(self, car: BydCar) -> None:
        await car.windows.close()
        car._client.close_windows.assert_awaited_once_with("TEST123")


# ------------------------------------------------------------------
# Projection lifecycle via _execute_command
# ------------------------------------------------------------------


class TestProjectionLifecycle:
    async def test_command_registers_projections(self, car: BydCar) -> None:
        """Lock command should register projections that appear in state."""
        # First update realtime with unlocked state
        car._client.get_vehicle_realtime = AsyncMock(
            return_value=VehicleRealtimeData.model_validate({"leftFrontDoorLock": 1})
        )
        await car.update_realtime()
        assert car.state.realtime is not None
        assert car.state.realtime.left_front_door_lock == LockState.UNLOCKED

        # Lock command should project LOCKED
        await car.lock.lock()
        assert car.state.realtime is not None
        assert car.state.realtime.left_front_door_lock == LockState.LOCKED

    async def test_command_rollback_on_error(self, car: BydCar) -> None:
        """Projections should be rolled back on non-BydRemoteControlError."""
        car._client.get_vehicle_realtime = AsyncMock(
            return_value=VehicleRealtimeData.model_validate({"leftFrontDoorLock": 1})
        )
        await car.update_realtime()

        # Replace the stored function reference in the capability
        car.lock._lock_fn = AsyncMock(side_effect=RuntimeError("network error"))

        with pytest.raises(RuntimeError, match="network error"):
            await car.lock.lock()

        # Projections should be rolled back — still UNLOCKED
        assert car.state.realtime is not None
        assert car.state.realtime.left_front_door_lock == LockState.UNLOCKED

    async def test_remote_control_error_tentative_success(self, car: BydCar) -> None:
        """BydRemoteControlError should be treated as tentative success."""
        car._client.get_vehicle_realtime = AsyncMock(
            return_value=VehicleRealtimeData.model_validate({"leftFrontDoorLock": 1})
        )
        await car.update_realtime()

        car.lock._lock_fn = AsyncMock(
            side_effect=BydRemoteControlError("command failed", code="1009", endpoint="/control")
        )

        # Should NOT raise
        await car.lock.lock()

        # Projections should remain (tentative success)
        assert car.state.realtime is not None
        assert car.state.realtime.left_front_door_lock == LockState.LOCKED


# ------------------------------------------------------------------
# MQTT routing
# ------------------------------------------------------------------


class TestMqttRouting:
    async def test_handle_mqtt_realtime(self, car: BydCar) -> None:
        """MQTT push should update state engine through guard window."""
        data = VehicleRealtimeData.model_validate({"elecPercent": 75.0})
        car.handle_mqtt_realtime(data)
        # Allow the async task to complete
        await asyncio.sleep(0.05)
        assert car.state.realtime is not None
        assert car.state.realtime.elec_percent == 75.0

    async def test_mqtt_through_guard_window(self, car: BydCar) -> None:
        """MQTT push during active projection should not override projected state."""
        car._client.get_vehicle_realtime = AsyncMock(
            return_value=VehicleRealtimeData.model_validate({"leftFrontDoorLock": 1})
        )
        await car.update_realtime()

        # Lock command → projection active
        await car.lock.lock()
        assert car.state.realtime is not None
        assert car.state.realtime.left_front_door_lock == LockState.LOCKED

        # Stale MQTT push with UNLOCKED
        stale = VehicleRealtimeData.model_validate({"leftFrontDoorLock": 1})
        car.handle_mqtt_realtime(stale)
        await asyncio.sleep(0.05)

        # Projection should still overlay
        assert car.state.realtime is not None
        assert car.state.realtime.left_front_door_lock == LockState.LOCKED


# ------------------------------------------------------------------
# Lifecycle
# ------------------------------------------------------------------


class TestLifecycle:
    def test_close_idempotent(self, car: BydCar) -> None:
        car.close()
        car.close()  # should not raise


# ------------------------------------------------------------------
# on_state_changed callback
# ------------------------------------------------------------------


class TestOnStateChanged:
    async def test_callback_fires(self, vehicle: Vehicle) -> None:
        callback = MagicMock()
        client = _mock_client()
        car = BydCar(client, "TEST123", vehicle, on_state_changed=callback)

        await car.update_realtime()

        assert callback.call_count >= 1
        vin, snapshot = callback.call_args[0]
        assert vin == "TEST123"
        assert isinstance(snapshot, VehicleSnapshot)
