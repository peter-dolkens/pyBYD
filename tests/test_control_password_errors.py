"""Tests for control-password API error-code mapping."""

from __future__ import annotations

import pytest

from pybyd._api._common import _raise_for_code
from pybyd._api.control import _CONTROL_EXTRA_CODES, _REMOTE_CONTROL_EXTRA_CODES
from pybyd.exceptions import BydApiError, BydControlPasswordError, BydRemoteControlError


@pytest.mark.parametrize("code", ["5005", "5006", "5011"])
def test_verify_control_password_codes_raise_control_password_error(code: str) -> None:
    """verifyControlPassword-related codes map to BydControlPasswordError."""
    with pytest.raises(BydControlPasswordError) as exc_info:
        _raise_for_code(
            endpoint="/vehicle/vehicleswitch/verifyControlPassword",
            code=code,
            message="test-message",
            extra_code_map=_CONTROL_EXTRA_CODES,
        )

    assert exc_info.value.code == code


@pytest.mark.parametrize("code", ["5005", "5006", "5011"])
def test_remote_control_codes_raise_control_password_error(code: str) -> None:
    """Remote-control trigger/poll paths keep control-password mapping."""
    with pytest.raises(BydControlPasswordError) as exc_info:
        _raise_for_code(
            endpoint="/control/remoteControl",
            code=code,
            message="test-message",
            extra_code_map=_REMOTE_CONTROL_EXTRA_CODES,
        )

    assert exc_info.value.code == code


def test_remote_control_service_code_still_maps_to_remote_control_error() -> None:
    """Regression guard: service-level remote-control code remains unchanged."""
    with pytest.raises(BydRemoteControlError) as exc_info:
        _raise_for_code(
            endpoint="/control/remoteControl",
            code="1009",
            message="service-error",
            extra_code_map=_REMOTE_CONTROL_EXTRA_CODES,
        )

    assert exc_info.value.code == "1009"


def test_unknown_code_falls_back_to_generic_api_error() -> None:
    """Unmapped codes still raise the generic API exception."""
    with pytest.raises(BydApiError) as exc_info:
        _raise_for_code(
            endpoint="/vehicle/vehicleswitch/verifyControlPassword",
            code="5999",
            message="unknown",
            extra_code_map=_CONTROL_EXTRA_CODES,
        )

    assert exc_info.value.code == "5999"
