"""Value-quality validators for vehicle state data.

These predicates reject suspicious transient data points (GPS Null Island,
zero-SOC spikes) before they enter the vehicle state engine.  Migrated from
the hass-byd ``value_guard.py`` module so that any pyBYD consumer benefits.
"""

from __future__ import annotations

from pybyd.models.gps import GpsInfo

_GPS_NULL_ISLAND_THRESHOLD: float = 0.1


def guard_gps_coordinates(
    previous: GpsInfo | None,
    incoming: GpsInfo | None,
) -> GpsInfo | None:
    """Return the best available GpsInfo, preferring *incoming*.

    Falls back to *previous* when *incoming* has ``None`` coordinates
    or suspiciously near-zero ``(0, 0)`` "Null Island" coordinates.
    On first startup (``previous=None``) always returns *incoming*.
    """
    if incoming is None:
        return previous
    if previous is None:
        return incoming
    lat, lon = incoming.latitude, incoming.longitude
    if lat is None and lon is None:
        return previous
    if (
        lat is not None
        and lon is not None
        and abs(lat) < _GPS_NULL_ISLAND_THRESHOLD
        and abs(lon) < _GPS_NULL_ISLAND_THRESHOLD
    ):
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
