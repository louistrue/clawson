from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)


DEFAULT_TIMEOUT_S = 8.0


@dataclass
class _Pending:
    description: str
    future: asyncio.Future


class ConfirmationSystem:
    """Single-slot pending confirmation register.

    Only one confirmation can be in flight at a time. A new request
    supersedes (auto-denies) the prior one — the LLM can change its
    mind mid-prompt and we shouldn't double-execute.

    Resolved by `confirm()` / `deny()` (called from the antenna handler
    or any other surface) or by timeout inside `request()`.
    """

    def __init__(self) -> None:
        self._pending: Optional[_Pending] = None
        self._lock = asyncio.Lock()
        # Listeners fire (is_pending: bool) whenever the pending state
        # changes — used by main.py to pause face tracking during a
        # confirmation so the head doesn't drift on the user.
        self._listeners: list = []

    def add_listener(self, cb: Callable[[bool], Awaitable[None]]) -> None:
        self._listeners.append(cb)

    async def _notify(self, pending: bool) -> None:
        for cb in self._listeners:
            try:
                await cb(pending)
            except Exception as e:
                logger.debug("confirmation listener failed: %s", e)

    @property
    def has_pending(self) -> bool:
        return self._pending is not None and not self._pending.future.done()

    @property
    def pending_description(self) -> Optional[str]:
        return self._pending.description if self._pending is not None else None

    async def request(
        self,
        description: str,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        on_announce: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> bool:
        """Ask for confirmation; return True/False (deny/timeout = False)."""
        async with self._lock:
            # New request preempts a stale prior one.
            if self._pending is not None and not self._pending.future.done():
                self._pending.future.set_result(False)
            future: asyncio.Future = asyncio.get_event_loop().create_future()
            my_pending = _Pending(description=description, future=future)
            self._pending = my_pending

        await self._notify(True)

        if on_announce is not None:
            try:
                await on_announce(
                    f"{description}. Nod or right antenna to confirm, "
                    f"shake or left to cancel."
                )
            except Exception as e:
                logger.debug("confirm announce failed: %s", e)

        try:
            return await asyncio.wait_for(future, timeout=timeout_s)
        except asyncio.TimeoutError:
            logger.info("confirm timed out: %s", description[:60])
            return False
        finally:
            # Only clear the slot if WE are still the active pending — a
            # superseding request will have replaced us already.
            async with self._lock:
                if self._pending is my_pending:
                    self._pending = None
                    await self._notify(False)

    def confirm(self) -> bool:
        """Resolve the pending confirmation as True. Returns False if there
        was nothing to confirm."""
        if self._pending is None or self._pending.future.done():
            return False
        self._pending.future.set_result(True)
        return True

    def deny(self) -> bool:
        if self._pending is None or self._pending.future.done():
            return False
        self._pending.future.set_result(False)
        return True
