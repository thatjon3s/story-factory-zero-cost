from __future__ import annotations

import os
import json
import time
from pathlib import Path
from typing import Any

import httpx


class KreaClient:
    """Krea async-job client for consistent human-action story scenes."""

    def __init__(self) -> None:
        token = os.getenv("KREA_API_TOKEN", "").strip()
        if not token:
            raise RuntimeError("KREA_API_TOKEN is missing")
        self.headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        self.base_url = "https://api.krea.ai"
        self.image_endpoint = os.getenv("KREA_IMAGE_ENDPOINT", "/generate/image/google/nano-banana")
        self.video_endpoint = os.getenv("KREA_VIDEO_ENDPOINT", "/generate/video/xai/grok-video-1.5")

    def _job(self, endpoint: str, payload: dict[str, Any], timeout: int = 1200) -> str:
        response = httpx.post(f"{self.base_url}{endpoint}", headers=self.headers, json=payload, timeout=60)
        response.raise_for_status()
        job_id = response.json()["job_id"]
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = httpx.get(f"{self.base_url}/jobs/{job_id}", headers=self.headers, timeout=30)
            response.raise_for_status()
            job = response.json()
            if job.get("status") == "completed":
                urls = (job.get("result") or {}).get("urls") or []
                if not urls:
                    raise RuntimeError(f"Krea job {job_id} completed without an asset URL")
                return urls[0]
            if job.get("status") in {"failed", "cancelled"}:
                raise RuntimeError(f"Krea job {job_id} {job['status']}: {job.get('error')}")
            time.sleep(3)
        raise TimeoutError(f"Krea job {job_id} timed out")

    def character_reference(self, character_bible: str) -> str:
        prompt = (
            "Landscape 16:9 cinematic casting photograph showing all recurring adult fictional characters. "
            "Photorealistic natural skin, realistic anatomy, contemporary wardrobe, neutral full-body poses, "
            "each person visually distinct, no text, no logos. Character continuity sheet: " + character_bible
        )
        return self._job(self.image_endpoint, {"prompt": prompt, "aspect_ratio": "16:9"})

    def scene_clip(self, scene: dict[str, str], character_bible: str, reference_url: str) -> str:
        still_url = self._job(self.image_endpoint, {
            "prompt": (
                "Landscape 16:9 cinematic frame from an original dramatic series. The people must exactly match "
                f"the reference image and this continuity description: {character_bible}. "
                f"Visible human action: {scene['image_prompt']}. Photorealistic, natural facial expression, "
                "correct hands, cinematic lighting, no text, no logos."
            ),
            "aspect_ratio": "16:9",
            "image_urls": [reference_url],
        })
        return self._job(self.video_endpoint, {
            "prompt": self._motion_prompt(scene),
            "start_image": still_url,
            "duration": 6,
            "resolution": os.getenv("KREA_VIDEO_RESOLUTION", "720p"),
            "aspect_ratio": "16:9",
        })

    @staticmethod
    def _motion_prompt(scene: dict[str, Any]) -> str:
        dialogue = " ".join(
            f'{beat["speaker"]} says in German with {beat.get("emotion", "natural")} emotion: '
            f'"{beat["text"]}"'
            for beat in scene.get("dialogue", [])
        )
        return (
            f"The same human characters perform this visible action naturally: {scene.get('action', '')}. "
            f"Exact dialogue with accurate lip sync: {dialogue}. "
            "No narrator, no voice-over and no additional speech. Preserve faces, voices, clothing and location. "
            "Realistic body motion, cinematic camera, no morphing, no text."
        )

    @staticmethod
    def download(url: str, destination: Path) -> None:
        with httpx.stream("GET", url, timeout=180, follow_redirects=True) as response:
            response.raise_for_status()
            with destination.open("wb") as output:
                for chunk in response.iter_bytes():
                    output.write(chunk)


def create_krea_clips(package: dict[str, Any], workdir: Path) -> list[Path]:
    scenes = package.get("scenes") or []
    if len(scenes) != 8:
        raise RuntimeError("The story package must contain exactly eight human-action scenes")
    client = KreaClient()
    character_bible = package["character_bible"]
    reference_url = client.character_reference(character_bible)
    clips: list[Path] = []
    for index, scene in enumerate(scenes):
        url = client.scene_clip(scene, character_bible, reference_url)
        clip = workdir / f"scene-{index:02d}.mp4"
        client.download(url, clip)
        clips.append(clip)
    return clips


class KreaSceneAdapter:
    """Official Krea API adapter; only enable it when the account terms and included quota permit automation."""

    def __init__(self) -> None:
        self.client = KreaClient()
        self.references: dict[str, str] = {}

    def generate(self, scene: dict[str, Any], package: dict[str, Any], destination: Path) -> None:
        revision = package["revision"]
        character_bible = json.dumps(package["character_bible"], ensure_ascii=False)
        reference = self.references.get(revision)
        if not reference:
            reference = self.client.character_reference(character_bible)
            self.references[revision] = reference
        normalized = {
            **scene,
            "image_prompt": (
                f"{scene['location']}. {scene['action']}. "
                f"Camera: {scene['camera']}. Lighting: {scene['lighting']}."
            ),
        }
        url = self.client.scene_clip(normalized, character_bible, reference)
        self.client.download(url, destination)
