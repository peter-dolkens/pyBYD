from __future__ import annotations

import asyncio

import pytest

from pybyd._mqtt import MqttEvent
from pybyd.client import _MQTT_DATA_UNAVAILABLE_CODES, BydClient
from pybyd.config import BydConfig
from pybyd.models.control import CommandAckEvent, CommandLifecycleEvent, CommandLifecycleStatus


class _DummyRuntime:
    @property
    def is_running(self) -> bool:  # pragma: no cover
        return True


@pytest.mark.asyncio
async def test_remote_control_waiter_matches_request_serial() -> None:
    config = BydConfig(username="user@example.com", password="secret", country_code="NL")
    client = BydClient(config)

    vin = "LC0CF4CD7N1000375"
    serial = "C97B51D8E15D46E589675474BA8A207A"

    # Bypass full startup; _mqtt_wait only requires a running runtime + loop.
    client._loop = asyncio.get_running_loop()  # type: ignore[attr-defined]
    client._mqtt_runtime = _DummyRuntime()  # type: ignore[attr-defined]

    waiter_task = asyncio.create_task(
        client._mqtt_wait(vin, event_type="remoteControl", serial=serial, timeout=1.0)  # type: ignore[attr-defined]
    )
    await asyncio.sleep(0)

    event = MqttEvent(
        event="remoteControl",
        vin=vin,
        topic="oversea/res/347678",
        payload={
            "event": "remoteControl",
            "vin": vin,
            "data": {
                "uuid": serial,
                "identifier": "347678",
                "respondData": {"res": 2, "message": "Unlocking successful."},
            },
        },
    )

    client._on_mqtt_event(event)  # type: ignore[attr-defined]

    raw = await waiter_task
    assert raw is not None
    assert raw["res"] == 2
    # ensure correlation id is normalised
    assert raw["requestSerial"] == serial


@pytest.mark.asyncio
async def test_remote_control_missing_serial_does_not_resolve_serial_waiters() -> None:
    config = BydConfig(username="user@example.com", password="secret", country_code="NL")
    client = BydClient(config)

    vin = "LC0CF4CD7N1000375"
    serial_1 = "SERIAL-ONE"
    serial_2 = "SERIAL-TWO"

    client._loop = asyncio.get_running_loop()  # type: ignore[attr-defined]
    client._mqtt_runtime = _DummyRuntime()  # type: ignore[attr-defined]

    task1 = asyncio.create_task(
        client._mqtt_wait(vin, event_type="remoteControl", serial=serial_1, timeout=1.0)  # type: ignore[attr-defined]
    )
    task2 = asyncio.create_task(
        client._mqtt_wait(vin, event_type="remoteControl", serial=serial_2, timeout=0.2)  # type: ignore[attr-defined]
    )
    await asyncio.sleep(0)

    # Payload with no requestSerial should not resolve requestSerial waiters.
    event = MqttEvent(
        event="remoteControl",
        vin=vin,
        topic="oversea/res/347678",
        payload={
            "event": "remoteControl",
            "vin": vin,
            "data": {"respondData": {"res": 2, "message": "OK"}},
        },
    )

    client._on_mqtt_event(event)  # type: ignore[attr-defined]

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(task1, timeout=0.05)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(task2, timeout=0.05)
    task1.cancel()
    task2.cancel()


# ---------------------------------------------------------------------------
# on_command_ack callback tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_command_ack_fires_for_genuine_remote_control() -> None:
    """on_command_ack should fire for a remoteControl event whose serial is NOT in _data_poll_serials."""
    acks: list[CommandAckEvent] = []

    def _capture_ack(event: CommandAckEvent) -> None:
        acks.append(event)

    config = BydConfig(username="user@example.com", password="secret", country_code="NL")
    client = BydClient(config, on_command_ack=_capture_ack)

    vin = "LC0CF4CD7N1000375"
    serial = "CMD-SERIAL-001"

    event = MqttEvent(
        event="remoteControl",
        vin=vin,
        topic="oversea/res/347678",
        payload={
            "event": "remoteControl",
            "vin": vin,
            "data": {
                "uuid": serial,
                "identifier": "347678",
                "respondData": {"controlState": 1, "message": "OK"},
            },
        },
    )

    client._on_mqtt_event(event)  # type: ignore[attr-defined]

    assert len(acks) == 1
    assert acks[0].vin == vin
    assert acks[0].request_serial == serial
    assert acks[0].raw_uuid == serial
    assert acks[0].is_correlated is True


