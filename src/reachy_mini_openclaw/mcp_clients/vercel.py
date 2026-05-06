from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

VERCEL_API = "https://api.vercel.com"
DEFAULT_TIMEOUT = 15.0


@dataclass(frozen=True)
class Deployment:
    uid: str                # Vercel deployment ID
    project_name: str       # human-readable project (== "name" field)
    state: str              # READY | ERROR | CANCELED | BUILDING | QUEUED | INITIALIZING
    created_at: datetime
    url: str                # vercel.app preview URL
    inspector_url: str      # vercel.com dashboard link
    target: Optional[str]   # "production" | "staging" | None
    raw: Dict[str, Any]


def _ms_to_dt(ms: Optional[int]) -> Optional[datetime]:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


class VercelClient:
    """Tiny async Vercel client. Personal account by default (no team_id)."""

    def __init__(self, token: str, *, base_url: str = VERCEL_API) -> None:
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

    async def list_recent_deployments(
        self,
        *,
        limit: int = 20,
        project: Optional[str] = None,
    ) -> List[Deployment]:
        """Return up to `limit` most-recent deployments for the personal
        account. `project` filters by Vercel project ID or name; None returns
        all watched projects."""
        params: Dict[str, Any] = {"limit": min(max(limit, 1), 100)}
        if project:
            params["projectId"] = project

        resp = await self._client.get("/v6/deployments", params=params)
        if resp.status_code == 401:
            logger.error("vercel: 401 unauthorized — check token")
            resp.raise_for_status()
        if resp.status_code == 403:
            logger.warning("vercel: 403 — token lacks permission for /v6/deployments")
            resp.raise_for_status()
        resp.raise_for_status()
        body = resp.json()
        deployments_raw = body.get("deployments") or body.get("data") or []

        out: List[Deployment] = []
        for d in deployments_raw:
            created = _ms_to_dt(d.get("createdAt") or d.get("created"))
            if created is None:
                created = datetime.now(timezone.utc)
            out.append(Deployment(
                uid=d.get("uid") or d.get("id") or "",
                project_name=d.get("name", ""),
                state=d.get("state") or d.get("readyState") or "UNKNOWN",
                created_at=created,
                url=d.get("url", ""),
                inspector_url=d.get("inspectorUrl", ""),
                target=d.get("target"),
                raw=d,
            ))
        return out
