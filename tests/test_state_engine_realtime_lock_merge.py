from __future__ import annotations

import pytest

from pybyd._state_engine import VehicleStateEngine
from pybyd.models.realtime import LockState, VehicleRealtimeData
from pybyd.models.vehicle import Vehicle


@pytest.mark.asyncio
async def test_realtime_missing_lock_fields_preserve_previous_values() -> None:
    engine = VehicleStateEngine(vin="VIN123", vehicle=Vehicle(vin="VIN123"))

    initial = VehicleRealtimeData.model_validate(
        {
            "time": 100,
            "leftFrontDoorLock": 2,
            "rightFrontDoorLock": 2,
            "leftRearDoorLock": 2,
            "rightRearDoorLock": 2,
        }
    )
    await engine.update_realtime(initial)

    sparse = VehicleRealtimeData.model_validate(
        {
            "time": 200,
            "enduranceMileage": 320,
        }
    )
    await engine.update_realtime(sparse)

    realtime = engine.snapshot.realtime
    assert realtime is not None
    assert realtime.left_front_door_lock == LockState.LOCKED
    assert realtime.right_front_door_lock == LockState.LOCKED
    assert realtime.left_rear_door_lock == LockState.LOCKED
    assert realtime.right_rear_door_lock == LockState.LOCKED
    assert realtime.is_locked is True


@pytest.mark.asyncio
async def test_realtime_explicit_lock_field_overrides_previous_value() -> None:
    engine = VehicleStateEngine(vin="VIN456", vehicle=Vehicle(vin="VIN456"))

    initial = VehicleRealtimeData.model_validate(
        {
            "time": 100,
            "leftFrontDoorLock": 2,
            "rightFrontDoorLock": 2,
            "leftRearDoorLock": 2,
            "rightRearDoorLock": 2,
        }
    )
    await engine.update_realtime(initial)

    update_with_unlock = VehicleRealtimeData.model_validate(
        {
            "time": 300,
            "leftFrontDoorLock": 1,
        }
    )
    await engine.update_realtime(update_with_unlock)

    realtime = engine.snapshot.realtime
    assert realtime is not None
    assert realtime.left_front_door_lock == LockState.UNLOCKED
    assert realtime.right_front_door_lock == LockState.LOCKED
    assert realtime.left_rear_door_lock == LockState.LOCKED
    assert realtime.right_rear_door_lock == LockState.LOCKED
    assert realtime.is_locked is False
