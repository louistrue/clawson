from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

TODOIST_API = "https://api.todoist.com/rest/v2"
DEFAULT_TIMEOUT = 15.0


@dataclass(frozen=True)
class Task:
    id: str
    content: str
    description: str
    project_id: Optional[str]
    is_completed: bool
    due_iso: Optional[str]      # YYYY-MM-DD or full datetime if dt-due
    due_is_recurring: bool
    url: str
    raw: Dict[str, Any]


def _parse_task(d: Dict[str, Any]) -> Task:
    due = d.get("due") or {}
    return Task(
        id=str(d.get("id", "")),
        content=d.get("content", ""),
        description=d.get("description", ""),
        project_id=d.get("project_id"),
        is_completed=bool(d.get("is_completed")),
        due_iso=due.get("datetime") or due.get("date"),
        due_is_recurring=bool(due.get("is_recurring")),
        url=d.get("url", ""),
        raw=d,
    )


class TodoistClient:
    """Tiny async Todoist REST client."""

    def __init__(self, token: str, *, base_url: str = TODOIST_API) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "clawson-reachy-mini/0.1",
            },
            timeout=DEFAULT_TIMEOUT,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def list_tasks(self, *, filter_str: Optional[str] = None) -> List[Task]:
        """List active (non-completed) tasks. `filter_str` uses Todoist's
        filter syntax — e.g. 'today', 'overdue', '@home & p1'."""
        params: Dict[str, Any] = {}
        if filter_str:
            params["filter"] = filter_str
        resp = await self._client.get("/tasks", params=params)
        if resp.status_code == 401:
            logger.error("todoist: 401 unauthorized — check token")
        resp.raise_for_status()
        return [_parse_task(t) for t in resp.json()]

    async def create_task(
        self,
        content: str,
        *,
        due_string: Optional[str] = None,
        description: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> Task:
        """Create a new task. `due_string` accepts natural language like
        'tomorrow at 9am' or 'every Friday'."""
        body: Dict[str, Any] = {"content": content}
        if due_string:
            body["due_string"] = due_string
        if description:
            body["description"] = description
        if project_id:
            body["project_id"] = project_id
        resp = await self._client.post("/tasks", json=body)
        resp.raise_for_status()
        return _parse_task(resp.json())
