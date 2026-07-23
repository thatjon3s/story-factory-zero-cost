from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import httpx

try:
    from .memory import slugify
except ImportError:
    from memory import slugify


class SupabaseControlPlane:
    """Supabase-first episode state machine and append-only event log."""

    def __init__(self) -> None:
        self.base_url = os.environ["SUPABASE_URL"].rstrip("/")
        self.key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
        self.owner_id = os.getenv("STORY_FACTORY_OWNER", "story-factory")
        self.headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        table: str,
        *,
        params: dict[str, str] | None = None,
        payload: Any = None,
        prefer: str = "",
    ) -> Any:
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        response = httpx.request(
            method,
            f"{self.base_url}/rest/v1/{table}",
            headers=headers,
            params=params,
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        return response.json() if response.content else None

    def ensure_labels(self) -> None:
        return

    def get(self, episode_id: int) -> dict[str, Any]:
        rows = self._request(
            "GET", "episodes",
            params={"select": "*", "id": f"eq.{episode_id}", "limit": "1"},
        ) or []
        if not rows:
            raise RuntimeError(f"Episode {episode_id} was not found")
        return rows[0]

    def episodes(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        params = {
            "select": "*",
            "order": "series_slug,episode_no",
            "limit": str(limit),
        }
        if status:
            params["status"] = f"eq.{status}"
        return self._request("GET", "episodes", params=params) or []

    def create_automatic_idea(self) -> dict[str, Any]:
        series = os.getenv("AUTO_SERIES_NAME", "Die letzte Leitung")
        universe = os.getenv("AUTO_UNIVERSE_NAME", "Story Factory Universe")
        series_slug = slugify(series)
        universe_slug = slugify(universe)
        existing = self._request(
            "GET", "episodes",
            params={
                "select": "episode_no",
                "universe_slug": f"eq.{universe_slug}",
                "series_slug": f"eq.{series_slug}",
                "order": "episode_no.desc",
                "limit": "1",
            },
        ) or []
        episode_no = int(existing[0]["episode_no"]) + 1 if existing else 1
        bible = os.getenv(
            "SERIES_BIBLE",
            "Eine nächtliche Notrufzentrale empfängt unmögliche Anrufe aus anderen Zeiten und Wirklichkeiten. "
            "Jede Folge löst ein eigenes Rätsel teilweise und erweitert zugleich das übergeordnete Geheimnis.",
        )
        row = {
            "owner_id": self.owner_id,
            "universe_name": universe,
            "universe_slug": universe_slug,
            "series_name": series,
            "series_slug": series_slug,
            "episode_no": episode_no,
            "title": f"Automatisch entwickelte Folge {episode_no}",
            "premise": f"{bible} Entwickle den nächsten kausalen Konflikt aus dem bestehenden Kanon.",
            "status": "idea",
            "package": {},
        }
        rows = self._request(
            "POST", "episodes", payload=row, prefer="return=representation"
        ) or []
        if not rows:
            raise RuntimeError("Supabase did not return the created episode")
        return rows[0]

    def update(
        self,
        episode_id: int,
        values: dict[str, Any],
        expected: str | None = None,
    ) -> dict[str, Any]:
        params = {"id": f"eq.{episode_id}", "select": "*"}
        if expected:
            params["status"] = f"eq.{expected}"
        payload = {
            **values,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        rows = self._request(
            "PATCH", "episodes", params=params, payload=payload,
            prefer="return=representation",
        ) or []
        if not rows:
            raise RuntimeError(
                f"Concurrent or invalid transition for episode {episode_id}; expected {expected}"
            )
        return rows[0]

    def event(self, kind: str, episode_id: int | None = None, **detail: Any) -> None:
        self._request(
            "POST",
            "automation_events",
            payload={"episode_id": episode_id, "kind": kind, "detail": detail},
            prefer="return=minimal",
        )

    def metric(self, episode_id: int, values: dict[str, Any]) -> None:
        self.event("metrics", episode_id, **values)

    def has_event(self, kind: str, episode_id: int) -> bool:
        rows = self._request(
            "GET", "automation_events",
            params={
                "select": "id",
                "episode_id": f"eq.{episode_id}",
                "kind": f"eq.{kind}",
                "limit": "1",
            },
        ) or []
        return bool(rows)
