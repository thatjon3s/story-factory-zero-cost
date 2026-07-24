from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from puppet_renderer import build_dialogue_track


def _asset_manifest(root: Path, destination: Path) -> list[str]:
    candidates = sorted(
        (
            path for path in root.rglob("*.fbx")
            if path.stat().st_size > 50_000
            and not any(word in path.name.lower() for word in ("weapon", "sword", "axe", "gun", "shield"))
        ),
        key=lambda path: path.stat().st_size,
        reverse=True,
    )
    if len(candidates) < 2:
        raise RuntimeError(f"CC0 character pack contains only {len(candidates)} usable FBX files")
    values = [str(path.resolve()) for path in candidates[:12]]
    destination.write_text(json.dumps(values), encoding="utf-8")
    return values


def _srt(cue_sets, durations, destination: Path) -> None:
    def stamp(seconds: float) -> str:
        millis = round(seconds * 1000)
        hours, rest = divmod(millis, 3_600_000)
        minutes, rest = divmod(rest, 60_000)
        secs, ms = divmod(rest, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"
    lines, offset, number = [], 0.0, 1
    for cues, duration in zip(cue_sets, durations):
        for cue in cues:
            lines += [str(number), f"{stamp(offset + cue.start)} --> {stamp(offset + cue.end)}",
                      f"{cue.speaker.upper()}: {cue.text}", ""]
            number += 1
        offset += duration
    destination.write_text("\n".join(lines), encoding="utf-8")


def render_blender_master(package: dict[str, Any], output: Path, workdir: Path) -> None:
    assets_root = Path(os.environ.get("CC0_CHARACTER_ASSETS", "cc0-character-assets"))
    manifest = workdir / "assets.json"
    _asset_manifest(assets_root, manifest)
    audio, cue_sets, durations = build_dialogue_track(package, workdir)
    clips = []
    blender = os.getenv("BLENDER_BIN", "blender")
    script = Path(__file__).with_name("blender_scene.py").resolve()
    for index, (scene, duration) in enumerate(zip(package["scenes"], durations)):
        config = workdir / f"scene-{index:02d}.json"
        config.write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")
        clip = workdir / f"scene-{index:02d}-3d.mp4"
        subprocess.run(
            [
                blender, "--background", "--python", str(script), "--",
                "--config", str(config), "--assets", str(manifest), "--scene-index", str(index),
                "--frames", str(round(duration * 24)), "--output", str(clip),
            ],
            check=True, timeout=5400,
        )
        clips.append(clip)
    concat = workdir / "3d-scenes.txt"
    concat.write_text("\n".join(f"file '{path.resolve().as_posix()}'" for path in clips), encoding="utf-8")
    silent = workdir / "3d-master-silent.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", str(silent)],
        check=True, timeout=1200,
    )
    subtitles = workdir / "dialogue.srt"
    _srt(cue_sets, durations, subtitles)
    escaped = str(subtitles.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(silent), "-i", str(audio),
            "-vf", f"scale=1920:1080:flags=lanczos,subtitles='{escaped}':"
                   "force_style='FontName=DejaVu Sans,FontSize=20,PrimaryColour=&H00FFFFFF,"
                   "OutlineColour=&H90000000,BorderStyle=3,Outline=1,MarginV=36,Alignment=2'",
            "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "slow", "-crf", "17",
            "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-shortest", str(output),
        ],
        check=True, timeout=3600,
    )
