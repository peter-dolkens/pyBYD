"""Smart charging control endpoints.

Endpoints:
  - /control/smartCharge/changeChargeStatue  (toggle on/off, or start/stop charge)
  - /control/smartCharge/changeResult        (poll until status change settles)
  - /control/smartCharge/saveOrUpdate        (save schedule)
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pybyd._api._common import ENDPOINT_NOT_SUPPORTED_CODES, build_inner_base, post_token_json
from pybyd._transport import Transport
from pybyd.config import BydConfig
from pybyd.models.control import CommandAck
from pybyd.session import Session

_TOGGLE_ENDPOINT = "/control/smartCharge/changeChargeStatue"
_RESULT_ENDPOINT = "/control/smartCharge/changeResult"
_SAVE_ENDPOINT = "/control/smartCharge/saveOrUpdate"

_logger = logging.getLogger(__name__)


async def toggle_smart_charging(
    config: BydConfig,
    session: Session,
    transport: Transport,
    vin: str,
    *,
    enable: bool,
) -> CommandAck:
    """Toggle smart charging on or off.

    Parameters
    ----------
    enable : bool
        True to enable smart charging, False to disable.

    Returns
    -------
    CommandAck
        Decoded API acknowledgement.
    """
    inner = build_inner_base(config, vin=vin)
    inner["smartChargeSwitch"] = str(1 if enable else 0)
    decoded = await post_token_json(
        endpoint=_TOGGLE_ENDPOINT,
        config=config,
        session=session,
        transport=transport,
        inner=inner,
        vin=vin,
        not_supported_codes=ENDPOINT_NOT_SUPPORTED_CODES,
    )
    raw = decoded if isinstance(decoded, dict) else {}
    return CommandAck.model_validate({"vin": vin, **raw, "raw": raw})


async def trigger_charge_change(
    config: BydConfig,
    session: Session,
    transport: Transport,
    vin: str,
    *,
    start: bool,
) -> tuple[dict[str, Any], str | None]:
    """POST ``/control/smartCharge/changeChargeStatue`` to start/stop charging.

    The toggle inner-payload always carries an empty ``timeZone`` (per the
    captured BYD-app traffic — see ``references/changeChargeStatue.md``).
    No other endpoint we implement uses this field, so it lives here
    rather than in the shared ``build_inner_base``.

    Returns ``(raw_response, request_serial)``.  The ``requestSerial`` is
    needed to correlate the follow-up MQTT push or ``changeResult`` poll.
    """
    inner = build_inner_base(config, vin=vin)
    inner["timeZone"] = ""
    inner["status"] = "1" if start else "0"
    decoded = await post_token_json(
        endpoint=_TOGGLE_ENDPOINT,
        config=config,
        session=session,
        transport=transport,
        inner=inner,
        vin=vin,
        not_supported_codes=ENDPOINT_NOT_SUPPORTED_CODES,
    )
    raw = decoded if isinstance(decoded, dict) else {}
    return raw, raw.get("requestSerial")


async def poll_charge_result(
    config: BydConfig,
    session: Session,
    transport: Transport,
    vin: str,
    request_serial: str,
) -> tuple[dict[str, Any], str | None]:
    """POST ``/control/smartCharge/changeResult`` to check toggle progress.

    Returns ``(raw_response, request_serial)``.  Caller (typically
    :meth:`BydClient._trigger_and_poll`) inspects ``raw["res"]``: ``1`` =
    still pending, ``2`` = success terminal, anything else = failure
    terminal.
    """
    inner = build_inner_base(config, vin=vin, request_serial=request_serial)
    decoded = await post_token_json(
        endpoint=_RESULT_ENDPOINT,
        config=config,
        session=session,
        transport=transport,
        inner=inner,
        vin=vin,
        not_supported_codes=ENDPOINT_NOT_SUPPORTED_CODES,
    )
    raw = decoded if isinstance(decoded, dict) else {}
    _logger.debug(
        "smartCharge changeResult vin=%s res=%s message=%s",
        vin,
        raw.get("res"),
        raw.get("message"),
    )
    return raw, raw.get("requestSerial") or request_serial


def make_change_charge_fetch_fn(*, start: bool) -> Callable[..., Awaitable[tuple[dict[str, Any], str | None]]]:
    """Adapter: select :func:`trigger_charge_change` or :func:`poll_charge_result`
    based on the endpoint URL :meth:`BydClient._trigger_and_poll` calls us with.

    The two operations have different inner-payload shapes (trigger needs
    ``status``/``timeZone``; poll needs ``requestSerial``), so a single
    generic ``fetch_fn`` like :func:`pybyd._api.realtime.fetch_realtime_endpoint`
    doesn't apply — we dispatch to the right helper at the seam.
    """

    async def _fetch(
        endpoint: str,
        config: BydConfig,
        session: Session,
        transport: Transport,
        vin: str,
        request_serial: str | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        if endpoint == _RESULT_ENDPOINT:
            return await poll_charge_result(config, session, transport, vin, request_serial or "")
        return await trigger_charge_change(config, session, transport, vin, start=start)

    return _fetch


def is_charge_change_ready(payload: dict[str, Any]) -> bool:
    """Predicate for :meth:`BydClient._trigger_and_poll`'s ``is_ready``.

    The trigger response carries only ``requestSerial`` (not ready).
    The result/MQTT-push response carries ``res``: ``1`` = pending (keep
    polling), ``2`` = success (terminal, ready), other values = failure
    (also terminal — surface to the caller, who decides whether to raise).
    """
    res = payload.get("res")
    return isinstance(res, int) and res != 1


async def save_charging_schedule(
    config: BydConfig,
    session: Session,
    transport: Transport,
    vin: str,
    *,
    target_soc: int,
    start_hour: int,
    start_minute: int,
    end_hour: int,
    end_minute: int,
) -> CommandAck:
    """Save a smart charging schedule.

    Parameters
    ----------
    target_soc : int
        Target state of charge (0-100).
    start_hour : int
        Scheduled start hour (0-23).
    start_minute : int
        Scheduled start minute (0-59).
    end_hour : int
        Scheduled end hour (0-23).
    end_minute : int
        Scheduled end minute (0-59).

    Returns
    -------
    CommandAck
        Decoded API acknowledgement.
    """
    inner = build_inner_base(config, vin=vin)
    inner.update(
        {
            "endHour": str(end_hour),
            "endMinute": str(end_minute),
            "startHour": str(start_hour),
            "startMinute": str(start_minute),
            "targetSoc": str(target_soc),
        }
    )
    decoded = await post_token_json(
        endpoint=_SAVE_ENDPOINT,
        config=config,
        session=session,
        transport=transport,
        inner=inner,
        vin=vin,
        not_supported_codes=ENDPOINT_NOT_SUPPORTED_CODES,
    )
    raw = decoded if isinstance(decoded, dict) else {}
    return CommandAck.model_validate({"vin": vin, **raw, "raw": raw})
