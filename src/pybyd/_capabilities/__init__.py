"""Capability namespace classes for BydCar.

Each capability encapsulates a group of related vehicle commands with
their associated projection specifications.
"""

from pybyd._capabilities.battery_heat import BatteryHeatCapability
from pybyd._capabilities.finder import FinderCapability
from pybyd._capabilities.hvac import HvacCapability
from pybyd._capabilities.lock import LockCapability
from pybyd._capabilities.seat import SeatCapability, SeatLevel, SeatPosition
from pybyd._capabilities.steering import SteeringCapability
from pybyd._capabilities.windows import WindowsCapability

__all__ = [
    "BatteryHeatCapability",
    "FinderCapability",
    "HvacCapability",
    "LockCapability",
    "SeatCapability",
    "SeatLevel",
    "SeatPosition",
    "SteeringCapability",
    "WindowsCapability",
]