@pytest.mark.asyncio
async def test_on_command_ack_suppressed_for_data_poll_serial() -> None:
    """on_command_ack must NOT fire when the serial belongs to a data poll (e.g. GPS)."""
    acks: list[CommandAckEvent] = []

    def _capture_ack(event: CommandAckEvent) -> None:
        acks.append(event)

    config = BydConfig(username="user@example.com", password="secret", country_code="NL")
    client = BydClient(config, on_command_ack=_capture_ack)

    vin = "LC0CF4CD7N1000375"
    gps_serial = "GPS-SERIAL-001"

    # Simulate _trigger_and_poll registering a data-poll serial.
    client._data_poll_serials.add(gps_serial)  # type: ignore[attr-defined]

    event = MqttEvent(
        event="remoteControl",
        vin=vin,
        topic="oversea/res/347678",
        payload={
            "event": "remoteControl",
            "vin": vin,
            "data": {
                "uuid": gps_serial,
                "identifier": "347678",
                "respondData": {"res": 2, "data": {"latitude": 63.4, "longitude": 10.4}},
            },
        },
    )

    client._on_mqtt_event(event)  # type: ignore[attr-defined]

    assert len(acks) == 0, "on_command_ack must not fire for data-poll serials"


@pytest.mark.asyncio
async def test_on_mqtt_event_still_fires_for_data_poll_serial() -> None:
    """on_mqtt_event (generic) should still fire even when on_command_ack is suppressed."""
    generic_events: list[tuple[str, str, dict[str, object]]] = []
    acks: list[CommandAckEvent] = []

    def _capture_generic(event: str, vin: str, data: dict[str, object]) -> None:
        generic_events.append((event, vin, data))

    def _capture_ack(event: CommandAckEvent) -> None:
        acks.append(event)

    config = BydConfig(username="user@example.com", password="secret", country_code="NL")
    client = BydClient(config, on_mqtt_event=_capture_generic, on_command_ack=_capture_ack)

    vin = "LC0CF4CD7N1000375"
    gps_serial = "GPS-SERIAL-002"

    # Register as data-poll serial.
    client._data_poll_serials.add(gps_serial)  # type: ignore[attr-defined]

    event = MqttEvent(
        event="remoteControl",
        vin=vin,
        topic="oversea/res/347678",
        payload={
            "event": "remoteControl",
            "vin": vin,
            "data": {
                "uuid": gps_serial,
                "respondData": {"res": 2},
            },
        },
    )

    client._on_mqtt_event(event)  # type: ignore[attr-defined]

    # Generic callback always fires.
    assert len(generic_events) == 1
    # Command ack must be suppressed.
    assert len(acks) == 0


@pytest.mark.asyncio
async def test_on_command_ack_fires_when_serial_is_none() -> None:
    """on_command_ack should fire for remoteControl events that lack a serial entirely."""
    acks: list[CommandAckEvent] = []

    def _capture_ack(event: CommandAckEvent) -> None:
        acks.append(event)

    config = BydConfig(username="user@example.com", password="secret", country_code="NL")
    client = BydClient(config, on_command_ack=_capture_ack)

    vin = "LC0CF4CD7N1000375"

    # Payload with no requestSerial or uuid — serial will be None.
    event = MqttEvent(
        event="remoteControl",
        vin=vin,
        topic="oversea/res/347678",
        payload={
            "event": "remoteControl",
            "vin": vin,
            "data": {"respondData": {"controlState": 1, "message": "OK"}},
        },
    )

    client._on_mqtt_event(event)  # type: ignore[attr-defined]

    # serial=None is not in _data_poll_serials, so ack should fire.
    assert len(acks) == 1
    assert acks[0].request_serial is None
    assert acks[0].is_correlated is False


