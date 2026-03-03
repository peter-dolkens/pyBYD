"""HVAC (climate control) capability."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pybyd._constants import minutes_to_time_span
from pybyd._state_engine import ProjectionSpec
from pybyd.models.control import ClimateScheduleParams, ClimateStartParams
from pybyd.models.hvac import HvacOverallStatus
from pybyd.models.realtime import SeatHeatVentState, StearingWheelHeat

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class HvacCapability:
    """HVAC start/stop/schedule commands with projection support."""

    def __init__(
        self,
        *,
        start_fn: Callable[..., Awaitable[Any]],
        stop_fn: Callable[..., Awaitable[Any]],
        schedule_fn: Callable[..., Awaitable[Any]],
        vin: str,
        execute_command: Callable[..., Awaitable[None]],
    ) -> None:
        self._start_fn = start_fn
        self._stop_fn = stop_fn
        self._schedule_fn = schedule_fn
        self._vin = vin
        self._execute = execute_command

    async def start(self, temperature: float = 21.0, duration: int = 20) -> None:
        """Start climate control.

        Parameters
        ----------
        temperature
            Target temperature in °C (15-31).
        duration
            Run duration in minutes (10/15/20/25/30).
        """
        time_span = minutes_to_time_span(duration)
        params = ClimateStartParams(temperature=temperature, time_span=time_span)
        specs = [
            ProjectionSpec("hvac", "status", HvacOverallStatus.ON),
            ProjectionSpec("hvac", "main_setting_temp_new", temperature),
        ]

        async def _cmd() -> Any:
            return await self._start_fn(self._vin, params=params)

        await self._execute(_cmd, specs)

    async def stop(self) -> None:
        """Stop climate control (including seat/steering heat)."""
        specs = [
            ProjectionSpec("hvac", "status", HvacOverallStatus.OFF),
            ProjectionSpec("hvac", "main_seat_heat_state", SeatHeatVentState.OFF),
            ProjectionSpec("hvac", "copilot_seat_heat_state", SeatHeatVentState.OFF),
            ProjectionSpec("hvac", "main_seat_ventilation_state", SeatHeatVentState.OFF),
            ProjectionSpec("hvac", "copilot_seat_ventilation_state", SeatHeatVentState.OFF),
            ProjectionSpec("hvac", "steering_wheel_heat_state", StearingWheelHeat.OFF),
            ProjectionSpec("realtime", "main_seat_heat_state", SeatHeatVentState.OFF),
            ProjectionSpec("realtime", "copilot_seat_heat_state", SeatHeatVentState.OFF),
            ProjectionSpec("realtime", "main_seat_ventilation_state", SeatHeatVentState.OFF),
            ProjectionSpec("realtime", "copilot_seat_ventilation_state", SeatHeatVentState.OFF),
            ProjectionSpec("realtime", "steering_wheel_heat_state", StearingWheelHeat.OFF),
        ]

        async def _cmd() -> Any:
            return await self._stop_fn(self._vin)

        await self._execute(_cmd, specs)

    async def schedule(self, params: ClimateScheduleParams) -> None:
        """Schedule climate control.

        Parameters
        ----------
        params
            Pre-built schedule parameters (create/modify/remove).
        """

        async def _cmd() -> Any:
            return await self._schedule_fn(self._vin, params=params)

        # Scheduling doesn't change observable state immediately
        await self._execute(_cmd, [])
