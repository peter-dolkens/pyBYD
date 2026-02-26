from __future__ import annotations

import json

import pytest

from pybyd._api.control import _build_control_inner, _is_remote_control_ready
from pybyd.config import BydConfig
from pybyd.models.control import (
    ClimateScheduleParams,
    ClimateStartParams,
    ControlState,
    RemoteCommand,
    RemoteControlResult,
    SeatClimateParams,
)

# ------------------------------------------------------------------
# ClimateStartParams
# ------------------------------------------------------------------


def test_climate_start_params_celsius_to_scale() -> None:
    params = ClimateStartParams(temperature=21.0, time_span=1)
    payload = params.to_control_params_map()
    assert payload["mainSettingTemp"] == 7
    assert payload["timeSpan"] == 1


def test_climate_start_params_rejects_out_of_range_temp() -> None:
    with pytest.raises(ValueError):
        ClimateStartParams(temperature=32.0)


def test_climate_start_params_immediate_defaults() -> None:
    """Immediate start should include all required defaults from API dumps."""
    params = ClimateStartParams(temperature=20.0, time_span=2)
    payload = params.to_control_params_map()
    assert payload["remoteMode"] == 4
    assert payload["airAccuracy"] == 1
    assert payload["airConditioningMode"] == 1
    assert payload["cycleMode"] == 2  # default internal circulation
    assert payload["airSet"] is None  # must be present as null for immediate mode


def test_climate_start_params_mirrors_copilot_temp() -> None:
    """When copilot_temperature is not set, it should mirror driver temp."""
    params = ClimateStartParams(temperature=20.0, time_span=1)
    payload = params.to_control_params_map()
    assert payload["mainSettingTemp"] == payload["copilotSettingTemp"]


def test_climate_start_params_independent_copilot_temp() -> None:
    """When copilot_temperature is explicitly set, it stays independent."""
    params = ClimateStartParams(temperature=20.0, copilot_temperature=25.0, time_span=1)
    payload = params.to_control_params_map()
    assert payload["mainSettingTemp"] == 6
    assert payload["copilotSettingTemp"] == 11


def test_climate_start_params_matches_api_dump_immediate() -> None:
    """Verify output matches real API dump for immediate AC activation."""
    # From temp.txt: "Activate AC immediately, 10 mins, internal circulation"
    params = ClimateStartParams(temperature=19.0, time_span=1, cycle_mode=2)
    payload = params.to_control_params_map()
    assert payload["remoteMode"] == 4
    assert payload["timeSpan"] == 1
    assert payload["mainSettingTemp"] == 5
    assert payload["copilotSettingTemp"] == 5
    assert payload["cycleMode"] == 2
    assert payload["airAccuracy"] == 1
    assert payload["airConditioningMode"] == 1
    assert payload["airSet"] is None


# ------------------------------------------------------------------
# ClimateScheduleParams
# ------------------------------------------------------------------


def test_climate_schedule_params_includes_booking_fields() -> None:
    params = ClimateScheduleParams(
        booking_time=1772024400,
        temperature=24.0,
        time_span=1,
        cycle_mode=1,
    )
    payload = params.to_control_params_map()
    assert payload["bookingTime"] == 1772024400
    assert payload["mainSettingTemp"] == 10
    assert payload["timeSpan"] == 1
    assert payload["remoteMode"] == 1  # create
    assert payload["acSwitch"] == 0


def test_climate_schedule_params_modify() -> None:
    params = ClimateScheduleParams(
        remote_mode=2,
        booking_id=1178679406714171392,
        booking_time=1772024400,
        temperature=24.0,
        time_span=2,
        cycle_mode=2,
        air_conditioning_mode=0,
    )
    payload = params.to_control_params_map()
    assert payload["remoteMode"] == 2
    assert payload["bookingId"] == 1178679406714171392


def test_climate_schedule_params_remove() -> None:
    params = ClimateScheduleParams(
        remote_mode=3,
        booking_id=1178679406714171392,
        air_accuracy=1,
    )
    payload = params.to_control_params_map()
    assert payload["remoteMode"] == 3
    assert payload["bookingId"] == 1178679406714171392


# ------------------------------------------------------------------
# SeatClimateParams
# ------------------------------------------------------------------


