#!/usr/bin/env python3
"""Decrypted API response debugger for all BYD endpoints.

Calls any BYD API endpoint and outputs the *decrypted* response as one
clean JSON structure: outer envelope metadata (code, message) merged
with the decrypted inner payload.  No encrypted hex blobs — everything
is fully unwrapped.

Request inner payloads are excluded by default (``--include-request``
to include).  Identifying fields (VIN, userId, serials, GPS coords,
timestamps, etc.) are redacted for safe sharing by default
(``--no-redact`` to disable).

Usage
-----
::

    export BYD_USERNAME="you@example.com"
    export BYD_PASSWORD="your-password"
    python scripts/api_debug.py --pretty

Examples::

    # All read-only endpoints, redacted, pretty
    python scripts/api_debug.py --pretty

    # Single endpoint with full detail
    python scripts/api_debug.py --hvac --no-redact --include-request --pretty

    # PIN verification round-trip
    python scripts/api_debug.py --verify-pin --pin 123456 --pretty

    # Remote command (lock) with confirmation bypass
    python scripts/api_debug.py --command lock --pin 123456 -y --pretty

    # Full dump to file
    python scripts/api_debug.py --all --include-request -o debug.json
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import re
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Allow running from the repo root without installing the package.
_repo = Path(__file__).resolve().parent.parent
_src = _repo / "src"
if _src.is_dir():
    sys.path.insert(0, str(_src))

# pylint: disable=wrong-import-position
from pybyd import BydClient, BydConfig  # noqa: E402
from pybyd._api._common import build_inner_base, decode_respond_data  # noqa: E402
from pybyd._api._envelope import build_token_outer_envelope  # noqa: E402
from pybyd._api.login import build_login_request  # noqa: E402
from pybyd._crypto.aes import aes_decrypt_utf8  # noqa: E402
from pybyd._crypto.hashing import md5_hex, pwd_login_key  # noqa: E402
from pybyd._transport import SecureTransport  # noqa: E402
from pybyd.session import Session  # noqa: E402

_logger = logging.getLogger(__name__)

# ── Redaction ────────────────────────────────────────────────

_VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")

_REDACT_KEY_MAP: dict[str, str] = {
    # Vehicle / account identifiers
    "vin": "***VIN***",
    "vinlist": "***VIN***",
    "vinno": "***VIN***",
    "userid": "***USER***",
    "user_id": "***USER***",
    "identifier": "***USER***",
    "username": "***USER***",
    "useraccount": "***USER***",
    # Device fingerprint
    "imei": "***DEVICE***",
    "imeimd5": "***DEVICE***",
    "mac": "***DEVICE***",
    # Request correlation / nonces
    "requestserial": "***SERIAL***",
    "serial": "***SERIAL***",
    "random": "***RANDOM***",
    # Timestamps
    "timestamp": "***TIME***",
    "reqtimestamp": "***TIME***",
    "servicetime": "***TIME***",
    "createtime": "***TIME***",
    "updatetime": "***TIME***",
    "logintime": "***TIME***",
    "lastlogintime": "***TIME***",
    # Crypto / auth
    "sign": "***SIG***",
    "signkey": "***SIG***",
    "checkcode": "***SIG***",
    "commandpwd": "***PIN***",
    "password": "***PWD***",
    "encrytoken": "***TOKEN***",
    "signtoken": "***TOKEN***",
    "refreshtoken": "***TOKEN***",
    "accesstoken": "***TOKEN***",
    # Encrypted blobs (safety net — shouldn't appear)
    "encrydata": "***ENCRYPTED***",
    "responddata": "***ENCRYPTED***",
    # GPS / location
    "longitude": "***COORD***",
    "latitude": "***COORD***",
    "lng": "***COORD***",
    "lat": "***COORD***",
    "gpslon": "***COORD***",
    "gpslat": "***COORD***",
    # PII
    "phoneno": "***PII***",
    "phone": "***PII***",
    "email": "***PII***",
    "nickname": "***PII***",
}


def _redact_value(value: Any, *, _depth: int = 0) -> Any:
    """Recursively redact identifying fields from a JSON-like structure."""
    if _depth > 30:
        return value

    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key_lower = k.lower().replace("_", "")
            placeholder = _REDACT_KEY_MAP.get(key_lower)
            if placeholder is not None:
                out[k] = placeholder
            else:
                out[k] = _redact_value(v, _depth=_depth + 1)
        return out

    if isinstance(value, list):
        return [_redact_value(v, _depth=_depth + 1) for v in value]

    if isinstance(value, str) and len(value) >= 17:
        return _VIN_RE.sub("***VIN***", value)

    return value


# ── Raw transport helper ─────────────────────────────────────


async def _raw_post(
    *,
    endpoint: str,
    config: BydConfig,
    session: Session,
    transport: SecureTransport,
    inner: dict[str, str],
    now_ms: int | None = None,
    include_request: bool = False,
    user_type: str | None = None,
) -> dict[str, Any]:
    """Post a token-enveloped request and return the structured result.

    Returns a dict with ``endpoint``, ``code``, ``message``, ``data``,
    and optionally ``request`` (the inner payload dict sent to the API).
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    outer, content_key = build_token_outer_envelope(config, session, inner, now_ms, user_type=user_type)
    response = await transport.post_secure(endpoint, outer)

    code = str(response.get("code", ""))
    message = str(response.get("message", ""))

    # Decrypt inner payload
    data: Any = {}
    if code == "0":
        data = decode_respond_data(endpoint=endpoint, response=response, content_key=content_key)
    else:
        # On error, still try to decrypt if respondData exists
        with contextlib.suppress(Exception):
            data = decode_respond_data(endpoint=endpoint, response=response, content_key=content_key)
    result: dict[str, Any] = {"endpoint": endpoint}
    if include_request:
        result["request"] = dict(inner)
    result["code"] = code
    result["message"] = message
    result["data"] = data
    return result


