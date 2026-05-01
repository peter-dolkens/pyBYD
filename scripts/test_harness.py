#!/usr/bin/env python3
"""End-to-end test harness for pybyd with full DEBUG output.

Usage::

    python scripts/test_harness.py                 # read-only dump
    python scripts/test_harness.py --verify        # also verify control PIN
    python scripts/test_harness.py --log run.log   # tee debug to a file

Credentials are read from a ``.env`` file in the repo root (see
``.env.example``).  Any ``BYD_*`` env var set in the shell wins over
``.env``, which wins over the non-secret defaults below.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import logging
import os
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

_repo = Path(__file__).resolve().parent.parent
_src = _repo / "src"
if _src.is_dir():
    sys.path.insert(0, str(_src))


def _load_dotenv(path: Path) -> None:
    """Minimal ``.env`` loader: ``KEY=VALUE`` lines, no quoting/expansion.

    Existing environment variables take precedence (shell wins over file).
    """
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
from pybyd._transport import SecureTransport  # noqa: E402
from pybyd.session import Session  # noqa: E402


def _inject_session_from_token(client: BydClient, token: str) -> None:
    """Decode a base64 ``userId:signToken:encryToken`` blob and seed the client.

    Skips login and uses the captured session as-is.
    """
    try:
        decoded = base64.b64decode(token).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        raise SystemExit(f"--token is not valid base64: {exc}") from exc
    parts = decoded.split(":")
    if len(parts) != 3:
        raise SystemExit(
            f"--token decoded to {decoded!r}, expected 'userId:signToken:encryToken'"
        )
    user_id, sign_token, encry_token = parts
    session = Session(user_id=user_id, sign_token=sign_token, encry_token=encry_token)
    client._session = session  # noqa: SLF001
    if client._mqtt_runtime is not None:  # noqa: SLF001
        client._mqtt_runtime.update_decrypt_key(session.content_key())  # noqa: SLF001


def _patch_transport_logging(extra_headers: dict[str, str] | None = None) -> None:
    """Wrap SecureTransport.post_secure to log responses and inject headers.

    The library hardcodes ``user-agent: okhttp/4.12.0`` and sends no
    ``x-app-key``.  The AU server rejects requests without the iOS
    ``x-app-key`` header.  We monkey-patch by reimplementing post_secure
    with our own header dict.
    """
    from pybyd._constants import USER_AGENT
    import aiohttp
    from pybyd.exceptions import BydTransportError

    log = logging.getLogger("pybyd._transport.dump")
    extra = dict(extra_headers or {})

    async def post_secure(self, endpoint, outer_payload):  # type: ignore[no-untyped-def]
        encoded = self._codec.encode_envelope(
            json.dumps(outer_payload, separators=(",", ":"))
        )
        headers: dict[str, str] = {
            "accept-encoding": "identity",
            "content-type": "application/json; charset=UTF-8",
            "user-agent": USER_AGENT,
        }
        headers.update(extra)
        url = f"{self._config.base_url}{endpoint}"
        body = json.dumps({"request": encoded})

        log.debug(
            "REQ %s headers=%s outer=%s",
            endpoint,
            headers,
            json.dumps(outer_payload, default=str, ensure_ascii=False)[:2000],
        )

        try:
            async with self._http.post(url, data=body, headers=headers) as resp:
                text = await resp.text()
                log.debug(
                    "HTTP %s %s resp_headers=%s",
                    resp.status,
                    endpoint,
                    dict(resp.headers),
                )
                if resp.status != 200:
                    raise BydTransportError(
                        f"HTTP {resp.status} from {endpoint}: {text[:200]}",
                        status_code=resp.status,
                        endpoint=endpoint,
                    )
        except aiohttp.ClientError as exc:
            raise BydTransportError(
                f"Request to {endpoint} failed: {exc}",
                endpoint=endpoint,
            ) from exc

        body_json = json.loads(text)
        response_str = body_json["response"]
        decoded_text = (
            self._codec.decode_envelope(response_str).decode("utf-8").strip()
        )
        if decoded_text.startswith("F{") or decoded_text.startswith("F["):
            decoded_text = decoded_text[1:]
        result = json.loads(decoded_text)
        log.debug(
            "RES %s body=%s",
            endpoint,
            json.dumps(result, default=str, ensure_ascii=False)[:4000],
        )
        return result

    SecureTransport.post_secure = post_secure  # type: ignore[assignment]

# ── Non-secret defaults ────────────────────────────────────────────
# Secrets (BYD_USERNAME, BYD_PASSWORD, BYD_CONTROL_PIN) come from .env or
# the shell environment.  These below are the public, region-specific
# defaults discovered while reverse-engineering the AU API.

COUNTRY_CODE = "AU"
LANGUAGE = "en"
TIME_ZONE = "Australia/Sydney"
APP_VERSION = "3.3.3"
APP_INNER_VERSION = "333"

# AU-specific overseas endpoint discovered via DNS probing.
BASE_URL = "https://dilinkappoversea-au.byd.auto"

# Device profile copied from a real BYD Connect Android capture (Redmi
# Note 9S).  The ``imei_md5`` is the device-specific MD5 the app sends —
# the AU server appears to reject the all-zeros default.
DEVICE_IMEI = "BANGCLE01234"
DEVICE_MODEL = "Redmi Note 9S"
DEVICE_MOBILE_BRAND = "REDMI"
DEVICE_MOBILE_MODEL = "REDMI NOTE 9S"
DEVICE_OSTYPE = "and"
DEVICE_OS_TYPE = "11"
DEVICE_OS_VERSION = "30"
DEVICE_SDK = "30"
DEVICE_MOD = "Xiaomi"


# ── Logging ────────────────────────────────────────────────────────


def configure_logging(log_file: str | None) -> None:
    """Turn on DEBUG everywhere relevant and tee to a file if given."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    stream = logging.StreamHandler(sys.stderr)
    stream.setLevel(logging.DEBUG)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    if log_file:
        file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)

    for name in (
        "pybyd",
        "pybyd._transport",
        "pybyd._mqtt",
        "pybyd._api",
        "pybyd.client",
        "pybyd.car",
        "aiohttp.client",
        "aiohttp.connector",
        "asyncio",
        "paho.mqtt.client",
    ):
        logging.getLogger(name).setLevel(logging.DEBUG)