def test_seat_climate_params_key_encoding() -> None:
    """Verify camelCase serialisation matches API field names."""
    params = SeatClimateParams(
        chair_type="1",
        main_heat=1,
        copilot_ventilation=3,
        steering_wheel_heat_state=1,
    )
    payload = params.to_control_params_map()
    assert payload["chairType"] == "1"
    assert payload["mainHeat"] == 1
    assert payload["copilotVentilation"] == 3
    assert payload["steeringWheelHeatState"] == 1
    assert payload["remoteMode"] == 1


def test_seat_climate_params_rear_field_names() -> None:
    """Rear seat fields must serialise to *State suffix (e.g. lrSeatHeatState)."""
    params = SeatClimateParams(
        lr_seat_heat_state=1,
        rr_seat_ventilation_state=2,
        lr_third_heat_state=0,
    )
    payload = params.to_control_params_map()
    assert "lrSeatHeatState" in payload
    assert "rrSeatVentilationState" in payload
    assert "lrThirdHeatState" in payload


def test_seat_climate_params_coerces_string_inputs() -> None:
    params = SeatClimateParams(
        main_heat="1",
        lr_seat_ventilation_state="2",
        steering_wheel_heat_state="3",
    )
    payload = params.to_control_params_map()
    assert payload["mainHeat"] == 1
    assert payload["lrSeatVentilationState"] == 2
    assert payload["steeringWheelHeatState"] == 3


def test_seat_climate_params_rejects_invalid_levels() -> None:
    with pytest.raises(ValueError):
        SeatClimateParams(main_heat=4)

    with pytest.raises(ValueError):
        SeatClimateParams(steering_wheel_heat_state=4)


def test_seat_climate_driver_heat_high_matches_api_dump() -> None:
    """Verify output matches real API dump: driver seat heating on, most powerful."""
    params = SeatClimateParams(
        chair_type="1",
        main_heat=1,  # high (most powerful)
        main_ventilation=0,
        copilot_heat=3,  # off
        copilot_ventilation=0,
        lr_seat_heat_state=0,
        lr_seat_ventilation_state=0,
        lr_third_heat_state=0,
        lr_third_ventilation_state=0,
        rr_seat_heat_state=0,
        rr_seat_ventilation_state=0,
        rr_third_heat_state=0,
        rr_third_ventilation_state=0,
        steering_wheel_heat_state=3,  # off
        remote_mode=1,
    )
    payload = params.to_control_params_map()
    assert payload == {
        "chairType": "1",
        "copilotHeat": 3,
        "copilotVentilation": 0,
        "lrSeatHeatState": 0,
        "lrSeatVentilationState": 0,
        "lrThirdHeatState": 0,
        "lrThirdVentilationState": 0,
        "mainHeat": 1,
        "mainVentilation": 0,
        "remoteMode": 1,
        "rrSeatHeatState": 0,
        "rrSeatVentilationState": 0,
        "rrThirdHeatState": 0,
        "rrThirdVentilationState": 0,
        "steeringWheelHeatState": 3,
    }


def test_seat_climate_steering_wheel_on_matches_api_dump() -> None:
    """Verify output matches real API dump: steering wheel heating on."""
    params = SeatClimateParams(
        chair_type="5",
        main_heat=3,
        main_ventilation=0,
        copilot_heat=3,
        copilot_ventilation=0,
        lr_seat_heat_state=0,
        lr_seat_ventilation_state=0,
        lr_third_heat_state=0,
        lr_third_ventilation_state=0,
        rr_seat_heat_state=0,
        rr_seat_ventilation_state=0,
        rr_third_heat_state=0,
        rr_third_ventilation_state=0,
        steering_wheel_heat_state=1,  # on
        remote_mode=1,
    )
    payload = params.to_control_params_map()
    assert payload["chairType"] == "5"
    assert payload["steeringWheelHeatState"] == 1


def test_seat_climate_with_change_sets_chair_type() -> None:
    """with_change() should auto-set chairType for the changed parameter."""
    base = SeatClimateParams.from_current_state()
    updated = base.with_change("main_heat", 1)
    assert updated.chair_type == "1"
    assert updated.main_heat == 1

    updated2 = base.with_change("copilot_heat", 2)
    assert updated2.chair_type == "2"

    updated3 = base.with_change("steering_wheel_heat_state", 1)
    assert updated3.chair_type == "5"