# ── Endpoint fetchers ────────────────────────────────────────


async def _fetch_login(
    *,
    config: BydConfig,
    transport: SecureTransport,
    include_request: bool,
) -> dict[str, Any]:
    """Capture the login round-trip."""
    endpoint = "/app/account/login"
    now_ms = int(time.time() * 1000)
    outer = build_login_request(config, now_ms)

    result: dict[str, Any] = {"name": "login", "endpoint": endpoint}
    if include_request:
        # The login inner is embedded inside encryData; reconstruct it
        # for display by re-deriving the fields (they're deterministic
        # except for the random nonce).
        result["request"] = "(login request — see build_login_request)"

    response = await transport.post_secure(endpoint, outer)
    code = str(response.get("code", ""))
    message = str(response.get("message", ""))
    result["code"] = code
    result["message"] = message

    # Decrypt login respondData with the login-specific key
    respond_data = response.get("respondData")
    if isinstance(respond_data, str) and respond_data:
        try:
            plaintext = aes_decrypt_utf8(respond_data, pwd_login_key(config.password))
            data = json.loads(plaintext) if plaintext and plaintext.strip() else {}
        except Exception:
            data = {}
    else:
        data = {}

    result["data"] = data
    return result


async def _fetch_simple(
    *,
    name: str,
    endpoint: str,
    config: BydConfig,
    session: Session,
    transport: SecureTransport,
    inner_extras: dict[str, str] | None = None,
    vin: str | None = None,
    include_request: bool,
) -> dict[str, Any]:
    """Fetch a simple single-request endpoint."""
    inner = build_inner_base(config, vin=vin)
    if inner_extras:
        inner.update(inner_extras)

    raw = await _raw_post(
        endpoint=endpoint,
        config=config,
        session=session,
        transport=transport,
        inner=inner,
        include_request=include_request,
    )
    return {"name": name, **raw}