@pytest.mark.asyncio
async def test_on_command_ack_not_fired_for_non_remote_control_events() -> None:
    """on_command_ack should NOT fire for vehicleInfo or other non-remoteControl events."""
    acks: list[CommandAckEvent] = []

    def _capture_ack(event: CommandAckEvent) -> None:
        acks.append(event)

    config = BydConfig(username="user@example.com", password="secret", country_code="NL")
    client = BydClient(config, on_command_ack=_capture_ack)

    vin = "LC0CF4CD7N1000375"

    event = MqttEvent(
        event="vehicleInfo",
        vin=vin,
        topic="oversea/res/347678",
        payload={
            "event": "vehicleInfo",
            "vin": vin,
            "data": {"respondData": {"onlineState": 1, "time": 123456}},
        },
    )

    client._on_mqtt_event(event)  # type: ignore[attr-defined]

    assert len(acks) == 0, "on_command_ack must not fire for vehicleInfo events"


@pytest.mark.asyncio
async def test_command_lifecycle_matched_event_for_registered_serial() -> None:
    lifecycle_events: list[CommandLifecycleEvent] = []

    def _capture_lifecycle(event: CommandLifecycleEvent) -> None:
        lifecycle_events.append(event)

    config = BydConfig(username="user@example.com", password="secret", country_code="NL")
    client = BydClient(config, on_command_lifecycle=_capture_lifecycle)

    vin = "LC0CF4CD7N1000375"
    serial = "CMD-SERIAL-MATCH-001"

    client._register_pending_command(vin, serial, "LOCKDOOR")  # type: ignore[attr-defined]
    await asyncio.sleep(0)

    event = MqttEvent(
        event="remoteControl",
        vin=vin,
        topic="oversea/res/347678",
        payload={
            "event": "remoteControl",
            "vin": vin,
            "data": {
                "uuid": serial,
                "respondData": {"res": 2, "message": "OK"},
            },
        },
    )

    client._on_mqtt_event(event)  # type: ignore[attr-defined]
    await asyncio.sleep(0)

    assert [e.status for e in lifecycle_events] == [
        CommandLifecycleStatus.REGISTERED,
        CommandLifecycleStatus.MATCHED,
    ]
    assert lifecycle_events[-1].request_serial == serial
    assert lifecycle_events[-1].command == "LOCKDOOR"

    diagnostics = client.get_command_ack_diagnostics()
    assert diagnostics.pending == 0
    assert diagnostics.matched == 1
    assert diagnostics.expired == 0
    assert diagnostics.uncorrelated == 0


@pytest.mark.asyncio
async def test_command_lifecycle_uncorrelated_event_when_serial_missing() -> None:
    lifecycle_events: list[CommandLifecycleEvent] = []

    def _capture_lifecycle(event: CommandLifecycleEvent) -> None:
        lifecycle_events.append(event)

    config = BydConfig(username="user@example.com", password="secret", country_code="NL")
    client = BydClient(config, on_command_lifecycle=_capture_lifecycle)

    vin = "LC0CF4CD7N1000375"

    event = MqttEvent(
        event="remoteControl",
        vin=vin,
        topic="oversea/res/347678",
        payload={
            "event": "remoteControl",
            "vin": vin,
            "data": {"respondData": {"res": 1, "message": "In progress"}},
        },
    )

    client._on_mqtt_event(event)  # type: ignore[attr-defined]
    await asyncio.sleep(0)

    assert len(lifecycle_events) == 1
    assert lifecycle_events[0].status == CommandLifecycleStatus.UNCORRELATED
    assert lifecycle_events[0].request_serial is None
    assert lifecycle_events[0].reason == "missing_request_serial"

    diagnostics = client.get_command_ack_diagnostics()
    assert diagnostics.pending == 0
    assert diagnostics.matched == 0
    assert diagnostics.expired == 0
    assert diagnostics.uncorrelated == 1


