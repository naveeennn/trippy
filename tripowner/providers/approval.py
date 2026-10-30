"""Human-in-the-loop approval. Dashboard buttons for the demo; add Telegram the same way."""
import asyncio
import itertools
from abc import ABC, abstractmethod


class ApprovalProvider(ABC):
    @abstractmethod
    async def request(self, summary: str, details: dict) -> bool: ...


class AutoApprove(ApprovalProvider):
    """Used in eval runs: the simulated user always says yes."""

    async def request(self, summary: str, details: dict) -> bool:
        return True


class DashboardApproval(ApprovalProvider):
    """Publishes a request to the dashboard and waits (sim clock paused) for a click."""

    def __init__(self, bus, timeout_s: float = 300):
        self.bus = bus
        self.timeout_s = timeout_s
        self._pending: dict[str, asyncio.Future] = {}
        self._ids = itertools.count(1)

    async def request(self, summary: str, details: dict) -> bool:
        req_id = f"A{next(self._ids)}"
        fut = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        self.bus.publish("approval_request", id=req_id, summary=summary, details=details)
        try:
            approved = await asyncio.wait_for(fut, self.timeout_s)
        except asyncio.TimeoutError:
            approved = False
        finally:
            self._pending.pop(req_id, None)
        self.bus.publish("approval_result", id=req_id, approved=approved)
        return approved

    def resolve(self, req_id: str, approved: bool) -> bool:
        fut = self._pending.get(req_id)
        if fut is None or fut.done():
            return False
        fut.set_result(approved)
        return True
