from __future__ import annotations

import asyncio

import pytest

from pybyd._mqtt import MqttEvent
from pybyd.client import BydClient
from pybyd.config import BydConfig


class _DummyRuntime:
    @property
    def is_running(self) -> bool:  # pragma: no cover
        return True


@pytest.mark.asyncio
async def test_remote_control_waiter_matches_uuid_serial() -> None:
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
async def test_remote_control_opportunistic_match_without_serial_resolves_oldest_only() -> None:
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

    # Payload shape observed in the wild: respondData has result, but no requestSerial/uuid.
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

    raw1 = await task1
    assert raw1 is not None
    assert raw1["res"] == 2

    # Only one waiter should be opportunistically satisfied.
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(task2, timeout=0.05)
    task2.cancel()


# ---------------------------------------------------------------------------
# on_command_ack callback tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_command_ack_fires_for_genuine_remote_control() -> None:
    """on_command_ack should fire for a remoteControl event whose serial is NOT in _data_poll_serials."""
    acks: list[tuple[str, str, dict[str, object]]] = []

    def _capture_ack(event: str, vin: str, data: dict[str, object]) -> None:
        acks.append((event, vin, data))

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
    assert acks[0][0] == "remoteControl"
    assert acks[0][1] == vin
    assert acks[0][2]["requestSerial"] == serial


@pytest.mark.asyncio
async def test_on_command_ack_suppressed_for_data_poll_serial() -> None:
    """on_command_ack must NOT fire when the serial belongs to a data poll (e.g. GPS)."""
    acks: list[tuple[str, str, dict[str, object]]] = []

    def _capture_ack(event: str, vin: str, data: dict[str, object]) -> None:
        acks.append((event, vin, data))

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
    acks: list[tuple[str, str, dict[str, object]]] = []

    def _capture_generic(event: str, vin: str, data: dict[str, object]) -> None:
        generic_events.append((event, vin, data))

    def _capture_ack(event: str, vin: str, data: dict[str, object]) -> None:
        acks.append((event, vin, data))

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
    acks: list[tuple[str, str, dict[str, object]]] = []

    def _capture_ack(event: str, vin: str, data: dict[str, object]) -> None:
        acks.append((event, vin, data))

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


@pytest.mark.asyncio
async def test_on_command_ack_not_fired_for_non_remote_control_events() -> None:
    """on_command_ack should NOT fire for vehicleInfo or other non-remoteControl events."""
    acks: list[tuple[str, str, dict[str, object]]] = []

    def _capture_ack(event: str, vin: str, data: dict[str, object]) -> None:
        acks.append((event, vin, data))

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
