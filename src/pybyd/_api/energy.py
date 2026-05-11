"""Energy consumption endpoint.

Endpoint:
  - /vehicleInfo/vehicle/getEnergyConsumption (single request)
"""

from __future__ import annotations

from typing import Any

from pybyd._api._common import ENDPOINT_NOT_SUPPORTED_CODES, build_inner_base, post_token_json
from pybyd._transport import Transport
from pybyd.config import BydConfig
from pybyd.models.energy import EnergyConsumption
from pybyd.models.vehicle import EnergyType
from pybyd.session import Session

_ENDPOINT = "/vehicleInfo/vehicle/getEnergyConsumption"


async def fetch_energy_consumption(
    config: BydConfig,
    session: Session,
    transport: Transport,
    vin: str,
    *,
    power_type: EnergyType = EnergyType.EV,
    auto_model_name: str | None = None,
) -> EnergyConsumption:
    """Fetch energy consumption data for a vehicle.

    The BYD app sends three request fields beyond ``build_inner_base``:

    - ``powerType``: ``EnergyType`` — sent on the wire as ``"0"`` (EV view),
      ``"1"`` (ICE view), or ``"2"`` (hybrid combined). Hybrids only populate
      both legs at ``HYBRID``.
    - ``requestType``: ``0`` (int) — purpose unknown but the BYD app sends it.
    - ``autoModelNameOut``: vehicle's marketing model name (e.g. ``"BYD SHARK"``).
      Without it the ``autoModelGraph`` section zero-fills.

    Parameters
    ----------
    config : BydConfig
        Client configuration.
    session : Session
        Authenticated session.
    transport : Transport
        HTTP transport.
    vin : str
        Vehicle Identification Number.
    power_type : EnergyType
        Defaults to ``EnergyType.EV`` to preserve the old hard-coded
        behaviour for any direct caller. ``BydClient.get_energy_consumption``
        supplies the per-vehicle ``energy_type`` value.
    auto_model_name : str | None
        Vehicle marketing name for the ``autoModelGraph`` lookup. Optional
        — if absent, ``autoModelGraph`` returns zeros.

    Returns
    -------
    EnergyConsumption
        Energy consumption data.

    Raises
    ------
    BydApiError
        If the API returns an error.
    """
    inner: dict[str, Any] = dict(build_inner_base(config, vin=vin))
    inner["powerType"] = str(int(power_type))
    inner["requestType"] = 0
    if auto_model_name:
        inner["autoModelNameOut"] = auto_model_name
    decoded = await post_token_json(
        endpoint=_ENDPOINT,
        config=config,
        session=session,
        transport=transport,
        inner=inner,
        vin=vin,
        not_supported_codes=ENDPOINT_NOT_SUPPORTED_CODES,
    )
    return EnergyConsumption.model_validate(decoded)
