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

ENVIRONMENT_STYLE_LOCK = (
    "Premium contemporary European children's-book environment painting, painterly texture, "
    "cinematic depth, colorful warm family-friendly mood, rich environmental detail, "
    "landscape 16:9, no text, no logo, no watermark."
)


def background_prompt(scene: dict[str, Any], package: dict[str, Any]) -> str:
    props = ", ".join(str(prop) for prop in scene.get("props", [])) or "appropriate story props"
    return (
        f"{ENVIRONMENT_STYLE_LOCK} Empty establishing-shot environment plate before the actors "
        f"arrive. Setting: {scene.get('location', '')}. Arrange these objects naturally: {props}. "
        f"Camera: {scene.get('camera', '')}. "
        f"Lighting: {scene.get('lighting', '')}. Leave clear open foreground space for two "
        "separately composited animated characters. The location is completely deserted."
    )


def character_prompt(character: dict[str, Any]) -> str:
    return (
        f"{STYLE_LOCK} Isolated full-body character cutout of {character.get('name')}: "
        f"{character.get('appearance', '')}; wearing exactly {character.get('wardrobe', '')}. "
        "Standing in a relaxed three-quarter pose, both complete hands and both complete shoes visible, "
        "arms slightly away from the torso for animation, friendly natural expression, looking toward "
        "camera, single character only. Perfectly uniform pure white studio background, no floor, "
        "no scenery, no props, no cast shadow, no text, no border. Keep this exact character design."
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

    def generate(
        self, prompt: str, destination: Path, *, seed_material: str | None = None,
        width: int = 1280, height: int = 720, model: str | None = None,
        negative_prompt: str | None = None,
    ) -> None:
        seed_source = seed_material or prompt
        seed = int(hashlib.sha256(seed_source.encode("utf-8")).hexdigest()[:8], 16) % 2_147_483_647
        url = f"{self.base_url}/image/{quote(prompt, safe='')}"
        params = {
            "model": model or self.model,
            "width": width,
            "height": height,
            "seed": seed,
            "safe": "true",
        }
        if negative_prompt:
            params["negative_prompt"] = negative_prompt
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
                _validate_image(destination, expected_ratio=width / height)
                return
            except Exception as error:
                last_error = error
                destination.unlink(missing_ok=True)
                if attempt < 3:
                    time.sleep(15 * (attempt + 1))
        raise RuntimeError(
            f"Pollinations failed after 4 attempts using model {model or self.model}: {last_error}"
        ) from last_error


def _download_image(url: str, destination: Path) -> None:
    with httpx.stream("GET", url, follow_redirects=True, timeout=300) as response:
        response.raise_for_status()
        with destination.open("wb") as output:
            for chunk in response.iter_bytes():
                output.write(chunk)
    _validate_image(destination)


def _validate_image(path: Path, expected_ratio: float = 16 / 9) -> None:
    # Clean studio plates and isolated portrait assets compress much more efficiently
    # than detailed landscape scenes. Decode and resolution checks below are the
    # authoritative quality gate; this only rejects obvious error payloads.
    if path.stat().st_size < 20_000:
        raise RuntimeError(f"Generated image is suspiciously small: {path.stat().st_size} bytes")
    with Image.open(path) as image:
        width, height = image.size
        if width < 720 or height < 576:
            raise RuntimeError(f"Generated image resolution is too small: {width}x{height}")
        if abs(width / height - expected_ratio) > .12:
            raise RuntimeError(f"Generated image has wrong aspect ratio: {width}x{height}")


def _extract_white_background(source: Path, destination: Path) -> None:
    with Image.open(source) as opened:
        image = opened.convert("RGBA")
    pixels = []
    for red, green, blue, _ in image.getdata():
        low, high = min(red, green, blue), max(red, green, blue)
        whiteness = min(255, max(0, (low - 170) * 3))
        neutrality = min(255, max(0, 255 - (high - low) * 7))
        pixels.append(255 - min(whiteness, neutrality))
    alpha = Image.new("L", image.size)
    alpha.putdata(pixels)
    image.putalpha(alpha)
    bbox = alpha.getbbox()
    if not bbox:
        raise RuntimeError("Generated character contains no extractable foreground")
    margin = 24
    bbox = (
        max(0, bbox[0] - margin), max(0, bbox[1] - margin),
        min(image.width, bbox[2] + margin), min(image.height, bbox[3] + margin),
    )
    image.crop(bbox).save(destination, "PNG", optimize=True)


def generate_visual_assets(
    package: dict[str, Any], workdir: Path
) -> tuple[list[Path], dict[str, Path], str]:
    requested = os.getenv("SCENE_IMAGE_PROVIDER", "auto").strip().lower()
    paid_allowed = os.getenv("ALLOW_PAID_IMAGE_API", "false").lower() == "true"
    # "auto" must always remain genuinely zero-cost, even when old paid keys exist.
    provider_name = "pollinations" if requested == "auto" else requested
    if provider_name not in {"pollinations", "krea", "google"}:
        return [], {}, "illustrated-static-fallback"
    if provider_name in {"krea", "google"} and not paid_allowed:
        return [], {}, f"{provider_name}-blocked-by-zero-cost-guard"
    provider = (
        KreaImageProvider() if provider_name == "krea" else
        GoogleImageProvider() if provider_name == "google" else
        PollinationsImageProvider()
    )
    images: list[Path] = []
    for index, scene in enumerate(package.get("scenes") or []):
        destination = workdir / f"generated-scene-{index:02d}.png"
        prompt = background_prompt(scene, package)
        if isinstance(provider, PollinationsImageProvider):
            provider.generate(
                prompt,
                destination,
                model=os.getenv("POLLINATIONS_BACKGROUND_MODEL", "zimage"),
                negative_prompt=(
                    "people, person, child, children, boy, girl, man, woman, human, face, body, "
                    "crowd, silhouette, character, animal, portrait, mannequin"
                ),
            )
        else:
            provider.generate(prompt, destination)
        images.append(destination)
    characters: dict[str, Path] = {}
    for index, character in enumerate(package.get("character_bible") or []):
        name = str(character.get("name", f"character-{index}"))
        raw = workdir / f"generated-character-{index:02d}-raw.png"
        destination = workdir / f"generated-character-{index:02d}.png"
        prompt = character_prompt(character)
        if isinstance(provider, PollinationsImageProvider):
            identity = "|".join((
                name, str(character.get("appearance", "")), str(character.get("wardrobe", "")),
            ))
            provider.generate(
                prompt, raw, seed_material=identity, width=768, height=1024,
            )
            _extract_white_background(raw, destination)
        else:
            provider.generate(prompt, raw)
            _extract_white_background(raw, destination)
        characters[name] = destination
    return images, characters, provider_name


def generate_scene_images(package: dict[str, Any], workdir: Path) -> tuple[list[Path], str]:
    """Compatibility wrapper for older callers."""
    images, _, provider_name = generate_visual_assets(package, workdir)
    return images, provider_name
