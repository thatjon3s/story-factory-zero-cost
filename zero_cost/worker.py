from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from cost_guard import CostGuard
from memory import SupabaseMemory, slugify
from blender_renderer import render_blender_master
from screenplay import finalize_package
from supabase_control import SupabaseControlPlane


TZ = ZoneInfo(os.getenv("APP_TIMEZONE", "Europe/Berlin"))
PUBLISH_DAYS = tuple(int(x) for x in os.getenv("PUBLISH_DAYS", "0,2,4").split(","))
PUBLISH_HOUR, PUBLISH_MINUTE = map(int, os.getenv("PUBLISH_TIME", "18:00").split(":"))
RESERVE_TARGET = int(os.getenv("RESERVE_TARGET", "4"))
PIPELINE_TARGET = int(os.getenv("PIPELINE_TARGET", "8"))


class ControlPlane:
    LABELS = {
        "episode": "1f6feb", "idea": "6f42c1", "producing": "d4c5f9",
        "awaiting_approval": "fbca04", "approved": "0e8a16",
        "approved_reserve": "2da44e", "scheduled": "0969da",
        "published": "1d76db", "failed": "d73a4a", "rejected": "b60205",
    }

    def __init__(self) -> None:
        self.url = f"https://api.github.com/repos/{os.environ['GITHUB_REPOSITORY']}"
        self.key = os.environ["GITHUB_TOKEN"]
        self.owner_id = os.environ.get("GITHUB_REPOSITORY_OWNER", "owner")
        self.headers = {
            "Authorization": f"Bearer {self.key}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        with httpx.Client(timeout=60) as client:
            response = client.request(method, f"{self.url}/{path}", headers=self.headers, **kwargs)
            response.raise_for_status()
            return response.json() if response.content else None

    def ensure_labels(self) -> None:
        existing = {label["name"] for label in self._request("GET", "labels", params={"per_page": 100})}
        for name, color in self.LABELS.items():
            if name not in existing:
                self._request("POST", "labels", json={"name": name, "color": color})

    def get(self, episode_id: int) -> dict[str, Any]:
        return self._data(self._request("GET", f"issues/{episode_id}"))

    @staticmethod
    def _body(row: dict[str, Any]) -> str:
        review = ""
        if row.get("status") == "awaiting_approval" and row.get("preview_youtube_id"):
            review = (
                "## Deine Freigabe\n\n"
                f"[Private Prüffassung auf YouTube ansehen](https://youtu.be/{row['preview_youtube_id']})\n\n"
                "- **Freigeben:** Rechts unter `Labels` das Label `approved` hinzufügen.\n"
                "- **Nicht freigeben:** Nichts tun; das Video bleibt privat und wird nicht veröffentlicht.\n"
                f"- **Frist:** `{row.get('approval_deadline', 'nicht gesetzt')}`\n\n"
            )
        return review + "## Automationsstatus\n\n```json\n" + json.dumps(row, ensure_ascii=False, indent=2) + "\n```"

    @staticmethod
    def _data(issue: dict[str, Any]) -> dict[str, Any]:
        match = re.search(r"```json\s*(\{.*?\})\s*```", issue.get("body") or "", re.S)
        data = json.loads(match.group(1)) if match else {}
        data["id"] = issue["number"]
        data["issue_url"] = issue["html_url"]
        data["labels"] = [x["name"] for x in issue.get("labels", [])]
        return data

    def episodes(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        issues = self._request("GET", "issues", params={"state": "all", "labels": "episode", "per_page": 100})
        rows = [self._data(x) for x in issues if "pull_request" not in x]
        for row in rows:
            if row.get("status") == "awaiting_approval" and "approved" in row["labels"]:
                package = row.get("package") or {}
                row["status"] = "approved_reserve"; row["approved_revision"] = package.get("revision")
                self.update(row["id"], {"status": row["status"], "approved_revision": row["approved_revision"]})
            elif row.get("status") == "awaiting_approval" and "rejected" in row["labels"]:
                row["status"] = "rejected"
                self.update(row["id"], {"status": "rejected", "rejected_reason": "Neue Inszenierung angefordert"})
        rows.sort(key=lambda x: int(x.get("episode_no", 0)))
        return [x for x in rows if not status or x.get("status") == status][:limit]

    def create_automatic_idea(self) -> dict[str, Any]:
        rows = self.episodes(limit=1000)
        series = os.getenv("AUTO_SERIES_NAME", "Die letzte Leitung")
        universe = os.getenv("AUTO_UNIVERSE_NAME", "Story Factory Universe")
        series_slug = slugify(series)
        episode_no = max(
            (
                int(row["episode_no"])
                for row in rows
                if (row.get("series_slug") or slugify(row.get("series_name", ""))) == series_slug
            ),
            default=0,
        ) + 1
        bible = os.getenv(
            "SERIES_BIBLE",
            "Eine nächtliche Notrufzentrale empfängt unmögliche Anrufe aus anderen Zeiten und Wirklichkeiten. "
            "Jede Folge löst ein eigenes Rätsel teilweise und erweitert zugleich das übergeordnete Geheimnis."
        )
        payload = {
            "owner_id": self.owner_id, "universe_name": universe,
            "universe_slug": slugify(universe), "series_name": series, "series_slug": series_slug,
            "episode_no": episode_no,
            "title": f"Automatisch entwickelte Folge {episode_no}",
            "premise": f"{bible} Erfinde einen neuen Konflikt, der sich klar von früheren Folgen unterscheidet.",
        }
        payload.update({"status": "idea", "package": {}, "created_at": datetime.now(timezone.utc).isoformat()})
        issue = self._request("POST", "issues", json={"title": f"[Folge {episode_no}] {payload['title']}", "body": self._body(payload), "labels": ["episode", "idea"]})
        return self._data(issue)

    def update(self, episode_id: int, values: dict[str, Any], expected: str | None = None) -> dict[str, Any]:
        issue = self._request("GET", f"issues/{episode_id}"); row = self._data(issue)
        if expected and row.get("status") != expected:
            raise RuntimeError(f"Concurrent or invalid transition for episode {episode_id}")
        row.update(values); row["updated_at"] = datetime.now(timezone.utc).isoformat()
        labels = ["episode", row.get("status", "idea")]
        if "approved" in row.get("labels", []): labels.append("approved")
        payload = {"title": f"[Folge {row.get('episode_no')}] {row.get('title')}", "body": self._body(row), "labels": labels}
        if row.get("status") == "awaiting_approval":
            payload["assignees"] = [self.owner_id]
        saved = self._request("PATCH", f"issues/{episode_id}", json=payload)
        return self._data(saved)

    def event(self, kind: str, episode_id: int | None = None, **detail: Any) -> None:
        if episode_id:
            self._request("POST", f"issues/{episode_id}/comments", json={"body": f"**{kind}** — `{json.dumps(detail, ensure_ascii=False)}`"})

    def metric(self, episode_id: int, values: dict[str, Any]) -> None:
        self.event("metrics", episode_id, **values)

    def has_event(self, kind: str, episode_id: int) -> bool:
        rows = self._request("GET", f"issues/{episode_id}/comments", params={"per_page": 100})
        return any(f"**{kind}**" in (x.get("body") or "") for x in rows)


def notify(subject: str, body: str) -> None:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    if token and chat:
        response = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": f"{subject}\n\n{body}"}, timeout=30,
        )
        response.raise_for_status()
        return
    print(f"{subject}\n\n{body}", file=sys.stderr)


def telegram_review(
    video: Path, episode_id: int, package: dict[str, Any], youtube_id: str, deadline: datetime
) -> None:
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat:
        notify(
            f"Freigabe benötigt: {package['title']}",
            f"Prüffassung: https://youtu.be/{youtube_id}\nFrist: {deadline.isoformat()}",
        )
        return
    revision = package["revision"]
    keyboard = {"inline_keyboard": [[
        {"text": "✅ Freigeben", "callback_data": f"approve:{episode_id}:{revision}"},
        {"text": "❌ Ablehnen", "callback_data": f"reject:{episode_id}:{revision}"},
    ]]}
    caption = (
        f"🎬 {package['title']}\n\n"
        "Format: kanonisches 16:9-Hauptvideo\n"
        f"Serie: {package.get('series_slug', 'unbekannt')}\n"
        f"Generator: {package.get('visual_mode', 'unbekannt')}\n"
        f"Token: {package.get('generation_token', package['revision'])}\n\n"
        f"Bitte bis {deadline.astimezone(TZ).strftime('%d.%m.%Y, %H:%M Uhr')} prüfen. "
        "Danach bleibt die Folge privat und eine Reservefolge übernimmt den Termin.\n"
        f"Vorschau: https://youtu.be/{youtube_id}"
    )
    api = f"https://api.telegram.org/bot{token}"
    sent = False
    max_bytes = int(os.getenv("TELEGRAM_MAX_VIDEO_MB", "49")) * 1024 * 1024
    if video.stat().st_size <= max_bytes:
        try:
            with video.open("rb") as handle:
                response = httpx.post(
                    f"{api}/sendVideo",
                    data={
                        "chat_id": chat, "caption": caption,
                        "reply_markup": json.dumps(keyboard), "supports_streaming": "true",
                    },
                    files={"video": (video.name, handle, "video/mp4")},
                    timeout=300,
                )
            response.raise_for_status()
            sent = True
        except httpx.HTTPError:
            sent = False
    if not sent:
        response = httpx.post(
            f"{api}/sendMessage",
            json={"chat_id": chat, "text": caption, "reply_markup": keyboard},
            timeout=30,
        )
        response.raise_for_status()


def process_telegram_callbacks(control: ControlPlane) -> None:
    token, allowed_chat = os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not allowed_chat:
        return
    api = f"https://api.telegram.org/bot{token}"
    response = httpx.get(
        f"{api}/getUpdates",
        params={"timeout": 0, "allowed_updates": json.dumps(["callback_query"])},
        timeout=30,
    )
    response.raise_for_status()
    updates = response.json().get("result", [])
    if not updates:
        return
    for update in updates:
        query = update.get("callback_query") or {}
        query_id = query.get("id")
        message = query.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        match = re.fullmatch(r"(approve|reject):(\d+):([0-9a-f]{16})", query.get("data") or "")
        answer = "Diese Schaltfläche ist ungültig."
        accepted = False
        if query_id and chat_id == str(allowed_chat) and match:
            action, episode_text, revision = match.groups()
            episode_id = int(episode_text)
            episode = control.get(episode_id)
            current_revision = (episode.get("package") or {}).get("revision")
            if episode.get("status") != "awaiting_approval" or current_revision != revision:
                answer = "Diese Prüffassung ist nicht mehr aktuell."
            elif action == "approve":
                control.update(
                    episode_id,
                    {"status": "approved_reserve", "approved_revision": revision},
                    "awaiting_approval",
                )
                control.event("telegram_approved", episode_id, revision=revision)
                answer, accepted = "Freigegeben – die Veröffentlichung wird automatisch geplant.", True
            else:
                control.update(
                    episode_id,
                    {"status": "rejected", "rejected_reason": "Über Telegram abgelehnt"},
                    "awaiting_approval",
                )
                control.event("telegram_rejected", episode_id, revision=revision)
                answer, accepted = "Abgelehnt – eine neue Fassung wird automatisch produziert.", True
        if query_id:
            httpx.post(
                f"{api}/answerCallbackQuery",
                json={"callback_query_id": query_id, "text": answer, "show_alert": False},
                timeout=30,
            ).raise_for_status()
        if accepted and message.get("message_id"):
            httpx.post(
                f"{api}/editMessageReplyMarkup",
                json={
                    "chat_id": chat_id, "message_id": message["message_id"],
                    "reply_markup": {"inline_keyboard": []},
                },
                timeout=30,
            ).raise_for_status()
            httpx.post(
                f"{api}/sendMessage",
                json={"chat_id": chat_id, "text": answer},
                timeout=30,
            ).raise_for_status()
    newest = max(int(item["update_id"]) for item in updates)
    httpx.get(f"{api}/getUpdates", params={"offset": newest + 1, "timeout": 0}, timeout=30).raise_for_status()


def ollama_story(episode: dict[str, Any]) -> dict[str, Any]:
    CostGuard().require_free("ollama")
    universe_slug = episode.get("universe_slug") or slugify(
        episode.get("universe_name") or os.getenv("AUTO_UNIVERSE_NAME", "Story Factory Universe")
    )
    series_slug = episode.get("series_slug") or slugify(episode["series_name"])
    canon = SupabaseMemory().context(universe_slug, series_slug)
    prompt = f"""Du bist Showrunner einer deutschen seriellen Live-Action-Mysteryserie.
Universum: {episode.get('universe_name', 'Story Factory Universe')}
Serie: {episode['series_name']}, Folge {episode['episode_no']}.
Prämisse: {episode['premise'] or episode['title']}.

{canon.prompt_block()}

Erstelle ein kausal geschlossenes Drehbuch für ein 16:9-Hauptvideo aus exakt acht Szenen zu je acht Sekunden.
Es gibt keinen Erzähler, keine Voice-over-Stimme und keine erklärenden Texttafeln. Die Geschichte wird nur durch
sichtbare Handlungen und kurze deutsche Dialoge erzählt. Jede Szene verändert den Zustand und folgt logisch aus der
vorherigen. Niemand besitzt Wissen, das er nicht erworben hat. Wiederhole weder Informationen noch Formulierungen.
Maximal zwei kurze Sätze pro Figur und Szene. Mindestens zwei Figuren sprechen.

Baue Cold Open, steigenden Konflikt, eine Teilauflösung und einen präzisen visuellen Cliffhanger. Bewahre Gesichter,
Alter, Haare, Kleidung und Stimme jeder Figur. `state_before` muss mit `state_after` der vorherigen Szene
übereinstimmen. Schreibe am Ende eine knappe Episodenzusammenfassung und nur neue oder veränderte kanonische Fakten.
Universumsfakten nur, wenn sie tatsächlich serienübergreifend gelten. Gib ausschließlich valides JSON zurück."""
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "thumbnail_text": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "character_bible": {
                "type": "array", "minItems": 2, "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"}, "appearance": {"type": "string"},
                        "wardrobe": {"type": "string"}, "voice": {"type": "string"},
                    },
                    "required": ["name", "appearance", "wardrobe", "voice"],
                },
            },
            "scenes": {
                "type": "array", "minItems": 8, "maxItems": 8,
                "items": {
                    "type": "object",
                    "properties": {
                        "duration_seconds": {"type": "integer", "enum": [8]},
                        "location": {"type": "string"}, "action": {"type": "string"},
                        "camera": {"type": "string"}, "lighting": {"type": "string"},
                        "dialogue": {
                            "type": "array", "minItems": 1, "maxItems": 4,
                            "items": {
                                "type": "object",
                                "properties": {
                                    "speaker": {"type": "string"}, "emotion": {"type": "string"},
                                    "text": {"type": "string"},
                                },
                                "required": ["speaker", "emotion", "text"],
                            },
                        },
                        "state_before": {"type": "object"},
                        "state_after": {"type": "object"},
                    },
                    "required": [
                        "duration_seconds", "location", "action", "camera", "lighting",
                        "dialogue", "state_before", "state_after",
                    ],
                },
            },
            "memory_delta": {
                "type": "object",
                "properties": {
                    "episode_summary": {"type": "string"},
                    "resolved_threads": {"type": "array", "items": {"type": "string"}},
                    "opened_threads": {"type": "array", "items": {"type": "string"}},
                    "next_episode_hooks": {"type": "array", "items": {"type": "string"}},
                    "canon_entries": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "scope": {"type": "string", "enum": ["universe", "series"]},
                                "key": {"type": "string"}, "summary": {"type": "string"},
                                "importance": {"type": "integer", "minimum": 1, "maximum": 100},
                            },
                            "required": ["scope", "key", "summary", "importance"],
                        },
                    },
                },
                "required": [
                    "episode_summary", "resolved_threads", "opened_threads",
                    "next_episode_hooks", "canon_entries",
                ],
            },
        },
        "required": [
            "title", "description", "thumbnail_text", "tags",
            "character_bible", "scenes", "memory_delta",
        ],
    }
    payload = {
        "model": os.getenv("OLLAMA_MODEL", "qwen3:4b"), "prompt": prompt, "stream": False,
        "format": schema, "think": False,
        "options": {"num_predict": 5000, "temperature": 0.65},
    }
    failures: list[str] = []
    for attempt in range(4):
        try:
            response = httpx.post("http://127.0.0.1:11434/api/generate", json=payload, timeout=1200)
            response.raise_for_status()
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            detail = ""
            if isinstance(exc, httpx.HTTPStatusError):
                detail = exc.response.text[:500].replace("\n", " ")
            failures.append(
                f"attempt {attempt + 1}: {type(exc).__name__}"
                + (f" ({detail})" if detail else "")
            )
            if attempt < 3:
                time.sleep(20 * (attempt + 1))
                continue
            break
        try:
            package = json.loads(response.json()["response"])
            package.update({
                "package_version": 2,
                "universe_slug": universe_slug,
                "series_slug": series_slug,
                "generator": payload["model"],
            })
            return finalize_package(package)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            failures.append(f"attempt {attempt + 1}: {type(exc).__name__}: {exc}")
        payload["prompt"] += (
            "\nDie vorige Ausgabe hat die Logikprüfung nicht bestanden. Erzeuge das vollständige JSON neu. "
            "Keine Wiederholungen, keine Zustandswidersprüche und kein Erzähler."
        )
    raise RuntimeError("Local story model failed validation: " + "; ".join(failures))