# ---------------------------------------------------------------------------
# MQTT error-code payloads (e.g. code 6051 = no GPS)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mqtt_error_6051_resolves_waiter() -> None:
    """An MQTT error payload (code 6051, no respondData) should resolve the matching waiter."""
    config = BydConfig(username="user@example.com", password="secret", country_code="NL")
    client = BydClient(config)

    vin = "LC0CF4CD7N1000375"
    serial = "BE3695B0014E49CBA40AA1D11979E61E"

    client._loop = asyncio.get_running_loop()  # type: ignore[attr-defined]
    client._mqtt_runtime = _DummyRuntime()  # type: ignore[attr-defined]
    # Register as data-poll serial (as _trigger_and_poll would).
    client._data_poll_serials.add(serial)  # type: ignore[attr-defined]

    waiter_task = asyncio.create_task(client._mqtt_wait(vin, serial=serial, timeout=1.0))  # type: ignore[attr-defined]
    await asyncio.sleep(0)

    # Exact payload from the BYD API for "no GPS signal".
    event = MqttEvent(
        event="remoteControl",
        vin=vin,
        topic="oversea/res/347678",
        payload={
            "data": {
                "code": "6051",
                "identifier": "347678",
                "message": "\u83b7\u53d6\u8f66\u8f86\u4f4d\u7f6e\u5931\u8d25",
                "uuid": serial,
            },
            "event": "remoteControl",
            "sign": "B11286bdA8dc288e15e243cf25806619B8b0F92c",
            "timestamp": 1772487924432,
            "userId": "347678",
            "vin": vin,
        },
    )

    client._on_mqtt_event(event)  # type: ignore[attr-defined]

    raw = await waiter_task
    assert raw is not None
    assert raw["code"] == "6051"
    assert raw["requestSerial"] == serial


@pytest.mark.asyncio
async def test_mqtt_error_6051_suppresses_command_ack() -> None:
    """Code 6051 with a data-poll serial must NOT fire on_command_ack."""
    acks: list[CommandAckEvent] = []

    def _capture_ack(event: CommandAckEvent) -> None:
        acks.append(event)

    config = BydConfig(username="user@example.com", password="secret", country_code="NL")
    client = BydClient(config, on_command_ack=_capture_ack)

    vin = "LC0CF4CD7N1000375"
    serial = "BE3695B0014E49CBA40AA1D11979E61E"

    client._data_poll_serials.add(serial)  # type: ignore[attr-defined]

    event = MqttEvent(
        event="remoteControl",
        vin=vin,
        topic="oversea/res/347678",
        payload={
            "data": {
                "code": "6051",
                "identifier": "347678",
                "message": "\u83b7\u53d6\u8f66\u8f86\u4f4d\u7f6e\u5931\u8d25",
                "uuid": serial,
            },
            "event": "remoteControl",
            "vin": vin,
        },
    )

    client._on_mqtt_event(event)  # type: ignore[attr-defined]

    assert len(acks) == 0, "on_command_ack must not fire for data-poll 6051 errors"


@pytest.mark.asyncio
async def test_mqtt_error_6051_fires_generic_callback() -> None:
    """Code 6051 error payloads should still surface through the generic on_mqtt_event callback."""
    generic_events: list[tuple[str, str, dict[str, object]]] = []

    def _capture_generic(event_name: str, vin: str, data: dict[str, object]) -> None:
        generic_events.append((event_name, vin, data))

    config = BydConfig(username="user@example.com", password="secret", country_code="NL")
    client = BydClient(config, on_mqtt_event=_capture_generic)

    vin = "LC0CF4CD7N1000375"
    serial = "BE3695B0014E49CBA40AA1D11979E61E"

    event = MqttEvent(
        event="remoteControl",
        vin=vin,
        topic="oversea/res/347678",
        payload={
            "data": {
                "code": "6051",
                "identifier": "347678",
                "message": "\u83b7\u53d6\u8f66\u8f86\u4f4d\u7f6e\u5931\u8d25",
                "uuid": serial,
            },
            "event": "remoteControl",
            "vin": vin,
        },
    )

    client._on_mqtt_event(event)  # type: ignore[attr-defined]

    assert len(generic_events) == 1
    assert generic_events[0][0] == "remoteControl"
    assert generic_events[0][2]["code"] == "6051"


def test_mqtt_data_unavailable_codes_contains_6051() -> None:
    """Smoke test: 6051 is a recognised data-unavailable code."""
    assert "6051" in _MQTT_DATA_UNAVAILABLE_CODES