async def _fetch_trigger_poll(
    *,
    name: str,
    trigger_endpoint: str,
    poll_endpoint: str,
    config: BydConfig,
    session: Session,
    transport: SecureTransport,
    inner_extras: dict[str, str] | None = None,
    vin: str | None = None,
    include_request: bool,
    poll_attempts: int = 5,
    poll_interval: float = 2.0,
) -> dict[str, Any]:
    """Fetch a trigger-then-poll endpoint, capturing every round-trip."""
    steps: list[dict[str, Any]] = []

    # Phase 1: Trigger
    inner = build_inner_base(config, vin=vin)
    if inner_extras:
        inner.update(inner_extras)

    trigger_raw = await _raw_post(
        endpoint=trigger_endpoint,
        config=config,
        session=session,
        transport=transport,
        inner=inner,
        include_request=include_request,
    )
    trigger_raw["phase"] = "trigger"
    steps.append(trigger_raw)

    # Extract serial for polling
    serial: str | None = None
    trigger_data = trigger_raw.get("data")
    if isinstance(trigger_data, dict):
        serial = trigger_data.get("requestSerial")

    # Phase 2: Poll
    for attempt in range(1, poll_attempts + 1):
        await asyncio.sleep(poll_interval)

        poll_inner = build_inner_base(config, vin=vin, request_serial=serial)
        if inner_extras:
            poll_inner.update(inner_extras)

        poll_raw = await _raw_post(
            endpoint=poll_endpoint,
            config=config,
            session=session,
            transport=transport,
            inner=poll_inner,
            include_request=include_request,
        )
        poll_raw["phase"] = f"poll_{attempt}"
        steps.append(poll_raw)

        # Check if data is ready (non-empty beyond just requestSerial)
        poll_data = poll_raw.get("data")
        if isinstance(poll_data, dict):
            serial = poll_data.get("requestSerial") or serial
            keys = set(poll_data.keys()) - {"requestSerial"}
            if keys:
                break

    return {"name": name, "steps": steps}


async def _fetch_verify_pin(
    *,
    config: BydConfig,
    session: Session,
    transport: SecureTransport,
    vin: str,
    pin_hash: str,
    include_request: bool,
) -> dict[str, Any]:
    """Capture the PIN verification round-trip."""
    endpoint = "/vehicle/vehicleswitch/verifyControlPassword"
    inner = build_inner_base(config, vin=vin)
    inner["commandPwd"] = pin_hash
    inner["functionType"] = "remoteControl"

    raw = await _raw_post(
        endpoint=endpoint,
        config=config,
        session=session,
        transport=transport,
        inner=inner,
        include_request=include_request,
    )
    return {"name": "verify_pin", **raw}


async def _fetch_remote_command(
    *,
    name: str,
    command_type: str,
    config: BydConfig,
    session: Session,
    transport: SecureTransport,
    vin: str,
    pin_hash: str,
    include_request: bool,
    poll_attempts: int = 5,
    poll_interval: float = 2.0,
) -> dict[str, Any]:
    """Capture a remote control command round-trip (trigger + polls)."""
    steps: list[dict[str, Any]] = []

    # Trigger
    trigger_endpoint = "/control/remoteControl"
    inner = build_inner_base(config, vin=vin)
    inner["commandPwd"] = pin_hash
    inner["commandType"] = command_type

    trigger_raw = await _raw_post(
        endpoint=trigger_endpoint,
        config=config,
        session=session,
        transport=transport,
        inner=inner,
        include_request=include_request,
    )
    trigger_raw["phase"] = "trigger"
    steps.append(trigger_raw)

    # Extract serial
    serial: str | None = None
    trigger_data = trigger_raw.get("data")
    if isinstance(trigger_data, dict):
        serial = trigger_data.get("requestSerial")

    # Poll
    poll_endpoint = "/control/remoteControlResult"
    for attempt in range(1, poll_attempts + 1):
        await asyncio.sleep(poll_interval)

        poll_inner = build_inner_base(config, vin=vin, request_serial=serial)
        poll_inner["commandPwd"] = ""
        poll_inner["commandType"] = command_type

        poll_raw = await _raw_post(
            endpoint=poll_endpoint,
            config=config,
            session=session,
            transport=transport,
            inner=poll_inner,
            include_request=include_request,
        )
        poll_raw["phase"] = f"poll_{attempt}"
        steps.append(poll_raw)

        poll_data = poll_raw.get("data")
        if isinstance(poll_data, dict):
            serial = poll_data.get("requestSerial") or serial
            # Terminal states: controlState != 0, or res >= 2, or result present
            cs = poll_data.get("controlState")
            if cs is not None and int(cs) != 0:
                break
            res = poll_data.get("res")
            if res is not None and int(res) >= 2:
                break
            if "result" in poll_data:
                break

    return {"name": name, "steps": steps}


