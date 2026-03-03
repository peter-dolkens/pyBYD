from __future__ import annotations

# pylint: disable=redefined-outer-name
import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from pybyd._api.control import _fetch_control_endpoint, verify_control_password
from pybyd._constants import SESSION_EXPIRED_CODES
from pybyd._mqtt import MqttEvent
from pybyd.client import BydClient
from pybyd.config import BydConfig
from pybyd.exceptions import BydApiError, BydAuthenticationError, BydRemoteControlError
from pybyd.models.charging import ChargingStatus
from pybyd.models.control import RemoteCommand
from pybyd.models.realtime import VehicleRealtimeData
from pybyd.models.realtime import VehicleState as RealtimeVehicleState
from pybyd.models.smart_charging import SmartChargingSchedule
from pybyd.session import Session


def _patch_common_client_monkeypatches(
    monkeypatch: pytest.MonkeyPatch,
    *,
    backend: Any,
    decrypt_targets: list[str],
) -> None:
    async def fake_post_secure(_self: Any, endpoint: str, outer_payload: dict[str, Any]) -> dict[str, Any]:
        return await backend.post_secure(endpoint, outer_payload)

    async def fake_async_load_tables(_self: Any) -> None:
        return None

    def fake_mqtt_start(self: Any, _bootstrap: Any) -> None:
        self._running = True

    def fake_mqtt_stop(self: Any) -> None:
        self._running = False

    def identity_decrypt(payload: str, _key: str) -> str:
        return payload

    monkeypatch.setattr("pybyd._transport.SecureTransport.post_secure", fake_post_secure)
    monkeypatch.setattr("pybyd._crypto.bangcle.BangcleCodec.async_load_tables", fake_async_load_tables)
    monkeypatch.setattr("pybyd._mqtt.BydMqttRuntime.start", fake_mqtt_start)
    monkeypatch.setattr("pybyd._mqtt.BydMqttRuntime.stop", fake_mqtt_stop)

    for target in decrypt_targets:
        monkeypatch.setattr(f"{target}.aes_decrypt_utf8", identity_decrypt, raising=False)


@pytest.fixture
def config() -> BydConfig:
    return BydConfig(
        username="user@example.com",
        password="secret",
        control_pin="123456",
        mqtt_enabled=True,
        mqtt_timeout=0.2,
    )


# ---------------------------------------------------------------------------
# Fake backend for full end-to-end tests
# ---------------------------------------------------------------------------


