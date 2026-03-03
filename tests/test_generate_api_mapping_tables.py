"""Tests for scripts/generate_api_mapping_tables.py helper behavior."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from pybyd.exceptions import BydTransportError
from pybyd.models.charging import ChargingStatus
from pybyd.models.gps import GpsInfo
from pybyd.models.realtime import VehicleRealtimeData


def _load_script_module() -> ModuleType:
    script_path = Path(__file__).resolve().parent.parent / "scripts" / "generate_api_mapping_tables.py"
    spec = importlib.util.spec_from_file_location("generate_api_mapping_tables", script_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_realtime_table_includes_enum_domain_lines() -> None:
    mod = _load_script_module()

    realtime = VehicleRealtimeData.model_validate(
        {
            "chargingState": 1,
            "leftFrontDoor": 0,
            "abs": 0,
        }
    )

    endpoint = mod.EndpointSpec("realtime", "Realtime")
    table = mod._endpoint_table(endpoint, realtime)

    assert "| chargingState | 1 |" in table
    assert "1 → ChargingState.CHARGING" in table
    assert "Enum ChargingState:" in table
    assert "0 → ChargingState.NOT_CHARGING" in table
    assert "15 → ChargingState.CONNECTED" in table


def test_unparsed_raw_key_is_marked_raw_only() -> None:
    mod = _load_script_module()

    realtime = VehicleRealtimeData.model_validate(
        {
            "leftFrontDoor": 1,
            "totallyUnknownRawField": 123,
        }
    )

    endpoint = mod.EndpointSpec("realtime", "Realtime")
    table = mod._endpoint_table(endpoint, realtime)

    assert "| totallyUnknownRawField | 123 | Not parsed (raw only) |" in table


def test_sensitive_fields_are_redacted() -> None:
    mod = _load_script_module()

    realtime = VehicleRealtimeData.model_validate(
        {
            "vin": "LNBXX1234567890",
            "requestSerial": "abcd-serial-123",
            "speed": 0,
        }
    )

    endpoint = mod.EndpointSpec("realtime", "Realtime")
    table = mod._endpoint_table(endpoint, realtime)

    assert "LNBXX1234567890" not in table
    assert "abcd-serial-123" not in table
    assert "<REDACTED>" in table


def test_transport_error_format_includes_status_and_endpoint() -> None:
    mod = _load_script_module()

    exc = BydTransportError("Not Found", status_code=404, endpoint="/control/getGpsInfo")
    message = mod._format_endpoint_error(exc)

    assert "status=404" in message
    assert "/control/getGpsInfo" in message


def test_endpoint_error_redacts_vin_and_timestamp() -> None:
    mod = _load_script_module()

    api_exc = Exception("/vehicleInfo/vehicle/getEnergyConsumption not supported for VIN LC0CF4CD7N1000375 (code=1001)")
    api_msg = mod._format_endpoint_error(api_exc)
    assert "LC0CF4CD7N1000375" not in api_msg
    assert "<REDACTED>" in api_msg

    transport_exc = BydTransportError(
        'HTTP 404 from /app/push/getPushSwitchState: {"timestamp":1772529876669,"path":"/push/getPushSwitchState"}',
        status_code=404,
        endpoint="/app/push/getPushSwitchState",
    )
    transport_msg = mod._format_endpoint_error(transport_exc)
    assert "1772529876669" not in transport_msg
    assert '"timestamp":"<REDACTED>"' in transport_msg


def test_update_time_is_redacted() -> None:
    mod = _load_script_module()

    charging = ChargingStatus.model_validate(
        {
            "vin": "LNBX123456789",
            "updateTime": 1735689600,
        }
    )

    endpoint = mod.EndpointSpec("charging", "Charging")
    table = mod._endpoint_table(endpoint, charging)

    assert "1735689600" not in table
    assert "updateTime" in table
    assert "<REDACTED>" in table


def test_gps_sensitive_rows_show_mapping_without_values() -> None:
    mod = _load_script_module()

    gps = GpsInfo.model_validate(
        {
            "data": {
                "latitude": 52.123456,
                "longitude": 4.987654,
                "gpsTimeStamp": 1735689600,
            },
            "requestSerial": "abc-serial-123",
        }
    )

    endpoint = mod.EndpointSpec("gps", "GPS")
    table = mod._endpoint_table(endpoint, gps)

    assert "52.123456" not in table
    assert "4.987654" not in table
    assert "abc-serial-123" not in table
    assert "<REDACTED> → latitude (float \\| NoneType)" in table
    assert "<REDACTED> → longitude (float \\| NoneType)" in table
