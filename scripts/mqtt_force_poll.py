#!/usr/bin/env python3
"""Trigger a force-poll and dump every MQTT push, raw + decoded, no translation.

Usage::

    python scripts/mqtt_force_poll.py                      # first VIN, realtime trigger
    python scripts/mqtt_force_poll.py --vin LXX...         # specific VIN
    python scripts/mqtt_force_poll.py --triggers realtime,gps
    python scripts/mqtt_force_poll.py --wait 60            # listen 60s after trigger

For each push received the script prints AND persists:
  - raw payload bytes (length + hex) → ``captures/logs/<session>/``
  - AES-decrypted plaintext + parsed JSON → ``captures/logs_decrypted/<session>/``

The trigger HTTP response is also saved (file ``00_*``) so the on-wire payload
and the MQTT push that follows it can be diffed.

It deliberately does NOT route the payload through pybyd's pydantic models —
the goal is to surface fields the library does not yet extract.

Credentials come from the same ``.env`` as ``test_harness.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import paho.mqtt.client as mqtt

_repo = Path(__file__).resolve().parent.parent
_src = _repo / "src"
if _src.is_dir():
    sys.path.insert(0, str(_src))


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(_repo / ".env")

from pybyd import BydClient, BydConfig  # noqa: E402
from pybyd._api._common import (  # noqa: E402
    ENDPOINT_NOT_SUPPORTED_CODES,
    build_inner_base,
    post_token_json,
)
from pybyd._api.gps import fetch_gps_endpoint  # noqa: E402
from pybyd._api.realtime import fetch_realtime_endpoint  # noqa: E402
from pybyd._crypto.aes import aes_decrypt_utf8  # noqa: E402
from pybyd._mqtt import fetch_mqtt_bootstrap  # noqa: E402

# Capture the inner payload pyBYD posts for the realtime trigger so we can
# diff against the mitmproxy capture under captures/logs_decrypted/.
_LAST_INNER: dict[str, Any] = {}


def _make_realtime_fetcher(energy_type: str):  # type: ignore[no-untyped-def]
    """Return a realtime fetcher that overrides ``energyType`` and records inner."""

    async def fetch(
        endpoint, config, session, transport, vin, request_serial=None,
    ):
        now_ms = int(time.time() * 1000)
        inner = build_inner_base(
            config, now_ms=now_ms, vin=vin, request_serial=request_serial,
        )
        inner["energyType"] = energy_type
        inner["tboxVersion"] = config.tbox_version
        _LAST_INNER.clear()
        _LAST_INNER.update(inner)
        decoded = await post_token_json(
            endpoint=endpoint,
            config=config,
            session=session,
            transport=transport,
            inner=inner,
            now_ms=now_ms,
            vin=vin,
            not_supported_codes=ENDPOINT_NOT_SUPPORTED_CODES,
        )
        vehicle_info = decoded if isinstance(decoded, dict) else {}
        next_serial = vehicle_info.get("requestSerial") or request_serial
        return vehicle_info, next_serial

    return fetch


def _make_energy_fetcher(power_type: str, auto_model_name: str | None):  # type: ignore[no-untyped-def]
    """Return a getEnergyConsumption fetcher mirroring the BYD app's request shape.

    The BYD app sends ``powerType`` + ``requestType: 0`` + ``autoModelNameOut``
    on this endpoint. pyBYD's ``fetch_energy_consumption`` currently sends
    none of these. This fetcher mirrors the app to surface response-format
    differences.
    """

    async def fetch(
        endpoint, config, session, transport, vin, request_serial=None,
    ):
        now_ms = int(time.time() * 1000)
        inner: dict[str, Any] = dict(build_inner_base(config, now_ms=now_ms, vin=vin))
        inner["powerType"] = power_type
        inner["requestType"] = 0  # BYD app sends int, not string
        if auto_model_name:
            inner["autoModelNameOut"] = auto_model_name
        _LAST_INNER.clear()
        _LAST_INNER.update(inner)
        decoded = await post_token_json(
            endpoint=endpoint,
            config=config,
            session=session,
            transport=transport,
            inner=inner,
            now_ms=now_ms,
            vin=vin,
            not_supported_codes=ENDPOINT_NOT_SUPPORTED_CODES,
        )
        return (decoded if isinstance(decoded, dict) else {}), None

    return fetch

# AU defaults that are non-secret but not in BydConfig.from_env defaults.
_DEFAULTS = {
    "BYD_COUNTRY_CODE": "AU",
    "BYD_LANGUAGE": "en",
    "BYD_TIME_ZONE": "Australia/Sydney",
    "BYD_BASE_URL": "https://dilinkappoversea-au.byd.auto",
    "BYD_APP_VERSION": "3.3.3",
    "BYD_APP_INNER_VERSION": "333",
    "BYD_IMEI": "BANGCLE01234",
    "BYD_MODEL": "Redmi Note 9S",
    "BYD_MOBILE_BRAND": "REDMI",
    "BYD_MOBILE_MODEL": "REDMI NOTE 9S",
    "BYD_OSTYPE": "and",
    "BYD_OS_TYPE": "11",
    "BYD_OS_VERSION": "30",
    "BYD_SDK": "30",
    "BYD_MOD": "Xiaomi",
}

# (label, trigger_endpoint, fetch_fn). The realtime/energy fetchers are built
# per-run so ``--energy-type`` can override the values baked into the library.
_GPS_TRIGGER = ("/control/getGpsInfo", fetch_gps_endpoint)


def _build_triggers(
    energy_type: str,
    power_type: str,
    auto_model_name: str | None,
) -> dict[str, tuple[str, Any]]:
    return {
        "realtime": (
            "/vehicleInfo/vehicle/vehicleRealTimeRequest",
            _make_realtime_fetcher(energy_type),
        ),
        "gps": _GPS_TRIGGER,
        "energy": (
            "/vehicleInfo/vehicle/getEnergyConsumption",
            _make_energy_fetcher(power_type, auto_model_name),
        ),
    }


_TRIGGERS_KEYS = ("realtime", "gps", "energy")


def _section(title: str) -> None:
    bar = "=" * 78
    print(f"\n{bar}\n  {title}\n{bar}", flush=True)


def _safe_event_name(parsed: Any) -> str:
    if isinstance(parsed, dict):
        ev = parsed.get("event")
        if isinstance(ev, str) and ev:
            return ev
    return "unknown"


def _dump_message(
    seq: int,
    topic: str,
    payload: bytes,
    decrypt_key_hex: str,
    raw_dir: Path,
    decrypted_dir: Path,
) -> None:
    ts = datetime.now(UTC).isoformat(timespec="milliseconds")
    cleaned = "".join(payload.decode("ascii", errors="replace").split())

    print(f"\n── MQTT #{seq}  topic={topic}  bytes={len(payload)}  at {ts} ──", flush=True)
    print("\n[raw hex]")
    print(payload.hex())

    raw_record: dict[str, Any] = {
        "kind": "mqtt",
        "seq": seq,
        "topic": topic,
        "timestamp": ts,
        "payload_bytes": len(payload),
        "payload_hex": payload.hex(),
        "ciphertext_ascii": cleaned,
    }

    try:
        plain = aes_decrypt_utf8(cleaned, decrypt_key_hex)
    except Exception as exc:  # noqa: BLE001
        print(f"\n[decrypt failed] {exc!r}")
        raw_record["decrypt_error"] = repr(exc)
        _write_json(raw_dir / f"{seq:02d}_mqtt_decrypt_failed.json", raw_record)
        return

    print("\n[plaintext]")
    print(plain)

    parsed: Any
    try:
        parsed = json.loads(plain)
    except json.JSONDecodeError as exc:
        print(f"\n[json decode failed] {exc!r}")
        parsed = None

    print("\n[parsed JSON]")
    print(json.dumps(parsed, indent=2, ensure_ascii=False, default=str))

    event = _safe_event_name(parsed)
    base_name = f"{seq:02d}_mqtt_{event}"
    _write_json(raw_dir / f"{base_name}.json", raw_record)
    _write_json(
        decrypted_dir / f"{base_name}.json",
        {
            "kind": "mqtt",
            "seq": seq,
            "topic": topic,
            "timestamp": ts,
            "payload_bytes": len(payload),
            "plaintext": plain,
            "parsed": parsed,
        },
    )


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )


def _start_mqtt(
    *,
    bootstrap: Any,
    decrypt_key_hex: str,
    on_message_cb: Any,
    logger: logging.Logger,
) -> mqtt.Client:
    client = mqtt.Client(
        callback_api_version=cast(Any, mqtt).CallbackAPIVersion.VERSION2,
        client_id=bootstrap.client_id,
        protocol=mqtt.MQTTv5,
    )
    client.enable_logger(logger)
    client.username_pw_set(bootstrap.username, bootstrap.password)
    client.tls_set()

    def on_connect(c, _u, _f, reason_code, _p):  # type: ignore[no-untyped-def]
        if reason_code.value != 0:
            logger.error("MQTT connect failed reason=%s", reason_code)
            return
        logger.info("MQTT connected; subscribing topic=%s", bootstrap.topic)
        c.subscribe(bootstrap.topic, qos=0)

    client.on_connect = on_connect
    client.on_message = on_message_cb
    client.connect(bootstrap.broker_host, bootstrap.broker_port, keepalive=60)
    client.loop_start()
    return client


async def run(args: argparse.Namespace) -> int:
    missing = [k for k in ("BYD_USERNAME", "BYD_PASSWORD") if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            f"Missing required env var(s): {', '.join(missing)}. "
            "Set them in .env or the shell."
        )
    for k, v in _DEFAULTS.items():
        os.environ.setdefault(k, v)
    os.environ.setdefault(
        "BYD_IMEI_MD5",
        hashlib.md5(os.environ["BYD_IMEI"].encode()).hexdigest(),  # noqa: S324
    )

    # mqtt_enabled=False so the client's own runtime doesn't compete for the
    # broker session — we open our own connection and capture raw payloads.
    config = BydConfig.from_env(mqtt_enabled=False)

    captures_root = Path(args.captures_root).expanduser().resolve()
    session_name = args.session or f"force_poll_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}"
    raw_dir = captures_root / "logs" / session_name
    decrypted_dir = captures_root / "logs_decrypted" / session_name
    raw_dir.mkdir(parents=True, exist_ok=True)
    decrypted_dir.mkdir(parents=True, exist_ok=True)

    _section("mqtt_force_poll")
    print(f"  base_url     : {config.base_url}")
    print(f"  username     : {config.username}")
    print(f"  raw dir      : {raw_dir}")
    print(f"  decrypted dir: {decrypted_dir}")

    triggers = [t.strip() for t in args.triggers.split(",") if t.strip()]
    unknown = [t for t in triggers if t not in _TRIGGERS_KEYS]
    if unknown:
        raise SystemExit(f"Unknown trigger(s): {unknown}. Choose from {list(_TRIGGERS_KEYS)}.")
    # ``--power-type`` defaults to ``--energy-type`` since we currently assume
    # they share the same per-vehicle classifier (no capture has shown them
    # diverging). Use ``--power-type`` to override independently.
    power_type = args.power_type if args.power_type is not None else args.energy_type

    async with BydClient(config) as client:
        _section("LOGIN")
        await client.login()
        session = await client.ensure_session()
        decrypt_key = session.content_key()
        print(f"  user_id     : {session.user_id}")
        print(f"  decrypt_key : {decrypt_key[:6]}…{decrypt_key[-4:]}")

        _section("VEHICLES")
        vehicles = await client.get_vehicles()
        if not vehicles:
            raise SystemExit("No vehicles on this account.")
        for v in vehicles:
            print(f"  {v.vin}  ({getattr(v, 'name', '') or getattr(v, 'model_name', '')})")
        vin = args.vin or vehicles[0].vin
        target = next((v for v in vehicles if v.vin == vin), vehicles[0])
        auto_model_name = (
            args.auto_model_name
            or target.model_name
            or target.out_model_type
            or None
        )
        print(f"  → using VIN: {vin}")
        if "energy" in triggers:
            print(f"  energy.powerType         : {power_type!r}")
            print(f"  energy.autoModelNameOut  : {auto_model_name!r}")
        triggers_map = _build_triggers(args.energy_type, power_type, auto_model_name)

        transport = client._require_transport()  # noqa: SLF001
        bootstrap = await fetch_mqtt_bootstrap(config, session, transport)

        _section("MQTT BROKER")
        print(f"  host        : {bootstrap.broker_host}:{bootstrap.broker_port}")
        print(f"  topic       : {bootstrap.topic}")
        print(f"  client_id   : {bootstrap.client_id}")

        loop = asyncio.get_running_loop()
        message_count = 0

        def on_message(_c, _u, msg):  # type: ignore[no-untyped-def]
            nonlocal message_count
            message_count += 1
            seq = message_count
            payload = bytes(msg.payload)
            topic = msg.topic
            loop.call_soon_threadsafe(
                _dump_message, seq, topic, payload, decrypt_key, raw_dir, decrypted_dir,
            )

        mqtt_logger = logging.getLogger("mqtt_force_poll.mqtt")
        mqtt_client = _start_mqtt(
            bootstrap=bootstrap,
            decrypt_key_hex=decrypt_key,
            on_message_cb=on_message,
            logger=mqtt_logger,
        )

        # Give the broker a moment to finish CONNACK + SUBACK before we
        # send the trigger so we don't miss the push.
        await asyncio.sleep(2.0)

        try:
            for idx, label in enumerate(triggers):
                endpoint, fetch_fn = triggers_map[label]
                _section(f"TRIGGER  {label}  →  {endpoint}")
                t0 = time.monotonic()
                trigger_info, serial = await fetch_fn(
                    endpoint, config, session, transport, vin,
                )
                dt = (time.monotonic() - t0) * 1000
                print(f"  trigger HTTP latency: {dt:.0f} ms")
                print(f"  requestSerial       : {serial}")
                if _LAST_INNER:
                    print("  request inner (sent):")
                    print(json.dumps(_LAST_INNER, indent=2, ensure_ascii=False))
                print("  trigger HTTP response (raw decoded inner):")
                print(json.dumps(trigger_info, indent=2, ensure_ascii=False, default=str))

                trigger_record = {
                    "kind": "http_trigger",
                    "label": label,
                    "endpoint": endpoint,
                    "vin": vin,
                    "request_serial": serial,
                    "latency_ms": round(dt, 1),
                    "request_inner": dict(_LAST_INNER) if _LAST_INNER else None,
                    "response_inner": trigger_info,
                    "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
                }
                _LAST_INNER.clear()
                fname = f"00_{idx:02d}_http_{label}.json"
                _write_json(raw_dir / fname, trigger_record)
                _write_json(decrypted_dir / fname, trigger_record)

            _section(f"LISTENING for {args.wait}s — every push will be dumped")
            await asyncio.sleep(args.wait)
        finally:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()

        _section(f"DONE  ({message_count} MQTT message(s) received)")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vin", help="VIN to force-poll (default: first vehicle)")
    parser.add_argument(
        "--triggers",
        default="realtime",
        help=f"Comma-separated triggers to fire. Available: {','.join(_TRIGGERS_KEYS)}",
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=30.0,
        help="Seconds to keep listening on MQTT after triggers fire (default: 30)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Verbose logging (pybyd, paho, aiohttp)",
    )
    parser.add_argument(
        "--energy-type",
        default="0",
        help="Override realtime trigger 'energyType' field (pyBYD default: 0; BYD app sends 2)",
    )
    parser.add_argument(
        "--power-type",
        default=None,
        help="Override 'powerType' on the energy trigger (defaults to --energy-type)",
    )
    parser.add_argument(
        "--auto-model-name",
        default=None,
        help="Override 'autoModelNameOut' on the energy trigger (defaults to Vehicle.model_name)",
    )
    parser.add_argument(
        "--captures-root",
        default=str(Path(__file__).resolve().parent.parent.parent / "captures"),
        help="Root directory containing logs/ and logs_decrypted/ (default: ../captures)",
    )
    parser.add_argument(
        "--session",
        default=None,
        help="Subfolder name under logs/ and logs_decrypted/ (default: force_poll_<UTC timestamp>)",
    )
    args = parser.parse_args()

    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    if not args.debug:
        # Quiet noisy libs at INFO; keep our own logger informative.
        for name in ("paho.mqtt.client", "aiohttp.client", "asyncio"):
            logging.getLogger(name).setLevel(logging.WARNING)

    try:
        sys.exit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
