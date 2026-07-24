from __future__ import annotations

import hashlib
import math
import os
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont


WIDTH, HEIGHT = 1280, 720
FPS = int(os.getenv("PUPPET_FPS", "24"))


@dataclass(frozen=True)
class DialogueCue:
    speaker: str
    text: str
    emotion: str
    start: float
    end: float


def _seed(value: str) -> int:
    return int(hashlib.sha256(value.encode()).hexdigest()[:12], 16)


def _font(size: int, bold: bool = False):
    paths = [
        f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'-Bold' if bold else ''}.ttf",
        f"C:/Windows/Fonts/arial{'bd' if bold else ''}.ttf",
    ]
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default()


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as stream:
        return stream.getnframes() / stream.getframerate()


def _voice_profile(character: dict[str, Any], index: int, emotion: str) -> tuple[float, float]:
    words = f"{character.get('voice', '')} {emotion}".lower()
    pitch = 0.89 if any(x in words for x in ("tief", "bass", "bariton")) else 1.03
    pitch *= (0.96, 1.05, 1.0, 1.09)[index % 4]
    speed = 0.91 if any(x in words for x in ("ruhig", "leise", "traurig")) else 1.0
    if any(x in words for x in ("panik", "wütend", "alarm", "aufgeregt")):
        speed = 1.1
    return pitch, speed


def _speak(text: str, output: Path, character: dict[str, Any], index: int, emotion: str) -> None:
    raw = output.with_suffix(".raw.wav")
    data_dir = os.getenv("PIPER_DATA_DIR", ".")
    subprocess.run(
        [
            "piper", "--data-dir", data_dir,
            "--model", os.getenv("PIPER_MODEL_PATH", "de_DE-thorsten-medium"),
            "--output_file", str(raw), "--sentence_silence", "0.12",
        ],
        input=text, text=True, encoding="utf-8", check=True, timeout=300,
    )
    pitch, speed = _voice_profile(character, index, emotion)
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(raw), "-af",
            f"asetrate=22050*{pitch},aresample=48000,atempo={speed / pitch},"
            "highpass=f=70,lowpass=f=10500,acompressor=threshold=-18dB:ratio=2.4",
            "-c:a", "pcm_s16le", str(output),
        ],
        check=True, capture_output=True, timeout=300,
    )


def build_dialogue_track(package: dict[str, Any], workdir: Path):
    characters = package.get("character_bible") or []
    cast = {str(c["name"]): (i, c) for i, c in enumerate(characters)}
    scene_audio, cue_sets, scene_durations = [], [], []
    for scene_index, scene in enumerate(package["scenes"]):
        target = float(scene["duration_seconds"])
        beats = scene["dialogue"]
        lines, lengths = [], []
        for beat_index, beat in enumerate(beats):
            speaker = str(beat["speaker"])
            char_index, character = cast.get(speaker, (len(cast), {"voice": "neutral"}))
            line = workdir / f"s{scene_index:02d}-l{beat_index:02d}.wav"
            _speak(str(beat["text"]), line, character, char_index, str(beat.get("emotion", "")))
            lines.append(line)
            lengths.append(_wav_duration(line))
        gap = 0.2
        tempo = max(1.0, sum(lengths) / max(1.0, target - gap * (len(lines) + 1)))
        fitted, fitted_lengths = [], []
        for index, line in enumerate(lines):
            fit = workdir / f"s{scene_index:02d}-f{index:02d}.wav"
            subprocess.run(
                ["ffmpeg", "-y", "-i", str(line), "-af", f"atempo={min(2.0, tempo)}",
                 "-c:a", "pcm_s16le", str(fit)],
                check=True, capture_output=True, timeout=300,
            )
            fitted.append(fit)
            fitted_lengths.append(_wav_duration(fit))
        inputs, filters, cues, cursor = [], [], [], gap
        for index, (beat, line, length) in enumerate(zip(beats, fitted, fitted_lengths)):
            inputs += ["-i", str(line)]
            delay = int(cursor * 1000)
            filters.append(f"[{index}:a]adelay={delay}|{delay}[a{index}]")
            cues.append(DialogueCue(
                str(beat["speaker"]), str(beat["text"]), str(beat.get("emotion", "")),
                cursor, min(target, cursor + length),
            ))
            cursor += length + gap
        mixed = workdir / f"scene-{scene_index:02d}.wav"
        filters.append(
            "".join(f"[a{i}]" for i in range(len(fitted)))
            + f"amix=inputs={len(fitted)}:duration=longest:normalize=0,"
              f"apad=pad_dur={target},atrim=0:{target},loudnorm=I=-18:LRA=7:TP=-2[a]"
        )
        subprocess.run(
            ["ffmpeg", "-y", *inputs, "-filter_complex", ";".join(filters),
             "-map", "[a]", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(mixed)],
            check=True, capture_output=True, timeout=600,
        )
        scene_audio.append(mixed)
        cue_sets.append(cues)
        scene_durations.append(target)
    manifest = workdir / "dialogue.txt"
    manifest.write_text("\n".join(f"file '{p.resolve().as_posix()}'" for p in scene_audio), encoding="utf-8")
    master = workdir / "dialogue-master.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(manifest),
         "-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(master)],
        check=True, capture_output=True, timeout=600,
    )
    return master, cue_sets, scene_durations


