from __future__ import annotations

import os
import socket
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx


@dataclass(frozen=True)
class StudioLease:
    job: dict[str, Any]
    provider: dict[str, Any]


class SceneAdapter(Protocol):
    def generate(self, scene: dict[str, Any], package: dict[str, Any], destination: Path) -> None:
        ...


class SupabaseStudioQueue:
    def __init__(self) -> None:
        self.base_url = os.getenv("SUPABASE_URL", "").rstrip("/")
        self.key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        if not self.base_url or not self.key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")
        self.headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
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
            f"{self.base_url}/rest/v1/{path}",
            headers=headers,
            params=params,
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        return response.json() if response.content else None

    def enqueue_package(self, package: dict[str, Any]) -> None:
        rows = [
            {
                "episode_revision": package["revision"],
                "universe_slug": package["universe_slug"],
                "series_slug": package["series_slug"],
                "scene_index": index,
                "payload": {
                    "scene": scene,
                    "character_bible": package["character_bible"],
                    "aspect_ratio": "16:9",
                    "resolution": "1920x1080",
                    "asset_role": "canonical_master",
                },
                "status": "queued",
                "priority": 100 - index,
            }
            for index, scene in enumerate(package["scenes"])
        ]
        self._request(
            "POST",
            "scene_jobs",
            params={"on_conflict": "episode_revision,scene_index"},
            payload=rows,
            prefer="resolution=ignore-duplicates,return=minimal",
        )

    def claim(self, episode_revision: str, worker_name: str | None = None) -> StudioLease | None:
        response = self._request(
            "POST",
            "rpc/claim_scene_job",
            payload={
                "worker_name": worker_name or f"{socket.gethostname()}-{os.getpid()}",
                "target_episode_revision": episode_revision,
                "lease_seconds": int(os.getenv("SCENE_LEASE_SECONDS", "7200")),
            },
        )
        if not response:
            return None
        return StudioLease(job=response["job"], provider=response["provider"])

    def mark_rendering(self, lease: StudioLease) -> None:
        self._update(lease, {"status": "rendering"})

    def complete(self, lease: StudioLease, output_url: str, quality_report: dict[str, Any]) -> None:
        self._update(lease, {
            "status": "completed",
            "output_url": output_url,
            "quality_report": quality_report,
            "lease_until": None,
            "error_log": None,
        })

    def retry(self, lease: StudioLease, error: str) -> None:
        self._update(lease, {
            "status": "retry",
            "error_log": error[-8000:],
            "lease_until": None,
        })

    def _update(self, lease: StudioLease, values: dict[str, Any]) -> None:
        result = self._request(
            "PATCH",
            "scene_jobs",
            params={
                "id": f"eq.{lease.job['id']}",
                "leased_by": f"eq.{lease.job['leased_by']}",
                "select": "id",
            },
            payload=values,
            prefer="return=representation",
        )
        if not result:
            raise RuntimeError("Scene lease was lost or updated by another worker")

    def completed_urls(self, revision: str, expected: int) -> list[str]:
        rows = self._request(
            "GET",
            "scene_jobs",
            params={
                "select": "scene_index,output_url,status",
                "episode_revision": f"eq.{revision}",
                "order": "scene_index",
            },
        ) or []
        if len(rows) != expected or any(row["status"] != "completed" for row in rows):
            return []
        return [row["output_url"] for row in rows]

    def upload_scene(self, revision: str, scene_index: int, source: Path) -> str:
        object_path = f"{revision}/scene-{scene_index:02d}.mp4"
        with source.open("rb") as handle:
            response = httpx.post(
                f"{self.base_url}/storage/v1/object/story-scenes/{object_path}",
                headers={
                    "apikey": self.key,
                    "Authorization": f"Bearer {self.key}",
                    "Content-Type": "video/mp4",
                    "x-upsert": "true",
                },
                content=handle,
                timeout=600,
            )
        response.raise_for_status()
        return object_path

    def download_scene(self, object_path: str, destination: Path) -> None:
        with httpx.stream(
            "GET",
            f"{self.base_url}/storage/v1/object/authenticated/story-scenes/{object_path}",
            headers={"apikey": self.key, "Authorization": f"Bearer {self.key}"},
            timeout=600,
        ) as response:
            response.raise_for_status()
            with destination.open("wb") as output:
                for chunk in response.iter_bytes():
                    output.write(chunk)


class StudioRouter:
    def __init__(self, adapters: dict[str, SceneAdapter], queue: SupabaseStudioQueue | None = None) -> None:
        self.adapters = adapters
        self.queue = queue or SupabaseStudioQueue()

    def work_once(self, package: dict[str, Any], workdir: Path) -> tuple[StudioLease, Path] | None:
        lease = self.queue.claim(package["revision"])
        if lease is None:
            return None
        adapter_key = lease.provider["adapter_key"]
        adapter = self.adapters.get(adapter_key)
        if adapter is None:
            self.queue.retry(lease, f"No installed adapter for {adapter_key}")
            raise RuntimeError(f"No installed studio adapter for {adapter_key}")
        destination = workdir / f"scene-{int(lease.job['scene_index']):02d}.mp4"
        self.queue.mark_rendering(lease)
        try:
            adapter.generate(lease.job["payload"]["scene"], package, destination)
            quality = inspect_scene(destination)
            if not quality["passed"]:
                raise RuntimeError("Scene quality gate failed: " + "; ".join(quality["errors"]))
            object_path = self.queue.upload_scene(
                package["revision"], int(lease.job["scene_index"]), destination
            )
            self.queue.complete(lease, object_path, quality)
        except Exception as exc:
            self.queue.retry(lease, repr(exc))
            raise
        return lease, destination

    def collect_package(self, package: dict[str, Any], workdir: Path) -> list[Path]:
        paths = self.queue.completed_urls(package["revision"], len(package["scenes"]))
        if not paths:
            raise RuntimeError("Not all scene jobs are completed")
        clips: list[Path] = []
        for index, object_path in enumerate(paths):
            destination = workdir / f"scene-{index:02d}.mp4"
            if not destination.exists():
                self.queue.download_scene(object_path, destination)
            clips.append(destination)
        return clips


def inspect_scene(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries",
            "format=duration,size:stream=codec_type,width,height",
            "-of", "json", str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    data = json.loads(result.stdout)
    duration = float(data.get("format", {}).get("duration", 0))
    size = int(data.get("format", {}).get("size", 0))
    streams = data.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    has_audio = any(item.get("codec_type") == "audio" for item in streams)
    errors = []
    if duration < 5:
        errors.append(f"duration {duration:.2f}s")
    if size < 100_000:
        errors.append(f"file size {size}")
    if int(video.get("width", 0)) < 1280 or int(video.get("height", 0)) < 720:
        errors.append(f"resolution {video.get('width')}x{video.get('height')}")
    if not has_audio:
        errors.append("missing audio")
    return {
        "passed": not errors,
        "errors": errors,
        "duration_seconds": duration,
        "bytes": size,
        "width": video.get("width"),
        "height": video.get("height"),
        "has_audio": has_audio,
    }
