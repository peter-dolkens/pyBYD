"""Smart charging status endpoint.

Endpoint:
  - /control/smartCharge/homePage  (live state + configured schedule, one call)

The endpoint returns both the live charging state (SOC, charge state,
time-to-full) and the user's configured schedule (start/end times,
``chargeWay``, etc.) in a single response.  We parse the same payload
into two specialised models so each can evolve independently:

* :class:`ChargingStatus` — live fields, also pushed via MQTT.
* :class:`SmartChargingSchedule` — schedule config, HTTP-only.

Callers that only care about one view should use
:func:`fetch_charging_status` or :func:`pybyd._api.smart_charging.fetch_charging_schedule`.
:func:`fetch_charging_homepage` returns the raw dict for callers that
want to populate both with a single request.
"""

from __future__ import annotations

from typing import Any

from pybyd._api._common import (
    ENDPOINT_NOT_SUPPORTED_CODES,
    build_inner_base,
    post_token_json,
)
from pybyd._transport import Transport
from pybyd.config import BydConfig
from pybyd.models.charging import ChargingStatus
from pybyd.session import Session

_ENDPOINT = "/control/smartCharge/homePage"


async def fetch_charging_homepage(
    config: BydConfig,
    session: Session,
    transport: Transport,
    vin: str,
) -> dict[str, Any]:
    """POST ``/control/smartCharge/homePage`` and return the raw response dict.

    Both :func:`fetch_charging_status` and
    :func:`pybyd._api.smart_charging.fetch_charging_schedule` build on
    this — and :meth:`pybyd.client.BydClient.get_charging_homepage`
    parses one raw response into both views, so a "force refresh" only
    spends one round-trip to update live state *and* schedule together.
    """
    inner = build_inner_base(config, vin=vin)
    decoded = await post_token_json(
        endpoint=_ENDPOINT,
        config=config,
        session=session,
        transport=transport,
        inner=inner,
        vin=vin,
        not_supported_codes=ENDPOINT_NOT_SUPPORTED_CODES,
    )
    return decoded if isinstance(decoded, dict) else {}


async def fetch_charging_status(
    config: BydConfig,
    session: Session,
    transport: Transport,
    vin: str,
) -> ChargingStatus:
    """Fetch smart charging status (SOC, charge state, time-to-full)."""
    raw = await fetch_charging_homepage(config, session, transport, vin)
    return ChargingStatus.model_validate(raw)
