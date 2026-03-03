"""Window control capability."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class WindowsCapability:
    """Window close command.

    Close-windows is a fire-and-forget command.  No projections are
    registered because window state updates arrive asynchronously.
    """

    def __init__(
        self,
        *,
        close_fn: Callable[..., Awaitable[Any]],
        vin: str,
        execute_command: Callable[..., Awaitable[None]],
    ) -> None:
        self._close_fn = close_fn
        self._vin = vin
        self._execute = execute_command

    async def close(self) -> None:
        """Close all windows."""

        async def _cmd() -> Any:
            return await self._close_fn(self._vin)

        await self._execute(_cmd, [])