# ------------------------------------------------------------------
# Build control inner
# ------------------------------------------------------------------


def test_build_control_inner_serializes_control_params_map_as_json_string() -> None:
    config = BydConfig(username="user@example.com", password="secret", country_code="NL")
    inner = _build_control_inner(
        config,
        vin="TESTVIN",
        command=RemoteCommand.START_CLIMATE,
        control_params={"mainSettingTemp": 7, "timeSpan": 1},
        command_pwd="ABCDEF",
    )
    assert inner["commandType"] == RemoteCommand.START_CLIMATE.value
    assert inner["commandPwd"] == "ABCDEF"
    assert isinstance(inner["controlParamsMap"], str)
    assert json.loads(inner["controlParamsMap"]) == {"mainSettingTemp": 7, "timeSpan": 1}


# ------------------------------------------------------------------
# RemoteControlResult – ``res`` field mapping
# ------------------------------------------------------------------


def test_remote_control_result_res_1_is_pending() -> None:
    """res=1 from HTTP poll means 'in progress' → PENDING, not FAILURE."""
    result = RemoteControlResult.model_validate({"res": 1})
    assert result.control_state == ControlState.PENDING
    assert result.success is False


def test_remote_control_result_res_2_is_success() -> None:
    """res=2 from MQTT or final poll means 'success'."""
    result = RemoteControlResult.model_validate({"res": 2})
    assert result.control_state == ControlState.SUCCESS
    assert result.success is True


def test_remote_control_result_res_2_with_message() -> None:
    """Full MQTT ack shape: res=2 with message and requestSerial."""
    result = RemoteControlResult.model_validate(
        {
            "res": 2,
            "message": "Setting for driver seat is successful",
            "requestSerial": "88FD061561D849129D06DF77C6A598A3",
        }
    )
    assert result.control_state == ControlState.SUCCESS
    assert result.success is True
    assert result.request_serial == "88FD061561D849129D06DF77C6A598A3"


def test_remote_control_result_res_other_is_failure() -> None:
    """Any res value other than 1 or 2 should map to FAILURE."""
    result = RemoteControlResult.model_validate({"res": 0})
    assert result.control_state == ControlState.FAILURE
    assert result.success is False

    result3 = RemoteControlResult.model_validate({"res": 3})
    assert result3.control_state == ControlState.FAILURE
    assert result3.success is False


def test_remote_control_result_control_state_standard() -> None:
    """Standard controlState format (from non-res responses)."""
    pending = RemoteControlResult.model_validate({"controlState": 0})
    assert pending.control_state == ControlState.PENDING
    assert pending.success is False

    success = RemoteControlResult.model_validate({"controlState": 1})
    assert success.control_state == ControlState.SUCCESS
    assert success.success is True

    failure = RemoteControlResult.model_validate({"controlState": 2})
    assert failure.control_state == ControlState.FAILURE
    assert failure.success is False


def test_remote_control_result_empty_payload() -> None:
    """Empty payload defaults to PENDING."""
    result = RemoteControlResult.model_validate({})
    assert result.control_state == ControlState.PENDING
    assert result.success is False


# ------------------------------------------------------------------
# _is_remote_control_ready – terminal state detection
# ------------------------------------------------------------------


def test_is_remote_control_ready_res_1_not_ready() -> None:
    """res=1 (in progress) should NOT be considered ready."""
    assert _is_remote_control_ready({"res": 1}) is False


def test_is_remote_control_ready_res_2_is_ready() -> None:
    """res=2 (success) is a terminal state."""
    assert _is_remote_control_ready({"res": 2}) is True


def test_is_remote_control_ready_res_3_is_ready() -> None:
    """res=3+ (failure) is also terminal."""
    assert _is_remote_control_ready({"res": 3}) is True


def test_is_remote_control_ready_control_state_nonzero() -> None:
    """controlState != 0 is ready."""
    assert _is_remote_control_ready({"controlState": 1}) is True
    assert _is_remote_control_ready({"controlState": 2}) is True


def test_is_remote_control_ready_control_state_zero_not_ready() -> None:
    """controlState=0 (pending) is not ready."""
    assert _is_remote_control_ready({"controlState": 0}) is False


def test_is_remote_control_ready_empty_not_ready() -> None:
    """Empty dict is not ready."""
    assert _is_remote_control_ready({}) is False