def _palette(name: str):
    value = _seed(name)
    return {
        "skin": ((value >> 1) % 45 + 168, (value >> 7) % 36 + 126, (value >> 13) % 30 + 96),
        "hair": ((value >> 4) % 48 + 16, (value >> 9) % 42 + 12, (value >> 15) % 36 + 10),
        "clothes": ((value >> 3) % 120 + 55, (value >> 10) % 120 + 55, (value >> 17) % 120 + 55),
        "accent": ((value >> 6) % 90 + 150, (value >> 12) % 90 + 140, (value >> 18) % 90 + 145),
    }


def _background(scene: dict[str, Any], index: int) -> Image.Image:
    location = str(scene.get("location", "")).lower()
    image = Image.new("RGB", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(image)
    top = (16, 28, 54) if index % 2 == 0 else (37, 20, 43)
    for y in range(HEIGHT):
        factor = y / HEIGHT
        draw.line((0, y, WIDTH, y), fill=tuple(int(top[c] * (1 - factor) + (5, 8, 16)[c] * factor) for c in range(3)))
    draw.rectangle((0, 520, WIDTH, HEIGHT), fill=(13, 17, 25))
    if any(x in location for x in ("zentrale", "büro", "office", "leitstelle")):
        for x in (120, 455, 790):
            draw.rounded_rectangle((x, 130, x + 260, 330), radius=12, fill=(5, 11, 18), outline=(50, 105, 140), width=4)
            draw.line((x + 25, 220, x + 220, 180), fill=(58, 224, 206), width=5)
        draw.rectangle((70, 520, 1210, 570), fill=(34, 41, 53))
    elif any(x in location for x in ("gang", "flur", "tür", "keller")):
        draw.polygon(((0, 0), (WIDTH, 0), (970, 520), (300, 520)), fill=(20, 25, 39))
        draw.rectangle((485, 120, 795, 570), fill=(25, 31, 43), outline=(97, 112, 133), width=8)
        draw.ellipse((735, 335, 752, 352), fill=(235, 188, 84))
    else:
        for x in range(0, WIDTH, 160):
            draw.rectangle((x + 20, 170 + (x // 8) % 90, x + 125, 520), fill=(17, 24, 38), outline=(42, 54, 74))
    glow = Image.new("RGBA", (WIDTH, HEIGHT))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((220, 30, 1060, 710), fill=(54, 175, 230, 38))
    return Image.alpha_composite(image.convert("RGBA"), glow.filter(ImageFilter.GaussianBlur(55)))


def _draw_character(draw, character, index, x, ground, scale, t, speaking, emotion, action):
    colors = _palette(str(character.get("name", index)))
    phase = (_seed(str(character.get("name", index))) % 100) / 17
    bob = math.sin(t * (5.2 if speaking else 2.1) + phase) * (5 if speaking else 2)
    if any(w in action.lower() for w in ("renn", "lauf", "stürm", "flieh")):
        bob += abs(math.sin(t * 9 + phase)) * 10
    cx, gy = int(x), int(ground + bob)
    head_r, body_h = int(64 * scale), int(255 * scale)
    shoulder, head_y = gy - body_h + int(85 * scale), gy - body_h
    outline = (7, 10, 18)
    draw.rounded_rectangle((cx - 82 * scale, shoulder, cx + 82 * scale, gy), radius=43 * scale,
                           fill=colors["clothes"], outline=outline, width=max(2, int(6 * scale)))
    swing = math.sin(t * 5 + phase) * 18 if speaking else math.sin(t * 1.5 + phase) * 5
    if any(w in action.lower() for w in ("zeig", "deut", "greif", "telefon", "hörer")):
        swing = -65
    for side in (-1, 1):
        sx = cx + side * 68 * scale
        elbow = (sx + side * (38 + swing * side) * scale, shoulder + 75 * scale)
        hand = (cx + side * (112 + swing * side) * scale, shoulder + 145 * scale)
        draw.line((sx, shoulder + 15, *elbow, *hand), fill=outline, width=int(33 * scale), joint="curve")
        draw.line((sx, shoulder + 15, *elbow, *hand), fill=colors["clothes"], width=int(23 * scale), joint="curve")
        draw.ellipse((hand[0] - 12, hand[1] - 12, hand[0] + 12, hand[1] + 12), fill=colors["skin"], outline=outline, width=3)
    draw.ellipse((cx - head_r, head_y - head_r, cx + head_r, head_y + head_r),
                 fill=colors["skin"], outline=outline, width=max(3, int(6 * scale)))
    draw.pieslice((cx - head_r - 2, head_y - head_r - 5, cx + head_r + 2, head_y + head_r),
                  180, 350, fill=colors["hair"], outline=outline, width=3)
    blink = abs(math.sin(t * .83 + phase)) > .988
    for side in (-1, 1):
        ex, ey = cx + side * 23 * scale, head_y - 4 * scale
        if blink:
            draw.line((ex - 8, ey, ex + 8, ey), fill=outline, width=4)
        else:
            draw.ellipse((ex - 7, ey - 7, ex + 7, ey + 7), fill="white", outline=outline)
            draw.ellipse((ex - 2, ey - 2, ex + 3, ey + 4), fill=(20, 29, 39))
    openness = .25 + .75 * abs(math.sin(t * 15.7 + phase)) if speaking else .08
    mouth_y = head_y + 30 * scale
    draw.ellipse((cx - 19 * scale, mouth_y - 5, cx + 19 * scale, mouth_y + (7 + 24 * openness) * scale),
                 fill=(63, 18, 27), outline=outline, width=3)


def _active_cue(cues: list[DialogueCue], time: float):
    return next((cue for cue in cues if cue.start <= time < cue.end), None)


def _wrap(text: str, width: int = 48):
    lines, current = [], []
    for word in text.split():
        if len(" ".join(current + [word])) > width and current:
            lines.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        lines.append(" ".join(current))
    return lines[:2]


def render_puppet_master(package: dict[str, Any], output: Path, workdir: Path) -> None:
    audio, cue_sets, durations = build_dialogue_track(package, workdir)
    process = subprocess.Popen(
        [
            "ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{WIDTH}x{HEIGHT}",
            "-r", str(FPS), "-i", "-", "-i", str(audio), "-map", "0:v", "-map", "1:a",
            "-vf", "scale=1920:1080:flags=lanczos", "-c:v", "libx264", "-preset", "slow",
            "-crf", "17", "-c:a", "aac", "-b:a", "192k", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", "-shortest", str(output),
        ],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    characters = package.get("character_bible") or []
    try:
        for scene_index, (scene, duration, cues) in enumerate(zip(package["scenes"], durations, cue_sets)):
            background = _background(scene, scene_index)
            for frame_index in range(round(duration * FPS)):
                time = frame_index / FPS
                frame = background.copy()
                draw = ImageDraw.Draw(frame, "RGBA")
                cue = _active_cue(cues, time)
                positions = (WIDTH * .32, WIDTH * .68, WIDTH * .50, WIDTH * .82)
                for index, character in enumerate(characters[:4]):
                    speaking = bool(cue and cue.speaker == character.get("name"))
                    _draw_character(draw, character, index, positions[index], HEIGHT * .83,
                                    .92 if index < 2 else .68, time, speaking,
                                    cue.emotion if speaking else "", str(scene.get("action", "")))
                draw.rounded_rectangle((30, 28, 370, 72), radius=18, fill=(3, 6, 12, 180))
                draw.text((48, 39), f"SZENE {scene_index + 1:02d}  •  {str(scene.get('location', ''))[:26].upper()}",
                          font=_font(20, True), fill=(216, 229, 241))
                if cue:
                    draw.rounded_rectangle((190, HEIGHT - 120, WIDTH - 190, HEIGHT - 25), radius=22,
                                           fill=(3, 6, 12, 225), outline=(81, 185, 220), width=2)
                    draw.text((225, HEIGHT - 106), cue.speaker.upper(), font=_font(18, True), fill=(92, 211, 235))
                    draw.multiline_text((WIDTH / 2, HEIGHT - 77), "\n".join(_wrap(cue.text)),
                                        font=_font(25, True), fill="white", anchor="ma", align="center")
                if not process.stdin:
                    raise RuntimeError("FFmpeg frame pipe closed")
                process.stdin.write(frame.tobytes())
        process.stdin.close()
        stderr = process.stderr.read().decode(errors="replace") if process.stderr else ""
        if process.wait(timeout=7200):
            raise RuntimeError(f"FFmpeg puppet render failed: {stderr[-2000:]}")
    except Exception:
        process.kill()
        raise
