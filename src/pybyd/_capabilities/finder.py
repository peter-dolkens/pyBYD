"""Vehicle finder capability (find car / flash lights)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class FinderCapability:
    """Find-my-car and flash lights commands.

    These are fire-and-forget commands with no observable state change,
    so no projections are registered.
    """

    def __init__(
        self,
        *,
        find_fn: Callable[..., Awaitable[Any]],
        flash_fn: Callable[..., Awaitable[Any]],
        vin: str,
        execute_command: Callable[..., Awaitable[None]],
    ) -> None:
        self._find_fn = find_fn
        self._flash_fn = flash_fn
        self._vin = vin
        self._execute = execute_command

    async def find(self) -> None:
        """Activate find-my-car (horn + lights)."""

        async def _cmd() -> Any:
            return await self._find_fn(self._vin)

        await self._execute(_cmd, [])

    async def flash_lights(self) -> None:
        """Flash vehicle lights."""

        async def _cmd() -> Any:
            return await self._flash_fn(self._vin)

        await self._execute(_cmd, [])
