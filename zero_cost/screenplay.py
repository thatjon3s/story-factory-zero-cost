from __future__ import annotations

import hashlib
import copy
import re
from typing import Any


ALLOWED_DURATIONS = {6, 8, 10}
FAMILY_SETTINGS = ("spielplatz", "park", "schule", "garten", "see", "ufer", "ausflug", "wiese")
FORBIDDEN_FAMILY_WORDS = (
    "waffe", "pistole", "messer", "blut", "tot", "töten", "leiche",
    "entführung", "horror", "dämon", "mord", "erpressung",
)
ILLUSTRATED_PROPS = ("schaufel", "ball", "papierboot", "picknickkorb")
FAMILY_BEATS = (
    "wunsch", "hindernis", "reaktion", "versuch",
    "rueckschlag", "verstehen", "loesung", "schlussgag",
)
VISIBLE_ACTION_WORDS = (
    "kommt", "geht", "läuft", "hält", "zeigt", "nimmt", "gibt", "reicht",
    "legt", "stellt", "setzt", "spielt", "baut", "versteckt", "findet",
    "öffnet", "dreht", "rollt", "fährt", "winkt", "lacht", "schaut",
)


def transcript(package: dict[str, Any]) -> str:
    lines: list[str] = []
    for scene in package.get("scenes", []):
        for beat in scene.get("dialogue", []):
            lines.append(f"{beat['speaker']}: {beat['text']}")
    return "\n".join(lines)


