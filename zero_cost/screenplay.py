from __future__ import annotations

import hashlib
import re
from typing import Any


ALLOWED_DURATIONS = {6, 8, 10}


def transcript(package: dict[str, Any]) -> str:
    lines: list[str] = []
    for scene in package.get("scenes", []):
        for beat in scene.get("dialogue", []):
            lines.append(f"{beat['speaker']}: {beat['text']}")
    return "\n".join(lines)


def validate_screenplay(package: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    scenes = package.get("scenes") or []
    if not 6 <= len(scenes) <= 12:
        errors.append("A canonical episode needs 6-12 scenes")
    if package.get("asset_role") != "canonical_master":
        errors.append("The production package must be a canonical_master")
    if package.get("aspect_ratio") != "16:9":
        errors.append("Canonical masters must use 16:9")

    seen_lines: list[str] = []
    previous_after: dict[str, Any] | None = None
    total_duration = 0
    speakers: set[str] = set()
    for index, scene in enumerate(scenes, 1):
        duration = int(scene.get("duration_seconds", 0))
        total_duration += duration
        if duration not in ALLOWED_DURATIONS:
            errors.append(f"Scene {index}: duration must be 6, 8 or 10 seconds")
        dialogue = scene.get("dialogue") or []
        if not dialogue:
            errors.append(f"Scene {index}: dialogue is required; narration is forbidden")
        spoken_words = 0
        for beat in dialogue:
            speaker = str(beat.get("speaker", "")).strip()
            text = str(beat.get("text", "")).strip()
            if not speaker or not text:
                errors.append(f"Scene {index}: every dialogue beat needs speaker and text")
                continue
            speakers.add(speaker)
            spoken_words += len(text.split())
            normalized = re.sub(r"\W+", " ", text.lower()).strip()
            if normalized in seen_lines:
                errors.append(f"Scene {index}: repeated dialogue: {text}")
            seen_lines.append(normalized)
        if spoken_words > duration * 2.7:
            errors.append(f"Scene {index}: dialogue is too long for {duration} seconds")
        before = scene.get("state_before") or {}
        after = scene.get("state_after") or {}
        if previous_after is not None:
            for key, value in before.items():
                if key in previous_after and previous_after[key] != value:
                    errors.append(
                        f"Scene {index}: state '{key}' contradicts previous scene "
                        f"({previous_after[key]!r} -> {value!r})"
                    )
        previous_after = after
    if total_duration < 48 or total_duration > 120:
        errors.append(f"Master duration must be 48-120 seconds, got {total_duration}")
    if len(speakers) < 2:
        errors.append("At least two speaking characters are required")

    delta = package.get("memory_delta") or {}
    if not str(delta.get("episode_summary", "")).strip():
        errors.append("memory_delta.episode_summary is required")
    if not isinstance(delta.get("canon_entries"), list):
        errors.append("memory_delta.canon_entries must be a list")
    return errors


def finalize_package(package: dict[str, Any]) -> dict[str, Any]:
    package = {
        **package,
        "asset_role": "canonical_master",
        "aspect_ratio": "16:9",
        "resolution": "1920x1080",
        "has_narrator": False,
    }
    package["script"] = transcript(package)
    errors = validate_screenplay(package)
    if errors:
        raise ValueError("Invalid screenplay: " + "; ".join(errors))
    digest = hashlib.sha256(
        (package["script"] + str(package["memory_delta"])).encode("utf-8")
    ).hexdigest()[:16]
    return {**package, "revision": digest}
