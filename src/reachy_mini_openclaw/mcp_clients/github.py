from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"
ACCEPT_HEADER = "application/vnd.github+json"
API_VERSION = "2022-11-28"
DEFAULT_TIMEOUT = 15.0


@dataclass(frozen=True)
class Repo:
    full_name: str
    default_branch: str
    pushed_at: datetime
    archived: bool


@dataclass(frozen=True)
class Notification:
    id: str
    reason: str           # "review_requested" | "mention" | "assign" | …
    type: str             # "PullRequest" | "Issue" | "Commit"
    title: str
    repo: str             # owner/name
    url: str              # API URL of the subject (we surface a web link)
    updated_at: datetime
    raw: dict


@dataclass(frozen=True)
class WorkflowRun:
    id: int
    repo: str
    branch: Optional[str]
    status: str           # "queued" | "in_progress" | "completed"
    conclusion: Optional[str]   # "success" | "failure" | "cancelled" | …
    actor_login: Optional[str]
    html_url: str
    name: str             # workflow name
    created_at: datetime
    updated_at: datetime


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    # GitHub returns RFC3339 with trailing 'Z'.
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


class GitHubClient:
    """Tiny async GitHub REST client. ETag-aware to avoid rate-limit burn."""

    def __init__(self, token: str, *, base_url: str = GITHUB_API) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": ACCEPT_HEADER,
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": "clawson-reachy-mini/0.1",
            },
            timeout=DEFAULT_TIMEOUT,
        )
        self._etags: Dict[str, str] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Internal HTTP helpers
    # ------------------------------------------------------------------
    async def _get_json(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        cache_key: Optional[str] = None,
    ) -> Optional[Any]:
        """GET path. If cache_key is given, send If-None-Match and treat 304
        as 'no change' (returns None)."""
        headers = {}
        if cache_key and cache_key in self._etags:
            headers["If-None-Match"] = self._etags[cache_key]

        resp = await self._client.get(path, params=params, headers=headers)
        if cache_key and resp.status_code == 304:
            return None
        if cache_key and "ETag" in resp.headers:
            self._etags[cache_key] = resp.headers["ETag"]
        if resp.status_code == 401:
            logger.error("github: 401 unauthorized — check token")
            resp.raise_for_status()
        if resp.status_code == 403:
            remaining = resp.headers.get("X-RateLimit-Remaining")
            reset = resp.headers.get("X-RateLimit-Reset")
            logger.warning("github: 403 (remaining=%s reset=%s)", remaining, reset)
            resp.raise_for_status()
        resp.raise_for_status()
        return resp.json()

    async def _get_paginated(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        max_pages: int = 5,
    ) -> List[Any]:
        """Walk Link headers up to max_pages. Used when we need the full set."""
        params = dict(params or {})
        params.setdefault("per_page", 100)
        results: List[Any] = []
        url = path
        for _ in range(max_pages):
            resp = await self._client.get(url, params=params if url == path else None)
            resp.raise_for_status()
            results.extend(resp.json())
            link = resp.headers.get("Link", "")
            next_url = _next_link(link)
            if not next_url:
                break
            url = next_url
        return results

    # ------------------------------------------------------------------
    # Public API surface
    # ------------------------------------------------------------------
    async def get_authenticated_user(self) -> str:
        data = await self._get_json("/user")
        return data["login"] if data else ""

    async def list_member_repos(self, *, max_pages: int = 5) -> List[Repo]:
        """All repos I'm a member of, sorted most-recent-push first."""
        raw = await self._get_paginated(
            "/user/repos",
            params={
                "affiliation": "owner,collaborator,organization_member",
                "sort": "pushed",
                "direction": "desc",
                "per_page": 100,
            },
            max_pages=max_pages,
        )
        out: List[Repo] = []
        for r in raw:
            pushed = _parse_dt(r.get("pushed_at")) or datetime.now(timezone.utc)
            out.append(Repo(
                full_name=r["full_name"],
                default_branch=r.get("default_branch", "main"),
                pushed_at=pushed,
                archived=bool(r.get("archived")),
            ))
        return out

    async def list_notifications(
        self,
        *,
        since: Optional[datetime] = None,
        all_notifications: bool = False,
    ) -> List[Notification]:
        """Unread + participating notifications. `since` reduces traffic."""
        params: Dict[str, Any] = {"all": "true" if all_notifications else "false"}
        if since is not None:
            params["since"] = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw = await self._get_json("/notifications", params=params, cache_key="notifications") or []
        out: List[Notification] = []
        for n in raw:
            subject = n.get("subject") or {}
            repo = (n.get("repository") or {}).get("full_name", "")
            updated = _parse_dt(n.get("updated_at")) or datetime.now(timezone.utc)
            out.append(Notification(
                id=n["id"],
                reason=n.get("reason", ""),
                type=subject.get("type", ""),
                title=subject.get("title", ""),
                repo=repo,
                url=subject.get("url", ""),
                updated_at=updated,
                raw=n,
            ))
        return out

    async def list_workflow_runs(
        self,
        repo: str,
        *,
        per_page: int = 10,
        branch: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> List[WorkflowRun]:
        params: Dict[str, Any] = {"per_page": per_page}
        if branch:
            params["branch"] = branch
        if actor:
            params["actor"] = actor
        cache_key = f"runs:{repo}:{branch or '-'}:{actor or '-'}"
        raw = await self._get_json(
            f"/repos/{repo}/actions/runs",
            params=params,
            cache_key=cache_key,
        )
        if raw is None:
            return []  # 304 — no change since last poll
        out: List[WorkflowRun] = []
        for r in raw.get("workflow_runs", []):
            out.append(WorkflowRun(
                id=r["id"],
                repo=repo,
                branch=r.get("head_branch"),
                status=r.get("status", "completed"),
                conclusion=r.get("conclusion"),
                actor_login=(r.get("actor") or {}).get("login"),
                html_url=r.get("html_url", ""),
                name=r.get("name", ""),
                created_at=_parse_dt(r.get("created_at")) or datetime.now(timezone.utc),
                updated_at=_parse_dt(r.get("updated_at")) or datetime.now(timezone.utc),
            ))
        return out


def _next_link(link_header: str) -> Optional[str]:
    """Parse RFC5988 Link header for rel='next'."""
    if not link_header:
        return None
    for part in link_header.split(","):
        section = part.strip()
        if not section.endswith('rel="next"'):
            continue
        url_start = section.find("<")
        url_end = section.find(">")
        if url_start == -1 or url_end == -1:
            continue
        return section[url_start + 1 : url_end]
    return None