def repair_screenplay(package: dict[str, Any]) -> dict[str, Any]:
    """Deterministically repair common small-model mistakes before strict validation."""
    package = copy.deepcopy(package)
    if package.get("story_profile") not in {"social-kindness-v2", "social-kindness-v3"}:
        return package
    scenes = list(package.get("scenes") or [])[:8]
    if len(scenes) != 8:
        return package
    safe_actions = (
        "Mia hält den gelb-blauen Ball hoch und zeigt auf den Spielplatz.",
        "Noah nimmt den Ball, dreht ihn in den Händen und schaut Mia an.",
        "Mia geht einen Schritt zurück und zeigt auf Noah.",
        "Noah rollt den Ball langsam zu Mia.",
        "Mia stoppt den Ball mit dem Fuß und legt ihn neben sich.",
        "Noah setzt sich zu Mia und reicht ihr den Ball.",
        "Mia nimmt den Ball und wirft ihn vorsichtig zu Noah.",
        "Noah fängt den Ball, lacht und beide winken.",
    )
    safe_dialogue = (
        ("Mia", "Komm, wir spielen zusammen!"), ("Noah", "Ich würde gern, aber ich bin noch unsicher."),
        ("Mia", "Dann fangen wir ganz langsam an."), ("Noah", "Roll ihn bitte erst zu mir."),
        ("Mia", "Oh, der Ball ist weggerollt!"), ("Noah", "Zusammen bekommen wir das hin."),
        ("Mia", "Jetzt klappt es — du bist dran!"), ("Noah", "Und morgen üben wir den Rekordwurf!"),
    )
    seen: set[str] = set()
    seen_actions: list[set[str]] = []
    previous_after: dict[str, Any] | None = None
    for index, scene in enumerate(scenes):
        scene["beat"] = FAMILY_BEATS[index]
        scene["duration_seconds"] = 8
        action = str(scene.get("action", ""))
        action_words = set(re.sub(r"\W+", " ", action.lower()).split())
        too_similar = any(
            action_words | prior and len(action_words & prior) / len(action_words | prior) > .78
            for prior in seen_actions
        )
        if not any(word in action.lower() for word in VISIBLE_ACTION_WORDS) or too_similar:
            scene["action"] = safe_actions[index]
            action_words = set(re.sub(r"\W+", " ", scene["action"].lower()).split())
        seen_actions.append(action_words)
        props = []
        for value in scene.get("props") or []:
            lowered = str(value).lower()
            canonical = next((name for key, name in (
                ("schaufel", "rote Spielzeugschaufel"), ("spaten", "rote Spielzeugschaufel"),
                ("ball", "gelb-blauer Ball"), ("boot", "Papierboot"),
                ("schiff", "Papierboot"), ("korb", "Picknickkorb"),
            ) if key in lowered), None)
            if canonical and canonical not in props:
                props.append(canonical)
        action_lower = scene["action"].lower()
        if "ball" in action_lower and "gelb-blauer Ball" not in props:
            props.append("gelb-blauer Ball")
        if "schaufel" in action_lower and "rote Spielzeugschaufel" not in props:
            props.append("rote Spielzeugschaufel")
        if "papierboot" in action_lower and "Papierboot" not in props:
            props.append("Papierboot")
        if "picknickkorb" in action_lower and "Picknickkorb" not in props:
            props.append("Picknickkorb")
        scene["props"] = props
        dialogue = scene.get("dialogue") or []
        cleaned = []
        for beat in dialogue[:2]:
            text = str(beat.get("text", "")).strip()
            normalized = re.sub(r"\W+", " ", text.lower()).strip()
            if not text or normalized in seen:
                continue
            seen.add(normalized)
            cleaned.append({**beat, "speaker": "Mia" if str(beat.get("speaker", "")).lower().startswith("mia") else "Noah", "text": text})
        if not cleaned:
            speaker, text = safe_dialogue[index]
            cleaned = [{"speaker": speaker, "text": text, "emotion": "lebhaft und natürlich"}]
            seen.add(re.sub(r"\W+", " ", text.lower()).strip())
        scene["dialogue"] = cleaned
        if previous_after is not None:
            scene["state_before"] = copy.deepcopy(previous_after)
        scene.setdefault("state_before", {})
        scene.setdefault("state_after", copy.deepcopy(scene["state_before"]))
        previous_after = scene["state_after"]
        if len(str(scene.get("visual_prompt", "")).split()) < 25:
            scene["visual_prompt"] = (
                "Premium contemporary colorful European children's-book illustration, cinematic depth, "
                f"Mia and Noah remain visually consistent, {scene.get('location', 'playground')}, "
                f"visible action: {scene['action']}, warm natural light, expressive faces and hands, "
                "family friendly, coherent perspective, landscape composition, no text or watermark."
            )
    present_speakers = {
        str(beat.get("speaker", "")) for scene in scenes for beat in scene.get("dialogue") or []
    }
    if present_speakers != {"Mia", "Noah"}:
        for index in (0, 1):
            speaker, text = safe_dialogue[index]
            scenes[index]["dialogue"] = [{
                "speaker": speaker, "text": text, "emotion": "lebhaft und natürlich"
            }]
    package["scenes"] = scenes
    if package.get("story_profile") == "social-kindness-v3":
        bible = list(package.get("character_bible") or [])[:2]
        while len(bible) < 2:
            bible.append({})
        bible[0]["name"], bible[1]["name"] = "Mia", "Noah"
        package["character_bible"] = bible
    return package


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
    family_setting_scenes = 0
    family_beats: list[str] = []
    prop_history: set[str] = set()
    for index, scene in enumerate(scenes, 1):
        duration = int(scene.get("duration_seconds", 0))
        total_duration += duration
        if duration not in ALLOWED_DURATIONS:
            errors.append(f"Scene {index}: duration must be 6, 8 or 10 seconds")
        dialogue = scene.get("dialogue") or []
        location = str(scene.get("location", "")).lower()
        if any(value in location for value in FAMILY_SETTINGS):
            family_setting_scenes += 1
        if package.get("story_profile") in {"social-kindness-v2", "social-kindness-v3"}:
            family_beats.append(str(scene.get("beat", "")).strip().lower())
            props = {str(value).strip().lower() for value in (scene.get("props") or []) if str(value).strip()}
            action_text = str(scene.get("action", "")).lower()
            if not any(word in action_text for word in VISIBLE_ACTION_WORDS):
                errors.append(f"Scene {index}: action is not visibly renderable")
            for prop in prop_history:
                if prop in action_text and prop not in props:
                    errors.append(f"Scene {index}: referenced prop '{prop}' is missing from props")
            prop_history |= props
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
    if package.get("story_profile") in {"social-kindness-v1", "social-kindness-v2", "social-kindness-v3"}:
        if family_setting_scenes < len(scenes) // 2:
            errors.append("At least half the scenes need a child/family-friendly everyday setting")
        family_text = (
            transcript(package) + " "
            + " ".join(str(scene.get("action", "")) for scene in scenes)
        ).lower()
        forbidden = sorted(word for word in FORBIDDEN_FAMILY_WORDS if word in family_text)
        if forbidden:
            errors.append("Family profile forbids: " + ", ".join(forbidden))
    if package.get("story_profile") in {"social-kindness-v2", "social-kindness-v3"}:
        if tuple(family_beats) != FAMILY_BEATS:
            errors.append(
                "Family story beats must be exactly: " + ", ".join(FAMILY_BEATS)
            )
        actions = [
            re.sub(r"\W+", " ", str(scene.get("action", "")).lower()).strip()
            for scene in scenes
        ]
        for index, action in enumerate(actions):
            action_words = set(action.split())
            for prior in actions[:index]:
                prior_words = set(prior.split())
                union = action_words | prior_words
                if union and len(action_words & prior_words) / len(union) > .78:
                    errors.append(f"Scene {index + 1}: action is too similar to an earlier scene")
                    break
    if package.get("story_profile") == "social-kindness-v3":
        if speakers != {"Mia", "Noah"}:
            errors.append("Illustrated v3 episodes must use exactly the fixed cast Mia and Noah")
        for index, scene in enumerate(scenes, 1):
            prompt = str(scene.get("visual_prompt", "")).strip()
            if len(prompt.split()) < 25:
                errors.append(f"Scene {index}: visual_prompt is too short for consistent generation")
            for prop in scene.get("props") or []:
                normalized = str(prop).lower().replace(" ", "-")
                if not any(allowed in normalized for allowed in ILLUSTRATED_PROPS):
                    errors.append(f"Scene {index}: unsupported illustrated prop: {prop}")

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
    package = repair_screenplay(package)
    package["script"] = transcript(package)
    errors = validate_screenplay(package)
    if errors:
        raise ValueError("Invalid screenplay: " + "; ".join(errors))
    digest = hashlib.sha256(
        (package["script"] + str(package["memory_delta"])).encode("utf-8")
    ).hexdigest()[:16]
    return {**package, "revision": digest}