def story_chunks(script: str, max_words: int = 11) -> list[str]:
    sentences = [c.strip() for c in re.split(r"\n+|(?<=[.!?…])\s+(?=[A-ZÄÖÜ„\"])", script) if c.strip()]
    chunks: list[str] = []
    for sentence in sentences:
        words = sentence.split()
        if len(words) <= max_words:
            chunks.append(sentence)
            continue
        while words:
            take = min(max_words, len(words))
            if len(words) > max_words:
                for index in range(min(max_words, len(words)) - 1, max(3, max_words - 5), -1):
                    if words[index].endswith((",", ";", ":")):
                        take = index + 1
                        break
            chunks.append(" ".join(words[:take]))
            words = words[take:]
    return chunks


def media_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        check=True, capture_output=True, text=True, timeout=60,
    )
    return float(result.stdout.strip())


def paragraphs_to_srt(script: str, destination: Path, total_duration: float | None = None) -> None:
    chunks = story_chunks(script)
    words_total = max(1, sum(len(c.split()) for c in chunks))
    cursor = 0.0
    lines: list[str] = []
    available = total_duration or (words_total / 2.25)
    for index, chunk in enumerate(chunks, 1):
        duration = max(1.15, len(chunk.split()) / words_total * available)
        start, end = cursor, cursor + duration
        def stamp(seconds: float) -> str:
            millis = int(seconds * 1000); hours, rem = divmod(millis, 3_600_000); minutes, rem = divmod(rem, 60_000); secs, ms = divmod(rem, 1000)
            return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"
        lines.extend([str(index), f"{stamp(start)} --> {stamp(end)}", chunk, ""])
        cursor = end
    destination.write_text("\n".join(lines), encoding="utf-8")