# ── CLI command map ──────────────────────────────────────────

_REMOTE_COMMANDS: dict[str, str] = {
    "lock": "LOCKDOOR",
    "unlock": "OPENDOOR",
    "climate-start": "OPENAIR",
    "climate-stop": "CLOSEAIR",
    "flash-lights": "FLASHLIGHTNOWHISTLE",
    "close-windows": "CLOSEWINDOW",
    "find-car": "FINDCAR",
    "seat-climate": "VENTILATIONHEATING",
    "battery-heat": "BATTERYHEAT",
    "schedule-climate": "BOOKINGAIR",
}

_READ_ONLY_FLAGS: list[str] = [
    "vehicles",
    "realtime",
    "gps",
    "energy",
    "charging",
    "hvac",
    "latest_config",
    "push_state",
]


# ── Resolve PIN ──────────────────────────────────────────────


def _resolve_pin(pin_arg: str | None, config: BydConfig) -> str:
    """Return the uppercase MD5 hex of the control PIN, or empty string."""
    raw: str | None = pin_arg or config.control_pin
    if not raw:
        return ""
    stripped = raw.strip()
    # Already an MD5 hex?
    if len(stripped) == 32 and all(ch in "0123456789abcdefABCDEF" for ch in stripped):
        return stripped.upper()
    return md5_hex(stripped)


# ── Confirmation prompt ──────────────────────────────────────


def _confirm_write(action: str, *, skip: bool) -> bool:
    """Prompt for confirmation before a write/command action."""
    if skip:
        return True
    print(
        f"\n\033[93mWARNING: This will execute '{action}' on your vehicle.\033[0m",
        file=sys.stderr,
    )
    try:
        response = input("Press Enter to continue or Ctrl+C to abort: ")  # noqa: F841
        return True
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.", file=sys.stderr)
        return False


