"""Remote control models, parameter builders, and command responses.

Consolidates enums, result models, typed ``controlParamsMap`` payloads,
and lightweight acknowledgement wrappers.
"""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator
from pydantic.alias_generators import to_camel

from pybyd._constants import celsius_to_scale
from pybyd.models._base import BydBaseModel

if TYPE_CHECKING:
    from pybyd.models.hvac import HvacStatus  # noqa: F401
    from pybyd.models.realtime import VehicleRealtimeData  # noqa: F401

# ------------------------------------------------------------------
# Enums
# ------------------------------------------------------------------


class RemoteCommand(enum.StrEnum):
    """Remote control ``commandType`` values.

    Each value corresponds to the ``commandType`` string sent to
    ``/control/remoteControl`` on the BYD API, as confirmed by
    Niek (BYD-re) and TA2k's APK analysis.
    """

    LOCK = "LOCKDOOR"
    UNLOCK = "OPENDOOR"
    START_CLIMATE = "OPENAIR"
    STOP_CLIMATE = "CLOSEAIR"
    SCHEDULE_CLIMATE = "BOOKINGAIR"
    FIND_CAR = "FINDCAR"
    FLASH_LIGHTS = "FLASHLIGHTNOWHISTLE"
    CLOSE_WINDOWS = "CLOSEWINDOW"
    SEAT_CLIMATE = "VENTILATIONHEATING"
    BATTERY_HEAT = "BATTERYHEAT"


class ControlState(enum.IntEnum):
    """Control command execution state."""

    PENDING = 0
    SUCCESS = 1
    FAILURE = 2


# ------------------------------------------------------------------
# Command result
# ------------------------------------------------------------------


class RemoteControlResult(BydBaseModel):
    """Result of a remote control command."""

    control_state: ControlState = Field(default=ControlState.PENDING, validation_alias="controlState")
    success: bool = False
    request_serial: str | None = Field(default=None, validation_alias="requestSerial")

    @model_validator(mode="before")
    @classmethod
    def _normalize_shapes(cls, values: Any) -> Any:
        if not isinstance(values, dict):
            return values

        merged = dict(values)

        # Immediate result format: {"res": 2, ...}
        if "res" in merged and "controlState" not in merged and "control_state" not in merged:
            res_val = int(merged["res"])
            state = ControlState.SUCCESS if res_val == 2 else ControlState.FAILURE
            merged["controlState"] = int(state)
            merged.setdefault("success", state == ControlState.SUCCESS)

        # Standard polled format: {"controlState": 0/1/2, ...}
        if "controlState" in merged and "success" not in merged:
            try:
                state = ControlState(int(merged["controlState"]))
            except ValueError:
                state = ControlState.PENDING
            merged["controlState"] = int(state)
            merged["success"] = state == ControlState.SUCCESS

        return merged


# ------------------------------------------------------------------
# Command acknowledgement responses
# ------------------------------------------------------------------


class CommandAck(BydBaseModel):
    """Generic acknowledgement response for write/toggle endpoints."""

    vin: str = ""
    result: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_result(cls, values: Any) -> Any:
        """Ensure ``result`` is a string or ``None``."""
        if not isinstance(values, dict):
            return values
        r = values.get("result")
        if r is not None and not isinstance(r, str):
            values = {**values, "result": None}
        return values


class VerifyControlPasswordResponse(BydBaseModel):
    """Response from the control password verification endpoint."""

    vin: str = ""
    ok: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def _coerce_ok(cls, values: Any) -> Any:
        """Ensure ``ok`` is a bool or ``None``."""
        if not isinstance(values, dict):
            return values
        v = values.get("ok")
        if v is not None and not isinstance(v, bool):
            values = {**values, "ok": None}
        return values


# ------------------------------------------------------------------
# Control parameter payloads (serialised to ``controlParamsMap``)
# ------------------------------------------------------------------