# ── Pretty printing helpers ────────────────────────────────────────


def _section(title: str) -> None:
    line = "=" * 72
    print(f"\n{line}\n  {title}\n{line}", flush=True)


def _dump_model(label: str, obj: Any) -> None:
    print(f"\n── {label} ──", flush=True)
    if isinstance(obj, BaseModel):
        print(json.dumps(obj.model_dump(), indent=2, default=str, ensure_ascii=False))
    else:
        print(repr(obj))


def _dump_raw(label: str, raw: Any) -> None:
    print(f"\n── {label} (raw) ──", flush=True)
    print(json.dumps(raw, indent=2, default=str, ensure_ascii=False))


# ── Fetch helpers ──────────────────────────────────────────────────


async def _safe(label: str, coro: Any) -> Any:
    """Run *coro*, log/print failures, never raise."""
    print(f"\n>>> {label}", flush=True)
    try:
        result = await coro
    except Exception as exc:  # noqa: BLE001
        print(f"!! {label} failed: {exc}", flush=True)
        traceback.print_exc()
        return None
    return result


async def dump_vehicle(client: BydClient, vin: str) -> None:
    _section(f"VEHICLE  {vin}")

    rt = await _safe(f"get_vehicle_realtime({vin})", client.get_vehicle_realtime(vin))
    if rt is not None:
        _dump_model("Realtime", rt)
        _dump_raw("Realtime", rt.raw)

    gps = await _safe(f"get_gps_info({vin})", client.get_gps_info(vin))
    if gps is not None:
        _dump_model("GPS", gps)
        _dump_raw("GPS", gps.raw)

    energy = await _safe(f"get_energy_consumption({vin})", client.get_energy_consumption(vin))
    if energy is not None:
        _dump_model("Energy", energy)
        _dump_raw("Energy", energy.raw)

    charging = await _safe(f"get_charging_status({vin})", client.get_charging_status(vin))
    if charging is not None:
        _dump_model("Charging", charging)
        _dump_raw("Charging", charging.raw)

    hvac = await _safe(f"get_hvac_status({vin})", client.get_hvac_status(vin))
    if hvac is not None:
        _dump_model("HVAC", hvac)
        _dump_raw("HVAC", hvac.raw)

    latest = await _safe(f"get_latest_config({vin})", client.get_latest_config(vin))
    if latest is not None:
        _dump_model("LatestConfig", latest)
        _dump_raw("LatestConfig", latest.raw)

    caps = await _safe(f"get_vehicle_capabilities({vin})", client.get_vehicle_capabilities(vin))
    if caps is not None:
        _dump_model("Capabilities", caps)


# ── Main ───────────────────────────────────────────────────────────


