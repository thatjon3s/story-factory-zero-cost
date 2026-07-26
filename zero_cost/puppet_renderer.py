from __future__ import annotations

import hashlib
import math
import os
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat


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
    for path in (
        f"/usr/share/fonts/truetype/dejavu/DejaVuSans{'-Bold' if bold else ''}.ttf",
        f"C:/Windows/Fonts/arial{'bd' if bold else ''}.ttf",
    ):
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
    pitch = 0.96 if any(x in words for x in ("tief", "bass", "bariton", "warm")) else 1.02
    pitch *= (0.98, 1.02, 1.0, 1.04)[index % 4]
    speed = 0.94 if any(x in words for x in ("ruhig", "leise", "nachdenklich", "traurig")) else 1.05
    if any(x in words for x in ("fröhlich", "begeistert", "neugierig", "aufgeregt", "lachend")):
        speed = 1.13
    if any(x in words for x in ("erschrocken", "wütend", "alarm", "empört")):
        speed = 1.09
        pitch *= 1.03
    return pitch, speed


def _speak(text: str, output: Path, character: dict[str, Any], index: int, emotion: str) -> None:
    raw = output.with_suffix(".raw.wav")
    models = [
        value.strip() for value in os.getenv(
            "PIPER_VOICE_MODELS",
            os.getenv("PIPER_MODEL_PATH", "de_DE-thorsten-medium"),
        ).split(",") if value.strip()
    ]
    model = models[index % len(models)]
    performed = text
    if any(x in emotion.lower() for x in ("zöger", "unsicher", "nachdenk")):
        performed = performed.replace(",", " ...").replace(" aber ", " ... aber ")
    subprocess.run(
        [
            "piper", "--data-dir", os.getenv("PIPER_DATA_DIR", "."),
            "--model", model, "--output_file", str(raw), "--sentence_silence", "0.18",
        ],
        input=performed, text=True, encoding="utf-8", check=True, timeout=300,
    )
    pitch, speed = _voice_profile(character, index, emotion)
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(raw), "-af",
            f"asetrate=22050*{pitch},aresample=48000,atempo={speed / pitch},"
            "highpass=f=70,lowpass=f=12000,equalizer=f=2600:t=q:w=1.1:g=2.0,"
            "acompressor=threshold=-20dB:ratio=2.5:attack=8:release=90,alimiter=limit=0.92",
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
        gap = 0.28
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
    top = (112, 190, 246) if index % 2 == 0 else (250, 183, 110)
    horizon = (226, 241, 216)
    for y in range(HEIGHT):
        factor = y / HEIGHT
        draw.line((0, y, WIDTH, y), fill=tuple(
            int(top[c] * (1 - factor) + horizon[c] * factor) for c in range(3)
        ))
    draw.ellipse((1010, 55, 1130, 175), fill=(255, 228, 112))
    draw.rectangle((0, 520, WIDTH, HEIGHT), fill=(88, 177, 94))
    if any(x in location for x in ("spielplatz", "park", "wiese", "garten")):
        draw.rectangle((80, 210, 115, 525), fill=(52, 104, 164))
        draw.rectangle((80, 210, 355, 238), fill=(52, 104, 164))
        for x, color in ((150, (238, 92, 103)), (285, (248, 188, 61))):
            draw.line((x, 238, x, 410), fill=(76, 80, 93), width=5)
            draw.rectangle((x - 45, 400, x + 45, 420), fill=color)
        draw.rounded_rectangle((880, 205, 1050, 500), radius=24, fill=(238, 91, 90))
        draw.polygon(((880, 330), (730, 520), (865, 520), (1025, 330)), fill=(255, 203, 65))
        for x in (440, 590, 1110):
            draw.rectangle((x, 370, x + 25, 540), fill=(92, 67, 38))
            draw.ellipse((x - 75, 270, x + 105, 430), fill=(61, 145, 78))
    elif any(x in location for x in ("see", "ufer", "strand", "steg")):
        draw.rectangle((0, 370, WIDTH, 570), fill=(54, 161, 218))
        for y in range(390, 560, 34):
            draw.arc((0, y, WIDTH, y + 24), 190, 350, fill=(194, 234, 248), width=4)
        draw.polygon(((0, 520), (1280, 470), (1280, 720), (0, 720)), fill=(225, 194, 119))
        draw.polygon(((540, 430), (760, 430), (900, 720), (430, 720)), fill=(137, 87, 48))
    else:
        draw.rectangle((0, 330, WIDTH, 520), fill=(248, 225, 190))
        for x in range(70, WIDTH, 230):
            draw.rounded_rectangle(
                (x, 185, x + 150, 470), radius=18,
                fill=(245, 171, 103), outline=(112, 78, 62), width=5,
            )
    glow = Image.new("RGBA", (WIDTH, HEIGHT))
    gd = ImageDraw.Draw(glow)
    gd.ellipse((220, 30, 1060, 710), fill=(255, 244, 190, 28))
    return Image.alpha_composite(image.convert("RGBA"), glow.filter(ImageFilter.GaussianBlur(55)))


