from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path
from typing import Any


def _safe_drawtext(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def create_fallback_clips(
    package: dict[str, Any], workdir: Path, generation_token: str
) -> list[Path]:
    """Create high-quality CPU-only story visuals without a paid API."""
    import torch
    from diffusers import AutoPipelineForText2Image

    scenes = package.get("scenes") or []
    if len(scenes) != 8:
        raise RuntimeError("The fallback requires exactly eight story scenes")

    model_id = os.getenv("FALLBACK_MODEL_ID", "Lykon/dreamshaper-8")
    steps = int(os.getenv("FALLBACK_INFERENCE_STEPS", "30"))
    shots_per_scene = int(os.getenv("FALLBACK_SHOTS_PER_SCENE", "2"))
    pipeline = AutoPipelineForText2Image.from_pretrained(
        model_id,
        torch_dtype=torch.float32,
        use_safetensors=True,
        low_cpu_mem_usage=True,
    )
    pipeline.enable_attention_slicing("max")
    pipeline.set_progress_bar_config(disable=False)

    character_bible = package["character_bible"]
    negative = (
        "illustration, cartoon, painting, abstract, text, watermark, logo, extra fingers, "
        "missing fingers, fused hands, deformed hands, malformed anatomy, duplicate person, "
        "plastic skin, doll, low resolution, blurry, oversaturated"
    )
    clips: list[Path] = []
    for scene_index, scene in enumerate(scenes):
        shots: list[Path] = []
        for shot_index in range(shots_per_scene):
            angle = (
                "intimate medium cinematic shot, expressive faces, visible hand action"
                if shot_index % 2 == 0
                else "dynamic wide cinematic shot, full body movement, environmental storytelling"
            )
            prompt = (
                "Photorealistic frame from an original German mystery drama, vertical composition, "
                f"{angle}. The same recurring adult characters must follow this exact casting: "
                f"{character_bible}. Visible action: {scene['image_prompt']}. "
                "Natural skin texture, anatomically correct hands, practical cinematic lighting, "
                "35mm film still, high detail, realistic contemporary wardrobe, no text, no logos."
            )
            seed_material = f"{package['revision']}:{scene_index}:{shot_index}".encode("utf-8")
            seed = int(hashlib.sha256(seed_material).hexdigest()[:8], 16)
            image = pipeline(
                prompt=prompt,
                negative_prompt=negative,
                width=512,
                height=768,
                num_inference_steps=steps,
                guidance_scale=7.5,
                generator=torch.Generator(device="cpu").manual_seed(seed),
            ).images[0]
            shot = workdir / f"fallback-{scene_index:02d}-{shot_index:02d}.png"
            image.save(shot, format="PNG", optimize=True)
            shots.append(shot)

        clip = workdir / f"scene-{scene_index:02d}.mp4"
        first, second = shots[0], shots[-1]
        token = _safe_drawtext(generation_token)
        filters = (
            "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
            "zoompan=z='min(zoom+0.0008,1.09)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            "d=105:s=1080x1920:fps=30,setsar=1[a];"
            "[1:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
            "zoompan=z='min(zoom+0.0008,1.09)':x='iw-iw/zoom':y='ih/2-(ih/zoom/2)':"
            "d=105:s=1080x1920:fps=30,setsar=1[b];"
            "[a][b]xfade=transition=fade:duration=0.7:offset=2.8,"
            f"drawtext=text='{token}':fontcolor=white@0.82:fontsize=26:"
            "box=1:boxcolor=black@0.48:boxborderw=10:x=w-tw-28:y=28[v]"
        )
        subprocess.run(
            [
                "ffmpeg", "-y", "-loop", "1", "-t", "3.5", "-i", str(first),
                "-loop", "1", "-t", "3.5", "-i", str(second),
                "-filter_complex", filters, "-map", "[v]", "-t", "6",
                "-an", "-c:v", "libx264", "-preset", "slow", "-crf", "18",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(clip),
            ],
            check=True,
            timeout=1800,
        )
        clips.append(clip)
    del pipeline
    return clips
