from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx


UNIVERSE_SERIES_KEY = "__universe__"


@dataclass(frozen=True)
class CanonContext:
    universe: list[dict[str, Any]]
    series: list[dict[str, Any]]
    recent_episodes: list[dict[str, Any]]

    def prompt_block(self) -> str:
        def lines(rows: list[dict[str, Any]], field: str = "summary") -> str:
            return "\n".join(f"- {row.get(field, '')}" for row in rows) or "- Noch keine kanonischen Einträge."

        return (
            "VERBINDLICHER UNIVERSUMS-KANON (darf nicht verletzt werden):\n"
            f"{lines(self.universe)}\n\n"
            "STARK GEWICHTETER SERIEN-KANON (hat innerhalb der Serie Vorrang):\n"
            f"{lines(self.series)}\n\n"
            "LETZTE FREIGEGEBENE FOLGEN (unmittelbare Kontinuität):\n"
            f"{lines(self.recent_episodes, 'episode_summary')}"
        )


class SupabaseMemory:
    """Server-only canon store. The service-role key must only live in GitHub Secrets."""

    def __init__(self) -> None:
        self.base_url = os.getenv("SUPABASE_URL", "").rstrip("/")
        self.key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        self.enabled = bool(self.base_url and self.key)
        self.headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, table: str, *, params: dict[str, str] | None = None,
                 payload: Any = None, prefer: str = "") -> Any:
        if not self.enabled:
            raise RuntimeError(
                "Canonical memory is not configured. Set SUPABASE_URL and "
                "SUPABASE_SERVICE_ROLE_KEY as GitHub Actions secrets."
            )
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

    def context(self, universe_slug: str, series_slug: str) -> CanonContext:
        if not self.enabled:
            return CanonContext([], [], [])
        base = {
            "select": "scope,canonical_key,summary,importance,source_episode_revision",
            "universe_slug": f"eq.{universe_slug}",
            "status": "eq.canonical",
            "order": "importance.desc,updated_at.desc",
        }
        universe = self._request(
            "GET", "canon_entries",
            params={**base, "series_slug": f"eq.{UNIVERSE_SERIES_KEY}", "limit": "60"},
        )
        series = self._request(
            "GET", "canon_entries",
            params={**base, "series_slug": f"eq.{series_slug}", "limit": "120"},
        )
        recent = self._request(
            "GET", "episode_memories",
            params={
                "select": "episode_no,title,episode_summary,memory_delta,episode_revision",
                "universe_slug": f"eq.{universe_slug}",
                "series_slug": f"eq.{series_slug}",
                "status": "eq.canonical",
                "order": "episode_no.desc",
                "limit": "5",
            },
        )
        return CanonContext(universe or [], series or [], list(reversed(recent or [])))

    def commit_approved_episode(self, episode: dict[str, Any]) -> None:
        package = episode.get("package") or {}
        if package.get("asset_role", "canonical_master") != "canonical_master":
            return
        revision = package["revision"]
        memory_delta = package.get("memory_delta") or {}
        summary = memory_delta.get("episode_summary", "").strip()
        if not summary:
            raise RuntimeError("Approved episode has no canonical episode_summary")
        universe_slug = package["universe_slug"]
        series_slug = package["series_slug"]
        episode_row = {
            "episode_revision": revision,
            "universe_slug": universe_slug,
            "series_slug": series_slug,
            "episode_no": int(episode["episode_no"]),
            "title": package["title"],
            "episode_summary": summary,
            "memory_delta": memory_delta,
            "status": "canonical",
        }
        self._request(
            "POST", "episode_memories",
            params={"on_conflict": "episode_revision"},
            payload=episode_row,
            prefer="resolution=merge-duplicates,return=minimal",
        )
        entries = []
        for item in memory_delta.get("canon_entries", []):
            scope = item.get("scope")
            if scope not in {"universe", "series"}:
                raise RuntimeError(f"Invalid canon scope: {scope!r}")
            entries.append({
                "universe_slug": universe_slug,
                "series_slug": UNIVERSE_SERIES_KEY if scope == "universe" else series_slug,
                "scope": scope,
                "canonical_key": item["key"],
                "summary": item["summary"],
                "importance": int(item.get("importance", 50)),
                "status": "canonical",
                "source_episode_revision": revision,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
        if entries:
            revisions = [
                {
                    **entry,
                    "episode_revision": revision,
                }
                for entry in entries
            ]
            self._request(
                "POST", "canon_entry_revisions",
                params={"on_conflict": "episode_revision,universe_slug,series_slug,canonical_key"},
                payload=revisions,
                prefer="resolution=ignore-duplicates,return=minimal",
            )
            self._request(
                "POST", "canon_entries",
                params={"on_conflict": "universe_slug,series_slug,canonical_key"},
                payload=entries,
                prefer="resolution=merge-duplicates,return=minimal",
            )


def slugify(value: str) -> str:
    normalized = value.lower().strip()
    normalized = (
        normalized.replace("ä", "ae").replace("ö", "oe")
        .replace("ü", "ue").replace("ß", "ss")
    )
    return "-".join(part for part in "".join(
        char if char.isalnum() else " " for char in normalized
    ).split())


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