def _draw_prop(draw, prop: str, x: float, y: float, scale: float = 1.0) -> None:
    label = prop.lower()
    if any(word in label for word in ("auto", "wagen")):
        draw.rounded_rectangle((x-55*scale, y-28*scale, x+55*scale, y+18*scale),
                               radius=12, fill=(232, 65, 72), outline=(34, 38, 50), width=4)
        draw.rectangle((x-24*scale, y-50*scale, x+25*scale, y-20*scale),
                       fill=(91, 183, 232), outline=(34, 38, 50), width=3)
        for dx in (-34, 34):
            draw.ellipse((x+(dx-13)*scale, y+5*scale, x+(dx+13)*scale, y+31*scale), fill=(32, 37, 48))
    elif any(word in label for word in ("schaufel", "spaten")):
        draw.line((x, y-80*scale, x, y+20*scale), fill=(142, 81, 44), width=max(5, int(9*scale)))
        draw.polygon(((x-28*scale, y+12*scale), (x+28*scale, y+12*scale), (x, y+58*scale)),
                     fill=(235, 65, 78), outline=(48, 40, 49))
    else:
        draw.ellipse((x-34*scale, y-34*scale, x+34*scale, y+34*scale),
                     fill=(255, 190, 43), outline=(50, 51, 62), width=4)


def _draw_character(draw, character, index, x, ground, scale, t, speaking, emotion, action, facing=1):
    colors = _palette(str(character.get("name", index)))
    phase = (_seed(str(character.get("name", index))) % 100) / 17
    action_l = action.lower()
    walk = any(w in action_l for w in ("komm", "geh", "lauf", "renn", "fährt", "fahren"))
    bob = math.sin(t * (5.2 if speaking else 2.1) + phase) * (5 if speaking else 2)
    cx = int(x + (math.sin(min(1, t / 1.3) * math.pi / 2) * 55 * facing if walk else 0))
    gy = int(ground + bob)
    head_r, body_h = int(64 * scale), int(250 * scale)
    shoulder, head_y = gy - body_h + int(85 * scale), gy - body_h
    outline = (34, 39, 53)
    stride = math.sin(t * 8 + phase) * 22 * scale if walk else 0
    for side in (-1, 1):
        hip_x = cx + side * 34 * scale
        foot_x = hip_x + side * stride
        draw.line((hip_x, gy-32*scale, foot_x, gy+105*scale), fill=outline, width=max(12, int(31*scale)))
        draw.line((hip_x, gy-32*scale, foot_x, gy+105*scale), fill=colors["accent"], width=max(8, int(21*scale)))
        draw.rounded_rectangle((foot_x-28*scale, gy+92*scale, foot_x+35*scale, gy+116*scale),
                               radius=10, fill=(245, 245, 241), outline=outline, width=3)
    draw.rounded_rectangle((cx-82*scale, shoulder, cx+82*scale, gy), radius=43,
                           fill=colors["clothes"], outline=outline, width=max(2, int(6*scale)))
    swing = math.sin(t * 5 + phase) * 18 if speaking else math.sin(t * 1.5 + phase) * 5
    if any(w in action_l for w in ("zeig", "deut", "greif", "halt", "teil", "geb", "nimm", "versteck")):
        swing = -62
    for side in (-1, 1):
        sx = cx + side * 68 * scale
        elbow = (sx + side * (38 + swing * side) * scale, shoulder + 75 * scale)
        hand = (cx + side * (112 + swing * side) * scale, shoulder + 145 * scale)
        draw.line((sx, shoulder+15, *elbow, *hand), fill=outline, width=max(12, int(33*scale)), joint="curve")
        draw.line((sx, shoulder+15, *elbow, *hand), fill=colors["clothes"], width=max(8, int(23*scale)), joint="curve")
        draw.ellipse((hand[0]-12, hand[1]-12, hand[0]+12, hand[1]+12), fill=colors["skin"], outline=outline, width=3)
    draw.ellipse((cx-head_r, head_y-head_r, cx+head_r, head_y+head_r),
                 fill=colors["skin"], outline=outline, width=max(3, int(6*scale)))
    hair_box = (cx-head_r-8, head_y-head_r-12, cx+head_r+8, head_y+head_r)
    draw.pieslice(hair_box, 175, 350, fill=colors["hair"], outline=outline, width=3)
    if index % 2 == 0:
        for dx in (-48, -30, 35, 51):
            draw.ellipse((cx+dx*scale-14, head_y-24*scale, cx+dx*scale+12, head_y+76*scale), fill=colors["hair"])
    else:
        draw.polygon(((cx-55*scale, head_y-42*scale), (cx, head_y-78*scale),
                      (cx+52*scale, head_y-38*scale)), fill=colors["hair"])
    blink = abs(math.sin(t * .83 + phase)) > .988
    for side in (-1, 1):
        ex, ey = cx + side*23*scale + facing*4, head_y - 4*scale
        if blink:
            draw.line((ex-8, ey, ex+8, ey), fill=outline, width=4)
        else:
            draw.ellipse((ex-7, ey-7, ex+7, ey+7), fill="white", outline=outline)
            draw.ellipse((ex-2+facing*2, ey-2, ex+3+facing*2, ey+4), fill=(20, 29, 39))
    worried = any(w in emotion.lower() for w in ("zöger", "traurig", "unsicher", "besorgt"))
    happy = any(w in emotion.lower() for w in ("freu", "begeistert", "lach", "erleichtert"))
    brow_y = head_y - 27*scale
    for side in (-1, 1):
        tilt = (-7 if worried else 5 if happy else 0) * side
        bx = cx + side*24*scale
        draw.line((bx-10*scale, brow_y-tilt, bx+10*scale, brow_y+tilt), fill=outline, width=4)
    openness = .25 + .75 * abs(math.sin(t * 15.7 + phase)) if speaking else .08
    mouth_y = head_y + 30*scale
    draw.ellipse((cx-19*scale, mouth_y-5, cx+19*scale, mouth_y+(7+24*openness)*scale),
                 fill=(137, 38, 55), outline=outline, width=3)


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