async def run(args: argparse.Namespace) -> int:
    # Defaults: hardcoded, but every BYD_* env var still wins (also brings in
    # device profile overrides like BYD_OSTYPE, BYD_MODEL, etc.).
    missing = [k for k in ("BYD_USERNAME", "BYD_PASSWORD") if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            f"Missing required env var(s): {', '.join(missing)}. "
            "Set them in .env (see .env.example) or in the shell."
        )

    os.environ.setdefault("BYD_COUNTRY_CODE", COUNTRY_CODE)
    os.environ.setdefault("BYD_LANGUAGE", LANGUAGE)
    os.environ.setdefault("BYD_TIME_ZONE", TIME_ZONE)
    os.environ.setdefault("BYD_BASE_URL", BASE_URL)
    os.environ.setdefault("BYD_APP_VERSION", APP_VERSION)
    os.environ.setdefault("BYD_APP_INNER_VERSION", APP_INNER_VERSION)
    os.environ.setdefault("BYD_IMEI", DEVICE_IMEI)
    os.environ.setdefault(
        "BYD_IMEI_MD5",
        hashlib.md5(os.environ["BYD_IMEI"].encode()).hexdigest(),  # noqa: S324
    )
    os.environ.setdefault("BYD_MODEL", DEVICE_MODEL)
    os.environ.setdefault("BYD_MOBILE_BRAND", DEVICE_MOBILE_BRAND)
    os.environ.setdefault("BYD_MOBILE_MODEL", DEVICE_MOBILE_MODEL)
    os.environ.setdefault("BYD_OSTYPE", DEVICE_OSTYPE)
    os.environ.setdefault("BYD_OS_TYPE", DEVICE_OS_TYPE)
    os.environ.setdefault("BYD_OS_VERSION", DEVICE_OS_VERSION)
    os.environ.setdefault("BYD_SDK", DEVICE_SDK)
    os.environ.setdefault("BYD_MOD", DEVICE_MOD)

    config = BydConfig.from_env(mqtt_enabled=not args.no_mqtt)

    _section("pybyd test_harness")
    print(f"  time         : {datetime.now(UTC).isoformat()}")
    print(f"  base_url     : {config.base_url}")
    print(f"  country_code : {config.country_code}")
    print(f"  language     : {config.language}")
    print(f"  time_zone    : {config.time_zone}")
    print(f"  username     : {config.username}")
    print(f"  control_pin  : {'set' if config.control_pin else 'missing'}")
    print(f"  mqtt_enabled : {config.mqtt_enabled}")

    async with BydClient(config) as client:
        _section("LOGIN" if not args.token else "INJECT TOKEN (skip login)")
        if args.token:
            _inject_session_from_token(client, args.token)
        else:
            await client.login()
        session = await client.ensure_session()
        print(f"  user_id     : {session.user_id}")
        print(f"  sign_token  : {session.sign_token[:6]}…{session.sign_token[-4:]}")
        print(f"  encry_token : {session.encry_token[:6]}…{session.encry_token[-4:]}")

        _section("VEHICLES")
        vehicles = await client.get_vehicles()
        print(f"  count: {len(vehicles)}")
        for v in vehicles:
            _dump_model(f"Vehicle {v.vin}", v)
            _dump_raw(f"Vehicle {v.vin}", v.raw)

        target_vins: list[str] = (
            [args.vin] if args.vin else [v.vin for v in vehicles]
        )

        for vin in target_vins:
            await dump_vehicle(client, vin)

            if args.verify:
                _section(f"VERIFY CONTROL PIN  {vin}")
                resp = await _safe(
                    f"verify_command_access({vin})",
                    client.verify_command_access(vin),
                )
                if resp is not None:
                    _dump_model("VerifyControlPasswordResponse", resp)
                    print(f"  commands_enabled: {client.commands_enabled}")

    _section("DONE")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vin", help="Only query this VIN")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="Also call verify_command_access (uses the control PIN)",
    )
    parser.add_argument(
        "--no-mqtt",
        action="store_true",
        help="Disable the MQTT background listener (HTTP only)",
    )
    parser.add_argument("--log", help="Tee debug output to FILE")
    parser.add_argument(
        "--token",
        default=os.environ.get("BYD_TOKEN"),
        help="Base64 'userId:signToken:encryToken' to skip login (or BYD_TOKEN env)",
    )
    parser.add_argument(
        "--user-agent",
        default=os.environ.get("BYD_USER_AGENT"),
        help="Override the User-Agent header (or BYD_USER_AGENT env)",
    )
    parser.add_argument(
        "--x-app-key",
        dest="x_app_key",
        default=os.environ.get("BYD_X_APP_KEY"),
        help="Set x-app-key header (or BYD_X_APP_KEY env)",
    )
    args = parser.parse_args()

    configure_logging(args.log)

    extra_headers: dict[str, str] = {}
    if args.user_agent:
        extra_headers["user-agent"] = args.user_agent
    if args.x_app_key:
        extra_headers["x-app-key"] = args.x_app_key
    _patch_transport_logging(extra_headers or None)

    try:
        sys.exit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        print("\nInterrupted", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