# ── Main ─────────────────────────────────────────────────────


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Decrypted API response debugger for all BYD endpoints.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Environment variables:\n"
            "  BYD_USERNAME       Account email/phone\n"
            "  BYD_PASSWORD       Account password\n"
            "  BYD_CONTROL_PIN    6-digit control PIN (alternative to --pin)\n"
        ),
    )

    # Endpoint selection
    ep = parser.add_argument_group("endpoint selection")
    ep.add_argument("--all", action="store_true", help="All read-only endpoints (default if nothing selected)")
    ep.add_argument("--vehicles", action="store_true", help="Vehicle list")
    ep.add_argument("--realtime", action="store_true", help="Realtime vehicle data (trigger+poll)")
    ep.add_argument("--gps", action="store_true", help="GPS info (trigger+poll)")
    ep.add_argument("--energy", action="store_true", help="Energy consumption")
    ep.add_argument("--charging", action="store_true", help="Charging status")
    ep.add_argument("--hvac", action="store_true", help="HVAC / climate status")
    ep.add_argument("--latest-config", action="store_true", help="Latest config / capabilities")
    ep.add_argument("--push-state", action="store_true", help="Push notification state")
    ep.add_argument("--verify-pin", action="store_true", help="Verify control PIN (requires --pin)")
    ep.add_argument(
        "--command",
        choices=list(_REMOTE_COMMANDS.keys()),
        help="Execute remote command (requires --pin)",
    )
    ep.add_argument(
        "--toggle-smart-charging",
        choices=["on", "off"],
        metavar="on|off",
        help="Toggle smart charging on/off",
    )
    ep.add_argument("--set-push", choices=["on", "off"], metavar="on|off", help="Set push notifications on/off")

    # Auth / targeting
    auth = parser.add_argument_group("auth / targeting")
    auth.add_argument("--vin", help="Target VIN (default: first discovered vehicle)")
    auth.add_argument("--pin", help="6-digit control PIN (or BYD_CONTROL_PIN env)")

    # Output
    out = parser.add_argument_group("output")
    out.add_argument("--include-request", action="store_true", help="Include request inner payload in output")
    out.add_argument("--no-redact", action="store_true", help="Disable identifier redaction")
    out.add_argument("--output", "-o", help="Write JSON to file")
    out.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    out.add_argument("--verbose", "-v", action="store_true", help="Enable DEBUG logging")

    # Polling
    poll = parser.add_argument_group("polling")
    poll.add_argument("--poll-attempts", type=int, default=5, help="Max poll attempts (default: 5)")
    poll.add_argument("--poll-interval", type=float, default=2.0, help="Seconds between polls (default: 2.0)")

    # Safety
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation for write commands")

    args = parser.parse_args()

    # Logging
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING)

    # Determine which endpoints to fetch
    any_selected = (
        args.vehicles
        or args.realtime
        or args.gps
        or args.energy
        or args.charging
        or args.hvac
        or args.latest_config
        or args.push_state
        or args.verify_pin
        or args.command
        or args.toggle_smart_charging
        or args.set_push
    )
    fetch_all = args.all or not any_selected

    # Build config with MQTT disabled (debug tool — HTTP only)
    config = BydConfig.from_env(mqtt_enabled=False)
    include_req = args.include_request
    results: list[dict[str, Any]] = []

    async with BydClient(config) as client:
        # ── Login (always captured) ──
        transport: SecureTransport = client._transport  # type: ignore[assignment]

        login_result = await _fetch_login(config=config, transport=transport, include_request=include_req)

        if login_result["code"] != "0":
            print(f"Login failed: code={login_result['code']} message={login_result['message']}", file=sys.stderr)
            results.append(login_result)
            _emit(results, args)
            sys.exit(1)

        results.append(login_result)

        # Extract session tokens from login response to create a session
        token_data = login_result.get("data", {})
        token = token_data.get("token", {}) if isinstance(token_data, dict) else {}
        if not token.get("userId") or not token.get("signToken") or not token.get("encryToken"):
            print("Login response missing token fields.", file=sys.stderr)
            _emit(results, args)
            sys.exit(1)

        session = Session(
            user_id=str(token["userId"]),
            sign_token=str(token["signToken"]),
            encry_token=str(token["encryToken"]),
        )

        # ── Discover vehicles (always needed for VIN) ──
        vin = args.vin
        vins: list[str] = []

        if fetch_all or args.vehicles or not vin:
            veh_result = await _fetch_simple(
                name="vehicles",
                endpoint="/app/account/getAllListByUserId",
                config=config,
                session=session,
                transport=transport,
                include_request=include_req,
            )
            if fetch_all or args.vehicles:
                results.append(veh_result)

            # Extract VINs from response
            veh_data = veh_result.get("data")
            if isinstance(veh_data, list):
                for v in veh_data:
                    if isinstance(v, dict) and v.get("vin"):
                        vins.append(str(v["vin"]))

            if not vin and vins:
                vin = vins[0]

        if not vin:
            print("No VIN available. Specify --vin or ensure vehicles endpoint returns data.", file=sys.stderr)
            _emit(results, args)
            sys.exit(1)

        # Resolve PIN
        pin_hash = _resolve_pin(args.pin, config)

        # ── Read-only simple endpoints ──
        simple_endpoints: list[tuple[str, str, str, dict[str, str] | None]] = [
            ("energy", "/vehicleInfo/vehicle/getEnergyConsumption", "energy", None),
            ("charging", "/control/smartCharge/homePage", "charging", None),
            ("hvac", "/control/getStatusNow", "hvac", None),
            ("push_state", "/app/push/getPushSwitchState", "push_state", None),
        ]

        for flag_name, endpoint, name, extras in simple_endpoints:
            if fetch_all or getattr(args, flag_name, False):
                try:
                    r = await _fetch_simple(
                        name=name,
                        endpoint=endpoint,
                        config=config,
                        session=session,
                        transport=transport,
                        inner_extras=extras,
                        vin=vin,
                        include_request=include_req,
                    )
                    results.append(r)
                except Exception as exc:
                    results.append({"name": name, "endpoint": endpoint, "error": str(exc)})
                    if args.verbose:
                        traceback.print_exc(file=sys.stderr)

        # ── Latest config (uses vinList, not single VIN) ──
        if fetch_all or args.latest_config:
            try:
                vin_list = vins if vins else [vin]
                r = await _fetch_simple(
                    name="latest_config",
                    endpoint="/vehicle/vehicleswitch/getLatestConfig",
                    config=config,
                    session=session,
                    transport=transport,
                    inner_extras={
                        "appConfigVersion": "2",
                        "terminalType": "0",
                        "vinList": json.dumps(vin_list, ensure_ascii=False),
                    },
                    include_request=include_req,
                )
                results.append(r)
            except Exception as exc:
                results.append(
                    {"name": "latest_config", "endpoint": "/vehicle/vehicleswitch/getLatestConfig", "error": str(exc)}
                )
                if args.verbose:
                    traceback.print_exc(file=sys.stderr)

        # ── Trigger-then-poll endpoints ──
        if fetch_all or args.realtime:
            try:
                r = await _fetch_trigger_poll(
                    name="realtime",
                    trigger_endpoint="/vehicleInfo/vehicle/vehicleRealTimeRequest",
                    poll_endpoint="/vehicleInfo/vehicle/vehicleRealTimeResult",
                    config=config,
                    session=session,
                    transport=transport,
                    inner_extras={"energyType": "0", "tboxVersion": config.tbox_version},
                    vin=vin,
                    include_request=include_req,
                    poll_attempts=args.poll_attempts,
                    poll_interval=args.poll_interval,
                )
                results.append(r)
            except Exception as exc:
                results.append({"name": "realtime", "error": str(exc)})
                if args.verbose:
                    traceback.print_exc(file=sys.stderr)

        if fetch_all or args.gps:
            try:
                r = await _fetch_trigger_poll(
                    name="gps",
                    trigger_endpoint="/control/getGpsInfo",
                    poll_endpoint="/control/getGpsInfoResult",
                    config=config,
                    session=session,
                    transport=transport,
                    vin=vin,
                    include_request=include_req,
                    poll_attempts=args.poll_attempts,
                    poll_interval=args.poll_interval,
                )
                results.append(r)
            except Exception as exc:
                results.append({"name": "gps", "error": str(exc)})
                if args.verbose:
                    traceback.print_exc(file=sys.stderr)

        # ── PIN verification ──
        if args.verify_pin:
            if not pin_hash:
                print("--verify-pin requires --pin or BYD_CONTROL_PIN.", file=sys.stderr)
            else:
                try:
                    r = await _fetch_verify_pin(
                        config=config,
                        session=session,
                        transport=transport,
                        vin=vin,
                        pin_hash=pin_hash,
                        include_request=include_req,
                    )
                    results.append(r)
                except Exception as exc:
                    results.append({"name": "verify_pin", "error": str(exc)})
                    if args.verbose:
                        traceback.print_exc(file=sys.stderr)

        # ── Remote command ──
        if args.command:
            if not pin_hash:
                print(f"--command {args.command} requires --pin or BYD_CONTROL_PIN.", file=sys.stderr)
            elif not _confirm_write(f"command:{args.command}", skip=args.yes):
                pass
            else:
                cmd_name = f"command_{args.command.replace('-', '_')}"
                command_type = _REMOTE_COMMANDS[args.command]

                # Verify PIN first
                try:
                    pin_r = await _fetch_verify_pin(
                        config=config,
                        session=session,
                        transport=transport,
                        vin=vin,
                        pin_hash=pin_hash,
                        include_request=include_req,
                    )
                    results.append(pin_r)

                    if pin_r.get("code") != "0":
                        print(
                            f"PIN verification failed: code={pin_r.get('code')} " f"message={pin_r.get('message')}",
                            file=sys.stderr,
                        )
                    else:
                        r = await _fetch_remote_command(
                            name=cmd_name,
                            command_type=command_type,
                            config=config,
                            session=session,
                            transport=transport,
                            vin=vin,
                            pin_hash=pin_hash,
                            include_request=include_req,
                            poll_attempts=args.poll_attempts,
                            poll_interval=args.poll_interval,
                        )
                        results.append(r)
                except Exception as exc:
                    results.append({"name": cmd_name, "error": str(exc)})
                    if args.verbose:
                        traceback.print_exc(file=sys.stderr)

        # ── Toggle smart charging ──
        if args.toggle_smart_charging:
            enable = args.toggle_smart_charging == "on"
            action = f"smart_charging_{'on' if enable else 'off'}"
            if _confirm_write(action, skip=args.yes):
                try:
                    r = await _fetch_simple(
                        name=action,
                        endpoint="/control/smartCharge/changeChargeStatue",
                        config=config,
                        session=session,
                        transport=transport,
                        inner_extras={"smartChargeSwitch": "1" if enable else "0"},
                        vin=vin,
                        include_request=include_req,
                    )
                    results.append(r)
                except Exception as exc:
                    results.append({"name": action, "error": str(exc)})
                    if args.verbose:
                        traceback.print_exc(file=sys.stderr)

        # ── Set push notifications ──
        if args.set_push:
            enable = args.set_push == "on"
            action = f"push_set_{'on' if enable else 'off'}"
            if _confirm_write(action, skip=args.yes):
                try:
                    r = await _fetch_simple(
                        name=action,
                        endpoint="/app/push/setPushSwitchState",
                        config=config,
                        session=session,
                        transport=transport,
                        inner_extras={"pushSwitch": "1" if enable else "0"},
                        vin=vin,
                        include_request=include_req,
                    )
                    results.append(r)
                except Exception as exc:
                    results.append({"name": action, "error": str(exc)})
                    if args.verbose:
                        traceback.print_exc(file=sys.stderr)

    # ── Emit output ──
    _emit(results, args)


def _emit(results: list[dict[str, Any]], args: argparse.Namespace) -> None:
    """Format and output the results."""
    do_redact = not args.no_redact

    output: dict[str, Any] = {
        "timestamp": datetime.now(UTC).isoformat(),
        "results": [_redact_value(r) for r in results] if do_redact else results,
    }

    indent = 2 if args.pretty else None
    payload = json.dumps(output, indent=indent, default=str, ensure_ascii=False)

    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
        print(f"Written to {args.output}", file=sys.stderr)
    else:
        print(payload)


if __name__ == "__main__":
    asyncio.run(main())