@dataclass
class FakeBydBackend:
    vin: str = "VIN-E2E-123"
    calls: dict[str, int] = field(default_factory=dict)
    login_should_fail: bool = False
    energy_error_code: str | None = None
    expire_once_endpoints: set[str] = field(default_factory=set)
    _expired_already: set[str] = field(default_factory=set)
    emit_mqtt_remote_result: bool = True
    mqtt_event_handler: Callable[[MqttEvent], None] | None = None

    def _record_call(self, endpoint: str) -> None:
        self.calls[endpoint] = self.calls.get(endpoint, 0) + 1

    def _code_zero(self, respond_data: Any) -> dict[str, Any]:
        return {"code": "0", "respondData": json.dumps(respond_data)}

    def _maybe_emit_remote_control_event(self) -> None:
        if not self.emit_mqtt_remote_result or self.mqtt_event_handler is None:
            return
        event = MqttEvent(
            event="remoteControl",
            vin=self.vin,
            topic="oversea/res/user-1",
            payload={"data": {"uuid": "CMD-1", "respondData": {"res": 2, "message": "ok"}}},
        )
        asyncio.get_running_loop().call_later(0.01, self.mqtt_event_handler, event)

    async def post_secure(self, endpoint: str, _outer_payload: dict[str, Any]) -> dict[str, Any]:
        self._record_call(endpoint)

        if endpoint in self.expire_once_endpoints and endpoint not in self._expired_already:
            self._expired_already.add(endpoint)
            return {"code": "1005", "message": "session expired"}

        if endpoint == "/app/account/login":
            if self.login_should_fail:
                return {"code": "5000", "message": "invalid credentials"}
            return self._code_zero(
                {
                    "token": {
                        "userId": "user-1",
                        "signToken": "sign-token-1",
                        "encryToken": "encrypt-token-1",
                    }
                }
            )

        if endpoint == "/app/account/getAllListByUserId":
            return self._code_zero(
                [
                    {
                        "vin": self.vin,
                        "modelName": "SEAL U",
                        "brandName": "BYD",
                        "energyType": "EV",
                        "autoAlias": "My BYD",
                        "rangeDetailList": [{"code": "2", "name": "Control", "childList": []}],
                    }
                ]
            )

        if endpoint == "/vehicleInfo/vehicle/vehicleRealTimeRequest":
            return self._code_zero({"requestSerial": "RT-1", "onlineState": 2})

        if endpoint == "/vehicleInfo/vehicle/vehicleRealTimeResult":
            return self._code_zero(
                {
                    "requestSerial": "RT-1",
                    "onlineState": 1,
                    "time": 1_771_000_000,
                    "elecPercent": 84,
                    "speed": 0,
                    "leftFrontDoorLock": 2,
                    "rightFrontDoorLock": 2,
                }
            )

        if endpoint == "/control/getGpsInfo":
            return self._code_zero({"requestSerial": "GPS-1"})

        if endpoint == "/control/getGpsInfoResult":
            return self._code_zero(
                {
                    "requestSerial": "GPS-1",
                    "latitude": 52.3676,
                    "longitude": 4.9041,
                    "gpsTimeStamp": 1_771_000_001,
                }
            )

        if endpoint == "/vehicleInfo/vehicle/getEnergyConsumption":
            if self.energy_error_code is not None:
                return {"code": self.energy_error_code, "message": "energy error"}
            return self._code_zero(
                {
                    "vin": self.vin,
                    "totalEnergy": "13.5",
                    "avgEnergyConsumption": "14.2",
                    "electricityConsumption": "11.8",
                    "fuelConsumption": "0",
                }
            )

        if endpoint == "/control/getStatusNow":
            return self._code_zero(
                {
                    "statusNow": {
                        "acSwitch": 1,
                        "status": 1,
                        "cycleChoice": 2,
                        "mainSettingTemp": 7,
                        "mainSettingTempNew": 21.0,
                    }
                }
            )

        if endpoint == "/control/smartCharge/homePage":
            return self._code_zero(
                {
                    "vin": self.vin,
                    "soc": 84,
                    "chargingState": 15,
                    "connectState": 1,
                    "waitStatus": 0,
                    "fullHour": 1,
                    "fullMinute": 20,
                    "updateTime": 1_771_000_002,
                }
            )

        if endpoint == "/vehicle/vehicleswitch/verifyControlPassword":
            return self._code_zero({"ok": True})

        if endpoint == "/app/emqAuth/getEmqBrokerIp":
            return self._code_zero({"emqBorker": "mqtt.example.com:8883"})

        if endpoint == "/control/remoteControl":
            self._maybe_emit_remote_control_event()
            return self._code_zero({"controlState": 0, "requestSerial": "CMD-1"})

        if endpoint == "/control/remoteControlResult":
            return self._code_zero({"controlState": 1, "requestSerial": "CMD-1"})

        if endpoint == "/control/smartCharge/changeChargeStatue":
            return self._code_zero({"result": "ok"})

        if endpoint == "/control/smartCharge/saveOrUpdate":
            return self._code_zero({"result": "ok"})

        if endpoint == "/control/vehicle/modifyAutoAlias":
            return self._code_zero({"result": "ok"})

        if endpoint == "/app/push/getPushSwitchState":
            return self._code_zero({"pushSwitch": 1})

        if endpoint == "/app/push/setPushSwitchState":
            return self._code_zero({"result": "ok"})

        raise AssertionError(f"Unexpected endpoint in fake backend: {endpoint}")


@pytest.fixture
def e2e_backend(monkeypatch: pytest.MonkeyPatch) -> FakeBydBackend:
    backend = FakeBydBackend()
    _patch_common_client_monkeypatches(
        monkeypatch,
        backend=backend,
        decrypt_targets=[
            "pybyd._api._common",
            "pybyd._api.login",
            "pybyd._api.realtime",
            "pybyd._api.gps",
            "pybyd._api.energy",
            "pybyd._api.hvac",
            "pybyd._api.charging",
            "pybyd._mqtt",
        ],
    )
    return backend