def narration_style(text: str) -> tuple[float, float]:
    lowered = text.lower()
    if any(word in lowered for word in ("flüster", "stille", "leise", "atem", "wartete", "dunkel", "niemand")):
        return 1.13, 0.62
    if "!" in text or any(word in lowered for word in ("plötzlich", "alarm", "schrie", "rannte", "jetzt", "gefahr")):
        return 0.92, 0.24
    if "?" in text:
        return 1.04, 0.48
    return 1.01, 0.36


def synthesize_voice(script: str, output: Path) -> None:
    CostGuard().require_free("piper")
    sentences = [c.strip() for c in re.split(r"(?<=[.!?…])\s+(?=[A-ZÄÖÜ„\"])", script) if c.strip()]
    workdir = output.parent / "narration_parts"; workdir.mkdir(exist_ok=True)
    parts: list[Path] = []
    for index in range(0, len(sentences), 4):
        text = " ".join(sentences[index:index + 4])
        length_scale, silence = narration_style(text)
        part = workdir / f"part-{len(parts):03d}.wav"
        command = [
            "piper", "--model", os.environ["PIPER_MODEL_PATH"], "--output_file", str(part),
            "--length_scale", str(length_scale), "--sentence_silence", str(silence),
        ]
        subprocess.run(command, input=text, text=True, encoding="utf-8", check=True, timeout=300)
        parts.append(part)
    concat = workdir / "parts.txt"
    concat.write_text("\n".join(f"file '{part.as_posix()}'" for part in parts), encoding="utf-8")
    pitch = float(os.getenv("NARRATOR_PITCH", "0.94"))
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
        "-af", f"asetrate=22050*{pitch},aresample=22050,atempo={1 / pitch:.5f},dynaudnorm=f=250:g=9",
        "-c:a", "pcm_s16le", str(output),
    ], check=True, timeout=600)


