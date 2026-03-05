"""Central filtering/validation layer for incoming vehicle telemetry models.

This module is the single place where pyBYD applies *quality filtering* to
incoming payloads before values are persisted by the state engine.

Design goals
------------
1. Keep filtering rules in one place.
2. Support both section-level and per-field filtering patterns.
3. Make extension safe and explicit, so new rules are easy to add.

Two filtering styles are used:

- **Section-level filters** (example: GPS):
    operate on the whole model because validity depends on multiple fields
    together (for example latitude + longitude).
- **Per-field filters** (example: realtime lock/SOC):
    operate on one field at a time and can preserve previous values when
    incoming values are missing or non-authoritative.

Execution model
---------------
- State engine calls ``apply_realtime_filters(previous, incoming)`` for
    realtime payloads and ``apply_gps_filters(previous, incoming)`` for GPS.
- A filter may keep the incoming value unchanged, or request a replacement
    (usually the last known value).
- Realtime per-field filters use a sentinel contract:
    return ``_MISSING`` to indicate "no change".

How to extend with more filters
-------------------------------
1. Add a new filter function with signature ``RealtimeFieldFilter`` when
     handling a single realtime field.
2. Register it in ``_REALTIME_FIELD_FILTERS`` under the target field name.
     Multiple filters can be chained per field; first non-``_MISSING`` wins.
3. For model-wide logic (cross-field dependencies), add/extend an
     ``apply_<section>_filters`` function that processes the full model.
4. Add regression tests for:
     - preserve previous value case,
     - accept incoming authoritative value case,
     - first payload / no-previous-value case.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pybyd.models.gps import GpsInfo
from pybyd.models.realtime import LockState, VehicleRealtimeData

_GPS_NULL_ISLAND_THRESHOLD: float = 0.1
_MISSING: object = object()

_LOCK_FIELD_NAMES: tuple[str, ...] = (
    "left_front_door_lock",
    "right_front_door_lock",
    "left_rear_door_lock",
    "right_rear_door_lock",
    "sliding_door_lock",
)

RealtimeFieldFilter = Callable[[str, Any, Any, bool], Any | object]
"""Per-field realtime filter callback.

Args:
    field_name: Canonical model field name being filtered.
    previous_value: Stored/last-known field value.
    incoming_value: Incoming field value (or ``_MISSING`` when absent).
    incoming_present: Whether field exists in incoming model payload.

Returns:
    - replacement value to override incoming field value, or
    - ``_MISSING`` to leave incoming field value unchanged.
"""


def _has_valid_coordinates(gps: GpsInfo) -> bool:
    """Return True when *gps* carries a usable latitude/longitude pair.

    A coordinate pair is considered **invalid** (returns False) when:
    - either latitude or longitude is ``None`` (partial fix is useless), or
    - both values fall within the Null Island threshold (API artefact).
    """
    lat, lon = gps.latitude, gps.longitude
    if lat is None or lon is None:
        return False
    return not (abs(lat) < _GPS_NULL_ISLAND_THRESHOLD and abs(lon) < _GPS_NULL_ISLAND_THRESHOLD)


def guard_gps_coordinates(
    previous: GpsInfo | None,
    incoming: GpsInfo | None,
) -> GpsInfo | None:
    """Return the best available GpsInfo, preferring *incoming*.

    Falls back to *previous* when *incoming* has incomplete coordinates
    (either value is ``None``), suspiciously near-zero ``(0, 0)``
    "Null Island" coordinates, or is ``None`` itself.

    On first startup (``previous is None``) the same validity checks
    apply; ``None`` is returned when no usable coordinates exist yet.
    """
    if incoming is None:
        return previous
    if not _has_valid_coordinates(incoming):
        return previous
    return incoming


def keep_previous_when_zero(
    previous: float | None,
    incoming: float | None,
) -> float | None:
    """Return *previous* when *incoming* is zero, otherwise *incoming*.

    Used to guard against transient zero-SOC spikes from the BYD API.
    """
    if incoming is not None and incoming == 0 and previous is not None:
        return previous
    return incoming


def _preserve_previous_lock_filter(
    _: str,
    previous_value: Any,
    incoming_value: Any,
    incoming_present: bool,
) -> Any | object:
    """Keep previous lock state when incoming value is missing or unavailable."""
    if not incoming_present or incoming_value == LockState.UNAVAILABLE:
        return previous_value
    return _MISSING


def _keep_previous_when_soc_zero_filter(
    _: str,
    previous_value: Any,
    incoming_value: Any,
    incoming_present: bool,
) -> Any | object:
    """Keep previous SOC when incoming value is a transient zero spike."""
    if not incoming_present:
        return _MISSING
    guarded_soc = keep_previous_when_zero(previous_value, incoming_value)
    if guarded_soc != incoming_value:
        return guarded_soc
    return _MISSING


_REALTIME_FIELD_FILTERS: dict[str, tuple[RealtimeFieldFilter, ...]] = {
    **{field_name: (_preserve_previous_lock_filter,) for field_name in _LOCK_FIELD_NAMES},
    "elec_percent": (_keep_previous_when_soc_zero_filter,),
}


def _apply_model_field_filters(
    previous: object,
    incoming: object,
    field_filters: dict[str, tuple[RealtimeFieldFilter, ...]],
) -> dict[str, Any]:
    """Apply per-field filters and return update overrides.

    Filtering rules are applied per field in registration order.
    For each field, the first filter returning a non-``_MISSING`` value wins.
    """
    updates: dict[str, Any] = {}
    incoming_fields: set[str] = set(getattr(incoming, "model_fields_set", set()))

    for field_name, filters in field_filters.items():
        previous_value = getattr(previous, field_name, _MISSING)
        if previous_value is _MISSING:
            continue

        incoming_present = field_name in incoming_fields
        incoming_value = getattr(incoming, field_name, _MISSING) if incoming_present else _MISSING

        for filter_func in filters:
            filtered_value = filter_func(
                field_name,
                previous_value,
                incoming_value,
                incoming_present,
            )
            if filtered_value is _MISSING:
                continue
            updates[field_name] = filtered_value
            break

    return updates


def apply_realtime_filters(
    previous: VehicleRealtimeData | None,
    incoming: VehicleRealtimeData,
) -> VehicleRealtimeData:
    """Apply all realtime filtering rules in one place.

    Rules currently include:
    - per-door lock stability (preserve previous on missing/UNAVAILABLE),
    - SOC zero-spike protection.

    The returned model is either:
    - the original incoming payload when no override is needed, or
    - a copy with field overrides from registered filters.
    """
    if previous is None:
        return incoming

    updates = _apply_model_field_filters(previous, incoming, _REALTIME_FIELD_FILTERS)
    if updates:
        return incoming.model_copy(update=updates)
    return incoming


def apply_gps_filters(
    previous: GpsInfo | None,
    incoming: GpsInfo | None,
) -> GpsInfo | None:
    """Apply all GPS filtering rules in one place."""
    return guard_gps_coordinates(previous, incoming)
