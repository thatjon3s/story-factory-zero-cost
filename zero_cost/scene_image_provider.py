from __future__ import annotations

import base64
import hashlib
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from PIL import Image


STYLE_LOCK = (
    "Premium contemporary European children's-book animation frame, painterly texture, "
    "cinematic depth, natural expressive human faces, anatomically correct hands, colorful "
    "clothing, warm family-friendly mood, rich environmental detail, no text, no logo, "
    "no watermark, landscape 16:9."
)


def visual_prompt(scene: dict[str, Any], package: dict[str, Any]) -> str:
    supplied = str(scene.get("visual_prompt", "")).strip()
    if supplied:
        return supplied
    cast = "; ".join(
        f"{item.get('name')}: {item.get('appearance', '')}; {item.get('wardrobe', '')}"
        for item in package.get("character_bible", [])
    )
    dialogue_mood = ", ".join(
        f"{beat.get('speaker')} is {beat.get('emotion', 'natural')}"
        for beat in scene.get("dialogue", [])
    )
    return (
        f"{STYLE_LOCK} Recurring cast must remain identical: {cast}. "
        f"Setting: {scene.get('location', '')}. Visible action: {scene.get('action', '')}. "
        f"Emotional performance: {dialogue_mood}. Camera: {scene.get('camera', '')}. "
        f"Lighting: {scene.get('lighting', '')}. Show one coherent instant of the action. "
        "Exactly the named recurring characters, no duplicate people."
    )


class KreaImageProvider:
    base_url = "https://api.krea.ai"

    def __init__(self) -> None:
        key = (os.getenv("KREA_API_KEY") or os.getenv("KREA_API_TOKEN") or "").strip()
        if not key:
            raise RuntimeError("KREA_API_KEY is missing")
        self.headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        self.endpoint = os.getenv("KREA_IMAGE_ENDPOINT", "/generate/image/krea/krea-2/medium")

    def generate(self, prompt: str, destination: Path) -> None:
        response = httpx.post(
            f"{self.base_url}{self.endpoint}",
            headers=self.headers,
            json={"prompt": prompt, "aspect_ratio": "16:9", "resolution": "1K"},
            timeout=90,
        )
        response.raise_for_status()
        job_id = response.json()["job_id"]
        deadline = time.monotonic() + int(os.getenv("IMAGE_JOB_TIMEOUT_SECONDS", "3600"))
        while time.monotonic() < deadline:
            status = httpx.get(
                f"{self.base_url}/jobs/{job_id}", headers=self.headers, timeout=60
            )
            status.raise_for_status()
            job = status.json()
            if job.get("status") == "completed":
                urls = (job.get("result") or {}).get("urls") or []
                if not urls:
                    raise RuntimeError(f"Krea image job {job_id} returned no URL")
                _download_image(urls[0], destination)
                return
            if job.get("status") in {"failed", "cancelled"}:
                raise RuntimeError(f"Krea image job {job_id}: {job.get('error') or job['status']}")
            time.sleep(5)
        raise TimeoutError(f"Krea image job {job_id} timed out")


class GoogleImageProvider:
    def __init__(self) -> None:
        self.key = os.getenv("GEMINI_API_KEY", "").strip()
        if not self.key:
            raise RuntimeError("GEMINI_API_KEY is missing")
        self.model = os.getenv("GOOGLE_IMAGE_MODEL", "gemini-3.1-flash-lite-image")

    def generate(self, prompt: str, destination: Path) -> None:
        response = httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            params={"key": self.key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "responseModalities": ["TEXT", "IMAGE"],
                    "imageConfig": {"aspectRatio": "16:9", "imageSize": "1K"},
                },
            },
            timeout=900,
        )
        response.raise_for_status()
        candidates = response.json().get("candidates") or []
        parts = ((candidates[0].get("content") or {}).get("parts") or []) if candidates else []
        image = next(
            (part.get("inlineData") or part.get("inline_data") for part in parts
             if part.get("inlineData") or part.get("inline_data")),
            None,
        )
        if not image or not image.get("data"):
            raise RuntimeError("Google image response contained no image")
        destination.write_bytes(base64.b64decode(image["data"]))
        _validate_image(destination)


class PollinationsImageProvider:
    """Free, keyless scene generation through Pollinations' public image API."""

    base_url = "https://gen.pollinations.ai"

    def __init__(self) -> None:
        self.model = os.getenv("POLLINATIONS_IMAGE_MODEL", "zimage").strip() or "zimage"
        self.key = os.getenv("POLLINATIONS_API_KEY", "").strip()

    def generate(self, prompt: str, destination: Path) -> None:
        seed = int(hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8], 16) % 2_147_483_647
        url = f"{self.base_url}/image/{quote(prompt, safe='')}"
        params = {
            "model": self.model,
            "width": 1280,
            "height": 720,
            "seed": seed,
            "safe": "true",
        }
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                with httpx.stream(
                    "GET",
                    url,
                    params=params,
                    headers=headers,
                    follow_redirects=True,
                    timeout=900,
                ) as response:
                    response.raise_for_status()
                    with destination.open("wb") as output:
                        for chunk in response.iter_bytes():
                            output.write(chunk)
                _validate_image(destination)
                return
            except Exception as error:
                last_error = error
                destination.unlink(missing_ok=True)
                if attempt < 3:
                    time.sleep(15 * (attempt + 1))
        raise RuntimeError(
            f"Pollinations failed after 4 attempts using model {self.model}: {last_error}"
        ) from last_error


def _download_image(url: str, destination: Path) -> None:
    with httpx.stream("GET", url, follow_redirects=True, timeout=300) as response:
        response.raise_for_status()
        with destination.open("wb") as output:
            for chunk in response.iter_bytes():
                output.write(chunk)
    _validate_image(destination)


def _validate_image(path: Path) -> None:
    if path.stat().st_size < 100_000:
        raise RuntimeError(f"Generated image is suspiciously small: {path.stat().st_size} bytes")
    with Image.open(path) as image:
        width, height = image.size
        if width < 1024 or height < 576:
            raise RuntimeError(f"Generated image resolution is too small: {width}x{height}")
        if abs(width / height - 16 / 9) > .12:
            raise RuntimeError(f"Generated image has wrong aspect ratio: {width}x{height}")


def generate_scene_images(package: dict[str, Any], workdir: Path) -> tuple[list[Path], str]:
    requested = os.getenv("SCENE_IMAGE_PROVIDER", "auto").strip().lower()
    paid_allowed = os.getenv("ALLOW_PAID_IMAGE_API", "false").lower() == "true"
    # "auto" must always remain genuinely zero-cost, even when old paid keys exist.
    provider_name = "pollinations" if requested == "auto" else requested
    if provider_name not in {"pollinations", "krea", "google"}:
        return [], "illustrated-static-fallback"
    if provider_name in {"krea", "google"} and not paid_allowed:
        return [], f"{provider_name}-blocked-by-zero-cost-guard"
    provider = (
        KreaImageProvider() if provider_name == "krea" else
        GoogleImageProvider() if provider_name == "google" else
        PollinationsImageProvider()
    )
    images: list[Path] = []
    for index, scene in enumerate(package.get("scenes") or []):
        destination = workdir / f"generated-scene-{index:02d}.png"
        provider.generate(visual_prompt(scene, package), destination)
        images.append(destination)
    return images, provider_name