# ---------------------------------------------------------------------------
# End-to-end tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_e2e_client_happy_path_exercises_full_library(config: BydConfig, e2e_backend: FakeBydBackend) -> None:
    e2e_backend.expire_once_endpoints.add("/app/account/getAllListByUserId")

    async with BydClient(config) as client:
        e2e_backend.mqtt_event_handler = client._on_mqtt_event

        vehicles = await client.get_vehicles()
        assert len(vehicles) == 1
        vin = vehicles[0].vin
        assert vin == e2e_backend.vin

        realtime = await client.get_vehicle_realtime(vin, poll_attempts=1, poll_interval=0, mqtt_timeout=0)
        assert realtime.elec_percent == 84

        gps = await client.get_gps_info(vin, poll_attempts=1, poll_interval=0)
        assert gps.latitude == pytest.approx(52.3676)

        energy = await client.get_energy_consumption(vin)
        assert energy.avg_energy_consumption == pytest.approx(14.2)

        hvac = await client.get_hvac_status(vin)
        assert hvac is not None
        assert hvac.main_setting_temp == 7

        charging = await client.get_charging_status(vin)
        assert charging is not None
        assert charging.soc == 84

        verify = await client.verify_control_password(vin)
        assert verify.vin == vin
        assert verify.ok is True

        lock_result = await client.lock(vin)
        assert lock_result.success is True

        assert client._mqtt_runtime is not None
        assert client._mqtt_runtime.is_running is True

    assert e2e_backend.calls.get("/app/account/login", 0) == 2
    assert e2e_backend.calls.get("/app/emqAuth/getEmqBrokerIp", 0) >= 1
    assert e2e_backend.calls.get("/vehicle/vehicleswitch/verifyControlPassword", 0) == 1
    assert e2e_backend.calls.get("/control/remoteControlResult", 0) == 0


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_e2e_login_error_raises_authentication(config: BydConfig, e2e_backend: FakeBydBackend) -> None:
    e2e_backend.login_should_fail = True

    async with BydClient(config) as client:
        with pytest.raises(BydAuthenticationError):
            await client.login()


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_e2e_api_error_raises_byd_api_error(config: BydConfig, e2e_backend: FakeBydBackend) -> None:
    e2e_backend.energy_error_code = "9999"

    async with BydClient(config) as client:
        vehicles = await client.get_vehicles()
        vin = vehicles[0].vin
        with pytest.raises(BydApiError, match="getEnergyConsumption"):
            await client.get_energy_consumption(vin)


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_e2e_remote_control_falls_back_to_poll_when_mqtt_times_out(
    config: BydConfig,
    e2e_backend: FakeBydBackend,
) -> None:
    e2e_backend.emit_mqtt_remote_result = False
    fallback_config = BydConfig(
        username=config.username,
        password=config.password,
        control_pin=config.control_pin,
        mqtt_enabled=True,
        mqtt_timeout=0.01,
    )

    async with BydClient(fallback_config) as client:
        vehicles = await client.get_vehicles()
        vin = vehicles[0].vin
        result = await client.lock(vin)
        assert result.success is True

    assert e2e_backend.calls.get("/control/remoteControlResult", 0) == 1


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_client_full_workflow(config: BydConfig, e2e_backend: FakeBydBackend) -> None:
    async with BydClient(config) as client:
        e2e_backend.mqtt_event_handler = client._on_mqtt_event

        await client.login()
        vehicles = await client.get_vehicles()
        assert vehicles
        vin = vehicles[0].vin

        realtime = await client.get_vehicle_realtime(vin, poll_attempts=2, poll_interval=0.01, mqtt_timeout=0)
        assert realtime.elec_percent is not None

        gps = await client.get_gps_info(vin, poll_attempts=2, poll_interval=0.01)
        assert gps.latitude == 52.3676

        energy = await client.get_energy_consumption(vin)
        assert energy.total_energy == 13.5

        hvac = await client.get_hvac_status(vin)
        assert hvac.is_ac_on is True

        charging = await client.get_charging_status(vin)
        assert charging.soc == 84

        push_state = await client.get_push_state(vin)
        assert push_state.is_enabled is True
        await client.set_push_state(vin, enable=False)

        verify = await client.verify_control_password(vin)
        assert verify.vin == vin
        result = await client.lock(vin)
        assert result.success is True

        await client.toggle_smart_charging(vin, enable=True)
        schedule = SmartChargingSchedule(
            vin=vin,
            target_soc=80,
            start_hour=1,
            start_minute=0,
            end_hour=6,
            end_minute=0,
            smart_charge_switch=1,
            raw={},
        )
        await client.save_charging_schedule(vin, schedule)
        await client.rename_vehicle(vin, name="New Name")