class ControlParams(BaseModel):
    """Base class for control-parameter models."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_default=True,
        str_strip_whitespace=True,
        alias_generator=to_camel,
        populate_by_name=True,
    )

    def to_control_params_map(self) -> dict[str, Any]:
        """Return a dict that can be JSON-encoded into ``controlParamsMap``."""
        return self.model_dump(by_alias=True, exclude_none=True)


class ClimateStartParams(ControlParams):
    """Parameters for starting HVAC (commandType ``OPENAIR``).

    Temperatures are specified in °C (15-31) and automatically converted
    to BYD's internal scale (1-17) on serialisation.
    """

    temperature: float | None = Field(default=None, ge=15.0, le=31.0)
    """Driver temperature setpoint in °C (15-31)."""

    copilot_temperature: float | None = Field(default=None, ge=15.0, le=31.0)
    """Passenger temperature setpoint in °C (15-31)."""

    cycle_mode: int | None = Field(default=None, ge=0)
    """Air recirculation/cycle mode code."""

    time_span: int | None = Field(default=None, ge=1, le=5)
    """Run duration code (1=10min, 2=15min, 3=20min, 4=25min, 5=30min)."""

    ac_switch: int | None = Field(default=None, ge=0, le=1)
    air_accuracy: int | None = Field(default=None, ge=0)
    air_conditioning_mode: int | None = Field(default=None, ge=0)
    remote_mode: int | None = Field(default=None, ge=0)
    wind_level: int | None = Field(default=None, ge=0)
    wind_position: int | None = Field(default=None, ge=0)

    @field_serializer("temperature", "copilot_temperature")
    def _serialize_temp(self, value: float | None) -> int | None:
        return celsius_to_scale(value) if value is not None else None

    def to_control_params_map(self) -> dict[str, Any]:
        """Return a dict that can be JSON-encoded into ``controlParamsMap``."""
        data = self.model_dump(by_alias=True, exclude_none=True)
        # Map temperature fields to BYD's expected keys.
        if "temperature" in data:
            data["mainSettingTemp"] = data.pop("temperature")
        if "copilotTemperature" in data:
            data["copilotSettingTemp"] = data.pop("copilotTemperature")
        return data


class ClimateScheduleParams(ClimateStartParams):
    """Parameters for scheduling HVAC (commandType ``BOOKINGAIR``)."""

    booking_id: int = Field(..., ge=1)
    """Schedule booking ID."""

    booking_time: int = Field(..., ge=1)
    """Schedule time as epoch seconds."""


class SeatClimateParams(ControlParams):
    """Parameters for seat heating/ventilation (commandType ``VENTILATIONHEATING``).

    Values use the same scale as the status enums:
    - 0 = not applicable (feature absent)
    - 1 = off
    - 2 = low
    - 3 = high
    """

    main_heat: int | None = Field(default=None, ge=0, le=3)
    main_ventilation: int | None = Field(default=None, ge=0, le=3)
    copilot_heat: int | None = Field(default=None, ge=0, le=3)
    copilot_ventilation: int | None = Field(default=None, ge=0, le=3)
    lr_seat_heat: int | None = Field(default=None, ge=0, le=3)
    lr_seat_ventilation: int | None = Field(default=None, ge=0, le=3)
    rr_seat_heat: int | None = Field(default=None, ge=0, le=3)
    rr_seat_ventilation: int | None = Field(default=None, ge=0, le=3)
    steering_wheel_heat: int | None = Field(default=None, ge=0, le=1)

    # Mapping from model attribute names → constructor keyword arguments.
    _SEAT_ATTR_TO_PARAM: ClassVar[dict[str, str]] = {
        "main_seat_heat_state": "main_heat",
        "main_seat_ventilation_state": "main_ventilation",
        "copilot_seat_heat_state": "copilot_heat",
        "copilot_seat_ventilation_state": "copilot_ventilation",
        "lr_seat_heat_state": "lr_seat_heat",
        "lr_seat_ventilation_state": "lr_seat_ventilation",
        "rr_seat_heat_state": "rr_seat_heat",
        "rr_seat_ventilation_state": "rr_seat_ventilation",
    }

    @classmethod
    def from_current_state(
        cls,
        hvac: HvacStatus | None = None,
        realtime: VehicleRealtimeData | None = None,
    ) -> SeatClimateParams:
        """Build params from current vehicle state.

        The BYD API requires *all* seat climate values to be sent with
        every command.  This factory reads the current state from the
        HVAC status (preferred) with realtime data as fallback, and
        converts each ``SeatHeatVentState`` to the command scale via
        :pymeth:`SeatHeatVentState.to_command_level`.

        Steering wheel heat is included (``1`` = on, ``0`` = off).
        """
        from pybyd.models.realtime import SeatHeatVentState, StearingWheelHeat

        kwargs: dict[str, int] = {}

        for attr, param in cls._SEAT_ATTR_TO_PARAM.items():
            val = None
            if hvac is not None:
                val = getattr(hvac, attr, None)
            if val is None and realtime is not None:
                val = getattr(realtime, attr, None)
            if isinstance(val, SeatHeatVentState):
                kwargs[param] = val.to_command_level()
            else:
                kwargs[param] = 0

        # Steering wheel heat
        sw_val = None
        if hvac is not None:
            sw_val = getattr(hvac, "steering_wheel_heat_state", None)
        if sw_val is None and realtime is not None:
            sw_val = getattr(realtime, "steering_wheel_heat_state", None)
        kwargs["steering_wheel_heat"] = 1 if sw_val == StearingWheelHeat.ON else 0

        return cls(**kwargs)


class BatteryHeatParams(ControlParams):
    """Parameters for battery heating (commandType ``BATTERYHEAT``)."""

    on: bool = Field(..., serialization_alias="batteryHeatSwitch")

    @field_serializer("on")
    def _serialize_on(self, value: bool) -> int:
        return 1 if value else 0