def render_storyboard_frame(
    package: dict[str, Any], scene_index: int, time: float, cue: DialogueCue | None = None
) -> Image.Image:
    scene = package["scenes"][scene_index]
    frame = _background(scene, scene_index)
    draw = ImageDraw.Draw(frame, "RGBA")
    characters = package.get("character_bible") or []
    shot = scene_index % 4
    positions = (
        (WIDTH*.29, WIDTH*.71),
        (WIDTH*.36, WIDTH*.76),
        (WIDTH*.24, WIDTH*.62),
        (WIDTH*.38, WIDTH*.66),
    )[shot]
    scale = (0.86, 1.02, 0.78, 1.0)[shot]
    for index, character in enumerate(characters[:2]):
        speaking = bool(cue and cue.speaker == character.get("name"))
        _draw_character(
            draw, character, index, positions[index], HEIGHT*.77, scale, time, speaking,
            cue.emotion if speaking else "", str(scene.get("action", "")),
            1 if index == 0 else -1,
        )
    props = scene.get("props") or []
    if props:
        phase = min(1.0, time / max(1.0, float(scene.get("duration_seconds", 8))))
        prop_x = positions[0] + (positions[1] - positions[0]) * phase if any(
            word in str(scene.get("action", "")).lower() for word in ("teil", "gibt", "reicht", "gemeinsam")
        ) else WIDTH*.50
        _draw_prop(draw, str(props[0]), prop_x, HEIGHT*.73, .85)
    if cue:
        draw.rounded_rectangle((190, HEIGHT-116, WIDTH-190, HEIGHT-24), radius=22,
                               fill=(24, 31, 48, 220), outline=(255, 216, 94), width=3)
        draw.text((225, HEIGHT-102), cue.speaker.upper(), font=_font(18, True), fill=(255, 220, 100))
        draw.multiline_text((WIDTH/2, HEIGHT-72), "\n".join(_wrap(cue.text)),
                            font=_font(25, True), fill="white", anchor="ma", align="center")
    return frame


def render_contact_sheet(package: dict[str, Any], destination: Path) -> dict[str, float]:
    frames = []
    for index, scene in enumerate(package["scenes"]):
        cues = [
            DialogueCue(str(d["speaker"]), str(d["text"]), str(d.get("emotion", "")), 0, 8)
            for d in scene.get("dialogue", [])
        ]
        frame = render_storyboard_frame(package, index, 3.0, cues[0] if cues else None)
        frames.append(frame.convert("RGB").resize((480, 270)))
    sheet = Image.new("RGB", (960, math.ceil(len(frames)/2)*270), "white")
    for index, frame in enumerate(frames):
        sheet.paste(frame, ((index % 2)*480, (index//2)*270))
    sheet.save(destination, quality=92)
    saturation = sum(ImageStat.Stat(frame.convert("HSV")).mean[1] for frame in frames) / len(frames)
    differences = []
    for left, right in zip(frames, frames[1:]):
        a, b = ImageStat.Stat(left).mean, ImageStat.Stat(right).mean
        differences.append(sum(abs(x-y) for x, y in zip(a, b)) / 3)
    return {
        "mean_saturation": round(saturation, 2),
        "mean_scene_difference": round(sum(differences) / max(1, len(differences)), 2),
    }


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
    try:
        for scene_index, (scene, duration, cues) in enumerate(zip(package["scenes"], durations, cue_sets)):
            for frame_index in range(round(duration * FPS)):
                time = frame_index / FPS
                cue = _active_cue(cues, time)
                frame = render_storyboard_frame(package, scene_index, time, cue)
                if not process.stdin:
                    raise RuntimeError("FFmpeg frame pipe closed")
                process.stdin.write(frame.tobytes())
        process.stdin.close()
        stderr = process.stderr.read().decode(errors="replace") if process.stderr else ""
        if process.wait(timeout=7200):
            raise RuntimeError(f"FFmpeg cutout render failed: {stderr[-2000:]}")
    except Exception:
        process.kill()
        raise