def scene_svg(text: str, index: int, destination: Path) -> None:
    lowered = text.lower()
    palettes = [("#070a16", "#183057", "#5d7cff"), ("#10070f", "#4b1535", "#ff477e"), ("#061313", "#164e50", "#4fe0c1")]
    dark, mid, glow = palettes[index % len(palettes)]
    symbols = []
    if any(word in lowered for word in ("telefon", "anruf", "leitung", "hörer")):
        symbols.append('<path d="M700 310 C620 420 650 620 790 720 L900 610 820 520 755 575 C710 520 700 455 735 405 L810 450 885 335 790 265 Z"/>')
    if any(word in lowered for word in ("uhr", "zeit", "sekunde", "jahr")):
        symbols.append('<circle cx="1200" cy="470" r="210"/><path d="M1200 330V470L1310 545"/>')
    if any(word in lowered for word in ("tür", "gang", "raum", "zentrale", "gebäude")):
        symbols.append('<path d="M650 180H1260V850H650Z M760 290H1140V850H760Z M1060 565h18"/>')
    if not symbols:
        symbols.append('<path d="M540 780L820 320 1030 650 1240 250 1430 780Z"/>')
    title_words = [w.strip('.,:;!?„“\"') for w in text.split() if len(w.strip('.,:;!?„“\"')) > 4][:5]
    title = html.escape(" ".join(title_words).upper() or "DIE LETZTE LEITUNG")
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080" viewBox="0 0 1920 1080">
<defs><radialGradient id="g"><stop stop-color="{mid}"/><stop offset="1" stop-color="{dark}"/></radialGradient>
<filter id="blur"><feGaussianBlur stdDeviation="34"/></filter></defs>
<rect width="1920" height="1080" fill="url(#g)"/><circle cx="{380 + index * 97 % 1200}" cy="420" r="330" fill="{glow}" opacity=".16" filter="url(#blur)"/>
<g fill="none" stroke="{glow}" stroke-width="18" stroke-linecap="round" stroke-linejoin="round" opacity=".82">{''.join(symbols)}</g>
<g fill="none" stroke="white" opacity=".10">{''.join(f'<circle cx="{160 + n * 170}" cy="{120 + (n * 83) % 700}" r="{20 + n * 7}"/>' for n in range(9))}</g>
<text x="110" y="135" fill="white" opacity=".78" font-family="DejaVu Sans" font-size="34" letter-spacing="8">SZENE {index + 1:02d}</text>
<text x="110" y="940" fill="white" font-family="DejaVu Sans" font-weight="bold" font-size="54">{title}</text>
</svg>'''
    destination.write_text(svg, encoding="utf-8")


def build_scene_images(script: str, workdir: Path, count: int = 12) -> list[Path]:
    sentences = [c.strip() for c in re.split(r"(?<=[.!?…])\s+", script) if c.strip()]
    images: list[Path] = []
    for index in range(count):
        start = index * len(sentences) // count
        end = max(start + 1, (index + 1) * len(sentences) // count)
        svg = workdir / f"scene-{index:02d}.svg"; png = workdir / f"scene-{index:02d}.png"
        scene_svg(" ".join(sentences[start:end]), index, svg)
        subprocess.run(["rsvg-convert", "-w", "1920", "-h", "1080", "-o", str(png), str(svg)], check=True, timeout=60)
        images.append(png)
    return images


def render_video(
    package: dict[str, Any], voice: Path, output: Path, workdir: Path, clips: list[Path] | None = None
) -> None:
    CostGuard().require_free("ffmpeg")
    duration = media_duration(voice)
    subtitle = workdir / "subtitles.srt"
    paragraphs_to_srt(package["script"], subtitle, duration)
    if clips:
        concat = workdir / "clips.txt"
        concat.write_text("\n".join(f"file '{clip.as_posix()}'" for clip in clips), encoding="utf-8")
        visual = workdir / "human-action.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-stream_loop", "-1", "-f", "concat", "-safe", "0", "-i", str(concat),
            "-t", f"{duration:.3f}",
            "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30",
            "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", str(visual),
        ], check=True, timeout=3600)
        escaped = str(subtitle.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
        subprocess.run([
            "ffmpeg", "-y", "-i", str(visual), "-i", str(voice),
            "-vf", f"subtitles='{escaped}':force_style='FontName=DejaVu Sans,FontSize=11,PrimaryColour=&H00FFFFFF,OutlineColour=&H90000000,BorderStyle=3,Outline=1,MarginV=40,Alignment=2'",
            "-map", "0:v", "-map", "1:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-c:a", "aac", "-b:a", "160k", "-shortest", "-movflags", "+faststart", str(output),
        ], check=True, timeout=3600)
        return
    images = build_scene_images(package["script"], workdir)
    escaped = str(subtitle.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    scene_duration = duration / len(images)
    inputs: list[str] = []
    chains: list[str] = []
    for index, image in enumerate(images):
        inputs += ["-loop", "1", "-t", f"{scene_duration + 0.6:.3f}", "-i", str(image)]
        direction = "iw-iw/zoom" if index % 2 else "0"
        chains.append(f"[{index}:v]scale=2048:1152,zoompan=z='min(zoom+0.0007,1.12)':x='{direction}':y='ih/2-(ih/zoom/2)':d={int((scene_duration + 0.6) * 30)}:s=1920x1080:fps=30,setsar=1[v{index}]")
    current = "v0"; elapsed = scene_duration
    for index in range(1, len(images)):
        out = f"x{index}"
        chains.append(f"[{current}][v{index}]xfade=transition=fade:duration=0.6:offset={elapsed - 0.6:.3f}[{out}]")
        current = out; elapsed += scene_duration
    audio_index = len(images)
    chains.append(f"[{current}]subtitles='{escaped}':force_style='FontName=DejaVu Sans,FontSize=16,PrimaryColour=&H00FFFFFF,OutlineColour=&H70000000,BorderStyle=3,Outline=1,Shadow=0,MarginV=24,Alignment=2'[v]")
    subprocess.run([
        "ffmpeg", "-y", *inputs, "-i", str(voice), "-filter_complex", ";".join(chains), "-map", "[v]", "-map", f"{audio_index}:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac", "-b:a", "160k",
        "-shortest", "-movflags", "+faststart", str(output)
    ], check=True, timeout=3600)


def render_dialogue_master(clips: list[Path], output: Path, workdir: Path) -> None:
    """Join native-audio scenes into the canonical 16:9 master."""
    CostGuard().require_free("ffmpeg")
    if not clips:
        raise RuntimeError("No dialogue scenes were generated")
    concat = workdir / "dialogue-scenes.txt"
    concat.write_text(
        "\n".join(f"file '{clip.resolve().as_posix()}'" for clip in clips),
        encoding="utf-8",
    )
    subprocess.run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
        "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,"
               "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,fps=24",
        "-af", "loudnorm=I=-16:LRA=9:TP=-1.5",
        "-c:v", "libx264", "-preset", "slow", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output),
    ], check=True, timeout=7200)


class YouTube:
    scopes = [
        "https://www.googleapis.com/auth/youtube",
        "https://www.googleapis.com/auth/yt-analytics-monetary.readonly",
    ]

    def credentials(self):
        from google.oauth2.credentials import Credentials
        return Credentials(
            token=None, refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
            token_uri="https://oauth2.googleapis.com/token", client_id=os.environ["YOUTUBE_CLIENT_ID"],
            client_secret=os.getenv("YOUTUBE_CLIENT_SECRET") or None, scopes=self.scopes,
        )

    def service(self, name: str = "youtube", version: str = "v3"):
        from googleapiclient.discovery import build
        return build(name, version, credentials=self.credentials(), cache_discovery=False)

    def upload_private(self, video: Path, package: dict[str, Any]) -> str:
        CostGuard().require_free("youtube")
        from googleapiclient.http import MediaFileUpload
        body = {"snippet": {
            "title": package["title"][:100],
            "description": (
                package["description"]
                + "\n\nKanonische Folge einer fiktionalen, KI-unterstützten Serie."
            ),
            "tags": package.get("tags", []), "categoryId": "24", "defaultLanguage": "de",
        }, "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False}}
        request = self.service().videos().insert(
            part="snippet,status", body=body, media_body=MediaFileUpload(str(video), chunksize=8 * 1024 * 1024, resumable=True)
        )
        response = None
        while response is None:
            _, response = request.next_chunk()
        return response["id"]

    def schedule(self, video_id: str, when: datetime) -> None:
        self.service().videos().update(part="status", body={"id": video_id, "status": {
            "privacyStatus": "private", "publishAt": when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "selfDeclaredMadeForKids": False,
        }}).execute()

    def analytics(self, video_id: str, start: date, end: date) -> dict[str, Any]:
        response = self.service("youtubeAnalytics", "v2").reports().query(
            ids="channel==MINE", startDate=start.isoformat(), endDate=end.isoformat(),
            metrics="views,estimatedMinutesWatched,averageViewDuration,subscribersGained,estimatedRevenue",
            filters=f"video=={video_id}",
        ).execute()
        row = (response.get("rows") or [[0, 0, 0, 0, 0]])[0]
        return {"views": row[0], "watch_minutes": row[1], "average_view_duration": row[2],
                "subscribers_gained": row[3], "estimated_revenue": row[4], "raw": response}


def next_slots(count: int) -> list[datetime]:
    now = datetime.now(TZ); cursor = now.date(); slots: list[datetime] = []
    while len(slots) < count:
        slot = datetime(cursor.year, cursor.month, cursor.day, PUBLISH_HOUR, PUBLISH_MINUTE, tzinfo=TZ)
        if slot.weekday() in PUBLISH_DAYS and slot > now + timedelta(hours=1): slots.append(slot)
        cursor += timedelta(days=1)
    return slots


def produce(control: ControlPlane) -> None:
    candidates = control.episodes("idea", 1) or control.episodes("failed", 1) or control.episodes("rejected", 1)
    if not candidates:
        active = sum(len(control.episodes(status)) for status in ("producing", "awaiting_approval", "approved_reserve", "scheduled"))
        if active >= PIPELINE_TARGET:
            control.event("production_idle", active_pipeline=active, target=PIPELINE_TARGET)
            return
        candidates = [control.create_automatic_idea()]
        control.event("automatic_idea_created", candidates[0]["id"], episode_no=candidates[0]["episode_no"])
    episode = candidates[0]
    control.update(episode["id"], {"status": "producing"}, episode["status"])
    try:
        existing_package = episode.get("package") or {}
        if existing_package.get("package_version") == 2:
            package = existing_package
        else:
            package = ollama_story(episode)
            control.update(episode["id"], {
                "title": package["title"], "script": package["script"], "package": package,
            }, "producing")
            control.event(
                "story_checkpointed", episode["id"],
                revision=package["revision"],
                dialogue_words=len(package["script"].split()),
                episode_summary=package["memory_delta"]["episode_summary"],
            )
        deadline = datetime.now(timezone.utc) + timedelta(hours=72)
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp); video = workdir / "episode-master-16x9.mp4"
            package = {
                **package,
                "visual_mode": "open-source-3d-studio",
                "generation_token": f"3D-CC0-{package['revision'][:8].upper()}",
            }
            render_blender_master(package, video, workdir)
            youtube_id = YouTube().upload_private(video, package)
            control.update(episode["id"], {
                "title": package["title"], "script": package["script"], "package": package,
                "preview_youtube_id": youtube_id, "approval_deadline": deadline.isoformat(),
                "status": "awaiting_approval", "rejected_reason": "",
            }, "producing")
            control.event("approval_requested", episode["id"], deadline=deadline.isoformat(), youtube_id=youtube_id)
            try:
                telegram_review(video, episode["id"], package, youtube_id, deadline)
            except Exception as notify_error:
                control.event("telegram_delivery_failed", episode["id"], error=repr(notify_error))
    except Exception as exc:
        control.update(episode["id"], {"status": "failed"}, "producing")
        control.event("production_failed", episode["id"], error=repr(exc))
        raise


def schedule_approved(control: ControlPlane) -> None:
    scheduled = control.episodes("scheduled")
    occupied = {row["planned_at"] for row in scheduled}
    reserves = control.episodes("approved_reserve")
    for slot in next_slots(max(RESERVE_TARGET, len(reserves))):
        iso = slot.astimezone(timezone.utc).isoformat()
        if iso in occupied or not reserves: continue
        episode = reserves.pop(0)
        package = episode.get("package") or {}
        if episode["approved_revision"] != package.get("revision"):
            control.event("revision_mismatch", episode["id"])
            continue
        if package.get("package_version") != 2:
            control.event("legacy_master_blocked", episode["id"])
            continue
        SupabaseMemory().commit_approved_episode(episode)
        control.event(
            "canon_committed", episode["id"],
            universe_slug=package["universe_slug"],
            series_slug=package["series_slug"],
            revision=package["revision"],
        )
        YouTube().schedule(episode["preview_youtube_id"], slot)
        control.update(episode["id"], {"status": "scheduled", "planned_at": iso}, "approved_reserve")
        control.event("youtube_scheduled", episode["id"], planned_at=iso)


def tick(control: ControlPlane) -> None:
    process_telegram_callbacks(control)
    schedule_approved(control)
    now = datetime.now(timezone.utc)
    for episode in control.episodes("awaiting_approval"):
        episode = control.update(episode["id"], {}, "awaiting_approval")
        if not episode["approval_deadline"]: continue
        remaining = (datetime.fromisoformat(episode["approval_deadline"]) - now).total_seconds() / 3600
        for threshold in (24, 6):
            marker = f"approval_reminder_{threshold}h"
            if remaining <= threshold and remaining > 0 and not control.has_event(marker, episode["id"]):
                control.event(marker, episode["id"], remaining_hours=remaining)
                notify(f"Noch {threshold} Stunden: {episode['title']}", f"Prüffassung: https://youtu.be/{episode['preview_youtube_id']}")
        if remaining <= 0 and not control.has_event("approval_overdue", episode["id"]):
            control.event("approval_overdue", episode["id"])
            notify(f"Freigabe verpasst: {episode['title']}", "Die ungeprüfte Folge bleibt privat. Eine freigegebene Reservefolge übernimmt den nächsten freien Termin.")
    for episode in control.episodes("scheduled"):
        if episode["planned_at"] and datetime.fromisoformat(episode["planned_at"]) <= now:
            control.update(episode["id"], {"status": "published", "published_at": now.isoformat()}, "scheduled")
            control.event("published", episode["id"])
    reserve = len(control.episodes("approved_reserve")) + len(control.episodes("scheduled"))
    control.event("heartbeat", reserve=reserve, reserve_target=RESERVE_TARGET)
    if reserve < RESERVE_TARGET:
        control.event("reserve_low", reserve=reserve, reserve_target=RESERVE_TARGET)
        notify("Story-Reserve zu niedrig", f"Aktuell {reserve} von {RESERVE_TARGET} freigegebenen/eingeplanten Folgen. Bitte offene Prüffassungen freigeben.")


def metrics(control: ControlPlane) -> None:
    end = datetime.now(timezone.utc).date(); start = end - timedelta(days=90); youtube = YouTube()
    for episode in control.episodes("published"):
        try: control.metric(episode["id"], youtube.analytics(episode["preview_youtube_id"], start, end))
        except Exception as exc: control.event("analytics_failed", episode["id"], error=repr(exc))


def main() -> None:
    parser = argparse.ArgumentParser(); parser.add_argument("command", choices=["produce", "tick", "metrics"]); args = parser.parse_args()
    CostGuard()
    control = SupabaseControlPlane()
    control.ensure_labels()
    {"produce": produce, "tick": tick, "metrics": metrics}[args.command](control)


if __name__ == "__main__":
    main()
