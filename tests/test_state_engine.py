"""Tests for the VehicleStateEngine — merge, projection, guard, rollback."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from pybyd._state_engine import ProjectionSpec, VehicleSnapshot, VehicleStateEngine
from pybyd.models.charging import ChargingStatus
from pybyd.models.gps import GpsInfo
from pybyd.models.hvac import HvacOverallStatus, HvacStatus
from pybyd.models.realtime import LockState, VehicleRealtimeData
from pybyd.models.vehicle import Vehicle

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


@pytest.fixture()
def vehicle() -> Vehicle:
    return Vehicle.model_validate({"vin": "TEST123", "modelName": "Seal"})


@pytest.fixture()
def engine(vehicle: Vehicle) -> VehicleStateEngine:
    return VehicleStateEngine("TEST123", vehicle)


def _make_realtime(**overrides: Any) -> VehicleRealtimeData:
    data: dict[str, Any] = {"elecPercent": 80.0, "leftFrontDoorLock": 2}
    data.update(overrides)
    return VehicleRealtimeData.model_validate(data)


def _make_hvac(**overrides: Any) -> HvacStatus:
    data: dict[str, Any] = {"status": 2}
    data.update(overrides)
    return HvacStatus.model_validate(data)


def _make_gps(lat: float = 48.0, lon: float = 11.0) -> GpsInfo:
    return GpsInfo.model_validate({"latitude": lat, "longitude": lon})


def _make_charging(**overrides: Any) -> ChargingStatus:
    data: dict[str, Any] = {"vin": "TEST123", "soc": 80}
    data.update(overrides)
    return ChargingStatus.model_validate(data)


# ------------------------------------------------------------------
# Basic snapshot
# ------------------------------------------------------------------


class TestInitialSnapshot:
    def test_initial_snapshot_has_vehicle(self, engine: VehicleStateEngine, vehicle: Vehicle) -> None:
        assert engine.snapshot.vehicle is vehicle
        assert engine.snapshot.realtime is None
        assert engine.snapshot.hvac is None
        assert engine.snapshot.gps is None
        assert engine.snapshot.charging is None

    def test_snapshot_is_frozen(self, engine: VehicleStateEngine) -> None:
        with pytest.raises(AttributeError):
            engine.snapshot.realtime = _make_realtime()  # type: ignore[misc]


# ------------------------------------------------------------------
# Partial merges
# ------------------------------------------------------------------


class TestPartialMerge:
    async def test_update_realtime(self, engine: VehicleStateEngine) -> None:
        data = _make_realtime()
        await engine.update_realtime(data)
        assert engine.snapshot.realtime is data
        assert engine.snapshot.hvac is None  # untouched

    async def test_update_hvac(self, engine: VehicleStateEngine) -> None:
        data = _make_hvac()
        await engine.update_hvac(data)
        assert engine.snapshot.hvac is data
        assert engine.snapshot.realtime is None

    async def test_update_gps(self, engine: VehicleStateEngine) -> None:
        data = _make_gps()
        await engine.update_gps(data)
        assert engine.snapshot.gps is data

    async def test_update_charging(self, engine: VehicleStateEngine) -> None:
        data = _make_charging()
        await engine.update_charging(data)
        assert engine.snapshot.charging is data

    async def test_multiple_sections_independent(self, engine: VehicleStateEngine) -> None:
        rt = _make_realtime()
        hvac = _make_hvac()
        await engine.update_realtime(rt)
        await engine.update_hvac(hvac)
        assert engine.snapshot.realtime is rt
        assert engine.snapshot.hvac is hvac

    async def test_second_update_replaces_first(self, engine: VehicleStateEngine) -> None:
        first = _make_realtime(elecPercent=80.0)
        second = _make_realtime(elecPercent=50.0)
        await engine.update_realtime(first)
        await engine.update_realtime(second)
        assert engine.snapshot.realtime is second
        assert engine.snapshot.realtime.elec_percent == 50.0


# ------------------------------------------------------------------
# Value-quality validators
# ------------------------------------------------------------------


class TestValueValidators:
    async def test_soc_zero_guarded(self, engine: VehicleStateEngine) -> None:
        """Zero SOC after non-zero should be rejected."""
        first = _make_realtime(elecPercent=80.0)
        await engine.update_realtime(first)

        spike = _make_realtime(elecPercent=0.0)
        await engine.update_realtime(spike)
        assert engine.snapshot.realtime is not None
        assert engine.snapshot.realtime.elec_percent == 80.0  # guarded

    async def test_soc_zero_accepted_on_first(self, engine: VehicleStateEngine) -> None:
        """Zero SOC on first update is accepted (no previous to guard against)."""
        data = _make_realtime(elecPercent=0.0)
        await engine.update_realtime(data)
        assert engine.snapshot.realtime is not None
        assert engine.snapshot.realtime.elec_percent == 0.0

    async def test_soc_nonzero_accepted(self, engine: VehicleStateEngine) -> None:
        first = _make_realtime(elecPercent=80.0)
        await engine.update_realtime(first)
        second = _make_realtime(elecPercent=42.0)
        await engine.update_realtime(second)
        assert engine.snapshot.realtime is not None
        assert engine.snapshot.realtime.elec_percent == 42.0

    async def test_gps_null_island_guarded(self, engine: VehicleStateEngine) -> None:
        good = _make_gps(48.0, 11.0)
        await engine.update_gps(good)

        bad = _make_gps(0.0, 0.0)
        await engine.update_gps(bad)
        assert engine.snapshot.gps is good  # guarded

    async def test_gps_valid_updates_accepted(self, engine: VehicleStateEngine) -> None:
        first = _make_gps(48.0, 11.0)
        await engine.update_gps(first)

        second = _make_gps(49.0, 12.0)
        await engine.update_gps(second)
        assert engine.snapshot.gps is second


# ------------------------------------------------------------------
# Projections
# ------------------------------------------------------------------


class TestProjections:
    async def test_projection_applies_to_snapshot(self, engine: VehicleStateEngine) -> None:
        """Projections should overlay on existing base state."""
        data = _make_realtime(leftFrontDoorLock=1)  # UNLOCKED
        await engine.update_realtime(data)
        assert engine.snapshot.realtime is not None
        assert engine.snapshot.realtime.left_front_door_lock == LockState.UNLOCKED

        # Register lock projection
        async with engine.lock:
            engine.register_projections(
                [
                    ProjectionSpec("realtime", "left_front_door_lock", LockState.LOCKED),
                ]
            )

        assert engine.snapshot.realtime is not None
        assert engine.snapshot.realtime.left_front_door_lock == LockState.LOCKED

    async def test_projection_confirmed_by_matching_data(self, engine: VehicleStateEngine) -> None:
        """When incoming data matches projection, projection is removed."""
        data = _make_realtime(leftFrontDoorLock=1)  # UNLOCKED
        await engine.update_realtime(data)

        async with engine.lock:
            engine.register_projections(
                [
                    ProjectionSpec("realtime", "left_front_door_lock", LockState.LOCKED),
                ]
            )

        # Incoming data confirms the projection
        confirmed = _make_realtime(leftFrontDoorLock=2)  # LOCKED
        await engine.update_realtime(confirmed)

        assert engine.snapshot.realtime is not None
        assert engine.snapshot.realtime.left_front_door_lock == LockState.LOCKED
        assert len(engine.active_projections) == 0  # projection cleared

    async def test_projection_rejects_contradicting_data(self, engine: VehicleStateEngine) -> None:
        """Contradicting data should be overridden by active projection (guard window)."""
        data = _make_realtime(leftFrontDoorLock=1)  # UNLOCKED
        await engine.update_realtime(data)

        async with engine.lock:
            engine.register_projections(
                [
                    ProjectionSpec("realtime", "left_front_door_lock", LockState.LOCKED),
                ]
            )

        # Contradicting MQTT data arrives (still UNLOCKED)
        stale = _make_realtime(leftFrontDoorLock=1)  # UNLOCKED
        await engine.update_realtime(stale)

        # Projection should override the stale data
        assert engine.snapshot.realtime is not None
        assert engine.snapshot.realtime.left_front_door_lock == LockState.LOCKED
        assert len(engine.active_projections) == 1  # projection still active

    async def test_projection_expires_after_ttl(self, engine: VehicleStateEngine) -> None:
        """Expired projections are removed and incoming data is accepted."""
        engine_short_ttl = VehicleStateEngine("TEST123", engine._vehicle, projection_ttl=0.01)

        data = _make_realtime(leftFrontDoorLock=1)  # UNLOCKED
        await engine_short_ttl.update_realtime(data)

        async with engine_short_ttl.lock:
            engine_short_ttl.register_projections(
                [
                    ProjectionSpec("realtime", "left_front_door_lock", LockState.LOCKED),
                ]
            )

        # Wait for TTL to expire
        await asyncio.sleep(0.05)

        # Now incoming data should be accepted
        new_data = _make_realtime(leftFrontDoorLock=1)  # UNLOCKED
        await engine_short_ttl.update_realtime(new_data)

        assert engine_short_ttl.snapshot.realtime is not None
        assert engine_short_ttl.snapshot.realtime.left_front_door_lock == LockState.UNLOCKED
        assert len(engine_short_ttl.active_projections) == 0

    async def test_rollback_removes_projections(self, engine: VehicleStateEngine) -> None:
        data = _make_realtime(leftFrontDoorLock=1)  # UNLOCKED
        await engine.update_realtime(data)

        async with engine.lock:
            cmd_id = engine.register_projections(
                [
                    ProjectionSpec("realtime", "left_front_door_lock", LockState.LOCKED),
                ]
            )
            assert engine.snapshot.realtime is not None
            assert engine.snapshot.realtime.left_front_door_lock == LockState.LOCKED

            engine.rollback_projections(cmd_id)

        assert engine.snapshot.realtime is not None
        assert engine.snapshot.realtime.left_front_door_lock == LockState.UNLOCKED
        assert len(engine.active_projections) == 0

    async def test_multiple_projections_different_sections(self, engine: VehicleStateEngine) -> None:
        rt = _make_realtime(leftFrontDoorLock=1)
        hvac = _make_hvac(status=2)  # OFF
        await engine.update_realtime(rt)
        await engine.update_hvac(hvac)

        async with engine.lock:
            engine.register_projections(
                [
                    ProjectionSpec("realtime", "left_front_door_lock", LockState.LOCKED),
                    ProjectionSpec("hvac", "status", HvacOverallStatus.ON),
                ]
            )

        assert engine.snapshot.realtime is not None
        assert engine.snapshot.realtime.left_front_door_lock == LockState.LOCKED
        assert engine.snapshot.hvac is not None
        assert engine.snapshot.hvac.status == HvacOverallStatus.ON

    async def test_rollback_only_removes_same_command(self, engine: VehicleStateEngine) -> None:
        """Rollback should only remove projections from the specified command."""
        data = _make_realtime(leftFrontDoorLock=1, batteryHeatState=0)
        await engine.update_realtime(data)

        async with engine.lock:
            cmd1 = engine.register_projections(
                [
                    ProjectionSpec("realtime", "left_front_door_lock", LockState.LOCKED),
                ]
            )
            engine.register_projections(
                [
                    ProjectionSpec("realtime", "battery_heat_state", 1),
                ]
            )

            engine.rollback_projections(cmd1)

        # cmd2 projection should still be active
        assert engine.snapshot.realtime is not None
        assert engine.snapshot.realtime.left_front_door_lock == LockState.UNLOCKED
        assert engine.snapshot.realtime.battery_heat_state == 1
        assert len(engine.active_projections) == 1

    async def test_projection_without_base_data_not_applied(self, engine: VehicleStateEngine) -> None:
        """Projections on sections without base data produce None in snapshot."""
        async with engine.lock:
            engine.register_projections(
                [
                    ProjectionSpec("realtime", "left_front_door_lock", LockState.LOCKED),
                ]
            )
        # No base realtime data — snapshot section remains None
        assert engine.snapshot.realtime is None


# ------------------------------------------------------------------
# on_state_changed callback
# ------------------------------------------------------------------


class TestOnStateChanged:
    async def test_fires_on_data_update(self, vehicle: Vehicle) -> None:
        callback = MagicMock()
        engine = VehicleStateEngine("TEST123", vehicle, on_state_changed=callback)

        data = _make_realtime()
        await engine.update_realtime(data)

        assert callback.call_count >= 1
        vin, snapshot = callback.call_args[0]
        assert vin == "TEST123"
        assert isinstance(snapshot, VehicleSnapshot)
        assert snapshot.realtime is data

    async def test_fires_on_projection_register(self, vehicle: Vehicle) -> None:
        callback = MagicMock()
        engine = VehicleStateEngine("TEST123", vehicle, on_state_changed=callback)

        data = _make_realtime(leftFrontDoorLock=1)
        await engine.update_realtime(data)
        callback.reset_mock()

        async with engine.lock:
            engine.register_projections(
                [
                    ProjectionSpec("realtime", "left_front_door_lock", LockState.LOCKED),
                ]
            )

        assert callback.call_count >= 1

    async def test_fires_on_rollback(self, vehicle: Vehicle) -> None:
        callback = MagicMock()
        engine = VehicleStateEngine("TEST123", vehicle, on_state_changed=callback)

        data = _make_realtime(leftFrontDoorLock=1)
        await engine.update_realtime(data)

        async with engine.lock:
            cmd_id = engine.register_projections(
                [
                    ProjectionSpec("realtime", "left_front_door_lock", LockState.LOCKED),
                ]
            )
            callback.reset_mock()
            engine.rollback_projections(cmd_id)

        assert callback.call_count >= 1

    async def test_callback_exception_does_not_propagate(self, vehicle: Vehicle) -> None:
        def bad_callback(vin: str, snapshot: VehicleSnapshot) -> None:
            raise RuntimeError("boom")

        engine = VehicleStateEngine("TEST123", vehicle, on_state_changed=bad_callback)
        # Should not raise
        await engine.update_realtime(_make_realtime())


# ------------------------------------------------------------------
# Concurrency
# ------------------------------------------------------------------


class TestConcurrency:
    async def test_concurrent_updates_serialize(self, engine: VehicleStateEngine) -> None:
        """Two concurrent update_realtime calls should serialize and both succeed."""
        results: list[float | None] = []

        async def _update(soc: float) -> None:
            data = _make_realtime(elecPercent=soc)
            await engine.update_realtime(data)
            assert engine.snapshot.realtime is not None
            results.append(engine.snapshot.realtime.elec_percent)

        await asyncio.gather(_update(80.0), _update(50.0))

        # Both should have completed; the final state is deterministic
        # (last writer wins, which depends on scheduling)
        assert engine.snapshot.realtime is not None
        assert engine.snapshot.realtime.elec_percent in (80.0, 50.0)
        assert len(results) == 2

    async def test_command_lock_serializes_with_updates(self, engine: VehicleStateEngine) -> None:
        """A held command lock blocks data updates."""
        data = _make_realtime(leftFrontDoorLock=1)
        await engine.update_realtime(data)

        update_started = asyncio.Event()
        update_done = asyncio.Event()

        async def _blocked_update() -> None:
            update_started.set()
            new_data = _make_realtime(leftFrontDoorLock=2)
            await engine.update_realtime(new_data)
            update_done.set()

        async with engine.lock:
            task = asyncio.create_task(_blocked_update())
            await update_started.wait()
            await asyncio.sleep(0.01)
            # Update should be blocked
            assert not update_done.is_set()
            engine.register_projections(
                [
                    ProjectionSpec("realtime", "left_front_door_lock", LockState.LOCKED),
                ]
            )

        await task
        assert update_done.is_set()