# ---------------------------------------------------------------------------
# Unit tests: control error mapping & password verify
# ---------------------------------------------------------------------------


class _ErrorTransport:
    def __init__(self, code: str, message: str = "") -> None:
        self._code = code
        self._message = message

    async def post_secure(self, _endpoint: str, _payload: dict[str, object]) -> dict[str, object]:
        return {"code": self._code, "message": self._message}


def _make_session() -> Session:
    return Session(
        user_id="user-1",
        sign_token="sign-token-1",
        encry_token="encry-token-1",
        ttl=3600,
    )


@pytest.mark.asyncio
async def test_remote_control_1009_raises_remote_control_error(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = BydConfig(username="user@example.com", password="secret")
    session = _make_session()

    monkeypatch.setattr(
        "pybyd._api._common.build_token_outer_envelope",
        lambda *_args, **_kwargs: ({"encryData": "ignored"}, "dummy-key"),
    )

    with pytest.raises(BydRemoteControlError) as exc_info:
        await _fetch_control_endpoint(
            "/control/remoteControl",
            cfg,
            session,
            _ErrorTransport("1009", "Dienstfehler(1009)"),
            "VIN-E2E-123",
            RemoteCommand.LOCK,
        )

    exc = exc_info.value
    assert exc.code == "1009"
    assert exc.endpoint == "/control/remoteControl"


@pytest.mark.asyncio
async def test_non_remote_endpoint_1009_stays_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = BydConfig(username="user@example.com", password="secret")
    session = _make_session()

    monkeypatch.setattr(
        "pybyd._api._common.build_token_outer_envelope",
        lambda *_args, **_kwargs: ({"encryData": "ignored"}, "dummy-key"),
    )

    with pytest.raises(BydApiError) as exc_info:
        await _fetch_control_endpoint(
            "/vehicle/someOtherEndpoint",
            cfg,
            session,
            _ErrorTransport("1009", "Dienstfehler(1009)"),
            "VIN-E2E-123",
            RemoteCommand.LOCK,
        )

    exc = exc_info.value
    assert not isinstance(exc, BydRemoteControlError)
    assert exc.code == "1009"
    assert exc.endpoint == "/vehicle/someOtherEndpoint"


class _FakeTransport:
    async def post_secure(self, endpoint: str, _outer_payload: dict[str, object]) -> dict[str, object]:
        assert endpoint == "/vehicle/vehicleswitch/verifyControlPassword"
        return {"code": "0", "respondData": "ciphertext"}


@pytest.mark.asyncio
async def test_verify_control_password_accepts_empty_decrypted_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = BydConfig(username="user@example.com", password="secret", control_pin="123456")
    session = Session(
        user_id="user-1",
        sign_token="sign-token-1",
        encry_token="encry-token-1",
        ttl=3600,
    )

    monkeypatch.setattr("pybyd._api._common.aes_decrypt_utf8", lambda _value, _key: "")

    result = await verify_control_password(
        cfg,
        session,
        _FakeTransport(),
        "VIN-E2E-123",
        "E10ADC3949BA59ABBE56E057F20F883E",
    )

    assert result.vin == "VIN-E2E-123"
    assert result.ok is None


# ---------------------------------------------------------------------------
# Unit tests: normalization and constants
# ---------------------------------------------------------------------------


def test_session_expired_codes_include_1002() -> None:
    assert "1002" in SESSION_EXPIRED_CODES


def test_realtime_negative_charge_times_are_stripped() -> None:
    """BYD sends -1 for charge times when not applicable — cleaned to None."""
    realtime = VehicleRealtimeData.model_validate(
        {
            "vin": "VIN123",
            "fullHour": -1,
            "fullMinute": -1,
            "remainingHours": -1,
            "remainingMinutes": -1,
        }
    )

    assert realtime.full_hour is None
    assert realtime.full_minute is None
    assert realtime.remaining_hours is None
    assert realtime.remaining_minutes is None


def test_realtime_vehicle_state_mapping_on_and_off() -> None:
    on_realtime = VehicleRealtimeData.model_validate({"vehicleState": 0})
    off_realtime = VehicleRealtimeData.model_validate({"vehicleState": 2})

    assert on_realtime.vehicle_state == RealtimeVehicleState.ON
    assert off_realtime.vehicle_state == RealtimeVehicleState.OFF


def test_charging_status_update_datetime_seconds() -> None:
    status = ChargingStatus(
        vin="VIN123",
        soc=None,
        charging_state=None,
        connect_state=None,
        wait_status=None,
        full_hour=None,
        full_minute=None,
        update_time=datetime.fromtimestamp(1_770_928_447, tz=UTC),
        raw={},
    )

    assert status.update_time == datetime.fromtimestamp(1_770_928_447, tz=UTC)


def test_charging_status_update_datetime_milliseconds() -> None:
    """Millisecond epoch values are normalised to seconds during parsing."""
    data = ChargingStatus.model_validate(
        {
            "vin": "VIN123",
            "updateTime": 1_770_928_447_000,
        }
    )

    assert data.update_time == datetime.fromtimestamp(1_770_928_447, tz=UTC)


# ---------------------------------------------------------------------------
# Focused endpoint tests: push notifications
# ---------------------------------------------------------------------------


@dataclass
class FakePushNotificationsBackend:
    vin: str = "VIN-PN-TEST"
    calls: dict[str, int] = field(default_factory=dict)
    get_error_code: str | None = None
    set_error_code: str | None = None

    def _record_call(self, endpoint: str) -> None:
        self.calls[endpoint] = self.calls.get(endpoint, 0) + 1

    def _code_zero(self, respond_data: Any) -> dict[str, Any]:
        return {"code": "0", "respondData": json.dumps(respond_data)}

    async def post_secure(self, endpoint: str, _outer_payload: dict[str, Any]) -> dict[str, Any]:
        self._record_call(endpoint)

        if endpoint == "/app/account/login":
            return self._code_zero(
                {
                    "token": {
                        "userId": "user-1",
                        "signToken": "sign-token-1",
                        "encryToken": "encrypt-token-1",
                    }
                }
            )

        if endpoint == "/app/emqAuth/getEmqBrokerIp":
            return self._code_zero({"emqBorker": "mqtt.example.com:8883"})

        if endpoint == "/app/push/getPushSwitchState":
            if self.get_error_code is not None:
                return {"code": self.get_error_code, "message": "error"}
            return self._code_zero({"pushSwitch": 1})

        if endpoint == "/app/push/setPushSwitchState":
            if self.set_error_code is not None:
                return {"code": self.set_error_code, "message": "error"}
            return self._code_zero({"result": "ok"})

        raise AssertionError(f"Unexpected endpoint: {endpoint}")


@pytest.fixture
def push_backend(monkeypatch: pytest.MonkeyPatch) -> FakePushNotificationsBackend:
    backend = FakePushNotificationsBackend()
    _patch_common_client_monkeypatches(
        monkeypatch,
        backend=backend,
        decrypt_targets=["pybyd._api.login", "pybyd._api._common", "pybyd._mqtt"],
    )
    return backend


@pytest.mark.asyncio
async def test_get_push_state(config: BydConfig, push_backend: FakePushNotificationsBackend) -> None:
    async with BydClient(config) as client:
        state = await client.get_push_state(push_backend.vin)
        assert state.is_enabled is True
        assert state.push_switch == 1
        assert state.vin == push_backend.vin
    assert push_backend.calls.get("/app/push/getPushSwitchState", 0) == 1


@pytest.mark.asyncio
async def test_get_push_state_api_error(config: BydConfig, push_backend: FakePushNotificationsBackend) -> None:
    push_backend.get_error_code = "9999"
    async with BydClient(config) as client:
        with pytest.raises(BydApiError, match="getPushSwitchState"):
            await client.get_push_state(push_backend.vin)


@pytest.mark.asyncio
async def test_set_push_state_enable(config: BydConfig, push_backend: FakePushNotificationsBackend) -> None:
    async with BydClient(config) as client:
        result = await client.set_push_state(push_backend.vin, enable=True)
        assert result.vin == push_backend.vin
        assert result.result == "ok"
        assert result.raw == {"result": "ok"}
    assert push_backend.calls.get("/app/push/setPushSwitchState", 0) == 1


@pytest.mark.asyncio
async def test_set_push_state_disable(config: BydConfig, push_backend: FakePushNotificationsBackend) -> None:
    async with BydClient(config) as client:
        result = await client.set_push_state(push_backend.vin, enable=False)
        assert result.vin == push_backend.vin
        assert result.result == "ok"
        assert result.raw == {"result": "ok"}


@pytest.mark.asyncio
async def test_set_push_state_api_error(config: BydConfig, push_backend: FakePushNotificationsBackend) -> None:
    push_backend.set_error_code = "9999"
    async with BydClient(config) as client:
        with pytest.raises(BydApiError, match="setPushSwitchState"):
            await client.set_push_state(push_backend.vin, enable=True)


# ---------------------------------------------------------------------------
# Focused endpoint tests: smart charging
# ---------------------------------------------------------------------------


@dataclass
class FakeSmartChargingBackend:
    vin: str = "VIN-SC-TEST"
    calls: dict[str, int] = field(default_factory=dict)
    toggle_error_code: str | None = None
    save_error_code: str | None = None

    def _record_call(self, endpoint: str) -> None:
        self.calls[endpoint] = self.calls.get(endpoint, 0) + 1

    def _code_zero(self, respond_data: Any) -> dict[str, Any]:
        return {"code": "0", "respondData": json.dumps(respond_data)}

    async def post_secure(self, endpoint: str, _outer_payload: dict[str, Any]) -> dict[str, Any]:
        self._record_call(endpoint)

        if endpoint == "/app/account/login":
            return self._code_zero(
                {
                    "token": {
                        "userId": "user-1",
                        "signToken": "sign-token-1",
                        "encryToken": "encrypt-token-1",
                    }
                }
            )

        if endpoint == "/app/emqAuth/getEmqBrokerIp":
            return self._code_zero({"emqBorker": "mqtt.example.com:8883"})

        if endpoint == "/control/smartCharge/changeChargeStatue":
            if self.toggle_error_code is not None:
                return {"code": self.toggle_error_code, "message": "error"}
            return self._code_zero({"result": "ok"})

        if endpoint == "/control/smartCharge/saveOrUpdate":
            if self.save_error_code is not None:
                return {"code": self.save_error_code, "message": "error"}
            return self._code_zero({"result": "ok"})

        raise AssertionError(f"Unexpected endpoint: {endpoint}")


@pytest.fixture
def smart_charging_backend(monkeypatch: pytest.MonkeyPatch) -> FakeSmartChargingBackend:
    backend = FakeSmartChargingBackend()
    _patch_common_client_monkeypatches(
        monkeypatch,
        backend=backend,
        decrypt_targets=["pybyd._api.login", "pybyd._api._common", "pybyd._mqtt"],
    )
    return backend


@pytest.mark.asyncio
async def test_toggle_smart_charging_enable(
    config: BydConfig,
    smart_charging_backend: FakeSmartChargingBackend,
) -> None:
    async with BydClient(config) as client:
        result = await client.toggle_smart_charging(smart_charging_backend.vin, enable=True)
        assert result.vin == smart_charging_backend.vin
        assert result.result == "ok"
        assert result.raw == {"result": "ok"}
    assert smart_charging_backend.calls.get("/control/smartCharge/changeChargeStatue", 0) == 1


@pytest.mark.asyncio
async def test_toggle_smart_charging_disable(
    config: BydConfig,
    smart_charging_backend: FakeSmartChargingBackend,
) -> None:
    async with BydClient(config) as client:
        result = await client.toggle_smart_charging(smart_charging_backend.vin, enable=False)
        assert result.vin == smart_charging_backend.vin
        assert result.result == "ok"
        assert result.raw == {"result": "ok"}


@pytest.mark.asyncio
async def test_toggle_smart_charging_api_error(
    config: BydConfig,
    smart_charging_backend: FakeSmartChargingBackend,
) -> None:
    smart_charging_backend.toggle_error_code = "9999"
    async with BydClient(config) as client:
        with pytest.raises(BydApiError, match="changeChargeStatue"):
            await client.toggle_smart_charging(smart_charging_backend.vin, enable=True)


@pytest.mark.asyncio
async def test_save_charging_schedule(config: BydConfig, smart_charging_backend: FakeSmartChargingBackend) -> None:
    schedule = SmartChargingSchedule(
        vin=smart_charging_backend.vin,
        target_soc=80,
        start_hour=22,
        start_minute=0,
        end_hour=6,
        end_minute=0,
        smart_charge_switch=1,
        raw={},
    )
    async with BydClient(config) as client:
        result = await client.save_charging_schedule(smart_charging_backend.vin, schedule)
        assert result.vin == smart_charging_backend.vin
        assert result.result == "ok"
        assert result.raw == {"result": "ok"}
    assert smart_charging_backend.calls.get("/control/smartCharge/saveOrUpdate", 0) == 1


@pytest.mark.asyncio
async def test_save_charging_schedule_api_error(
    config: BydConfig,
    smart_charging_backend: FakeSmartChargingBackend,
) -> None:
    smart_charging_backend.save_error_code = "9999"
    schedule = SmartChargingSchedule(
        vin=smart_charging_backend.vin,
        target_soc=80,
        start_hour=22,
        start_minute=0,
        end_hour=6,
        end_minute=0,
        smart_charge_switch=1,
        raw={},
    )
    async with BydClient(config) as client:
        with pytest.raises(BydApiError, match="saveOrUpdate"):
            await client.save_charging_schedule(smart_charging_backend.vin, schedule)


# ---------------------------------------------------------------------------
# Focused endpoint tests: vehicle settings
# ---------------------------------------------------------------------------


@dataclass
class FakeVehicleSettingsBackend:
    vin: str = "VIN-VS-TEST"
    calls: dict[str, int] = field(default_factory=dict)
    rename_error_code: str | None = None

    def _record_call(self, endpoint: str) -> None:
        self.calls[endpoint] = self.calls.get(endpoint, 0) + 1

    def _code_zero(self, respond_data: Any) -> dict[str, Any]:
        return {"code": "0", "respondData": json.dumps(respond_data)}

    async def post_secure(self, endpoint: str, _outer_payload: dict[str, Any]) -> dict[str, Any]:
        self._record_call(endpoint)

        if endpoint == "/app/account/login":
            return self._code_zero(
                {
                    "token": {
                        "userId": "user-1",
                        "signToken": "sign-token-1",
                        "encryToken": "encrypt-token-1",
                    }
                }
            )

        if endpoint == "/app/emqAuth/getEmqBrokerIp":
            return self._code_zero({"emqBorker": "mqtt.example.com:8883"})

        if endpoint == "/control/vehicle/modifyAutoAlias":
            if self.rename_error_code is not None:
                return {"code": self.rename_error_code, "message": "error"}
            return self._code_zero({"result": "ok"})

        raise AssertionError(f"Unexpected endpoint: {endpoint}")


@pytest.fixture
def vehicle_settings_backend(monkeypatch: pytest.MonkeyPatch) -> FakeVehicleSettingsBackend:
    backend = FakeVehicleSettingsBackend()
    _patch_common_client_monkeypatches(
        monkeypatch,
        backend=backend,
        decrypt_targets=["pybyd._api.login", "pybyd._api._common", "pybyd._mqtt"],
    )
    return backend


@pytest.mark.asyncio
async def test_rename_vehicle(config: BydConfig, vehicle_settings_backend: FakeVehicleSettingsBackend) -> None:
    async with BydClient(config) as client:
        result = await client.rename_vehicle(vehicle_settings_backend.vin, name="My New BYD")
        assert result.vin == vehicle_settings_backend.vin
        assert result.result == "ok"
        assert result.raw == {"result": "ok"}
    assert vehicle_settings_backend.calls.get("/control/vehicle/modifyAutoAlias", 0) == 1


@pytest.mark.asyncio
async def test_rename_vehicle_api_error(
    config: BydConfig,
    vehicle_settings_backend: FakeVehicleSettingsBackend,
) -> None:
    vehicle_settings_backend.rename_error_code = "9999"
    async with BydClient(config) as client:
        with pytest.raises(BydApiError, match="modifyAutoAlias"):
            await client.rename_vehicle(vehicle_settings_backend.vin, name="My New BYD")
