from __future__ import annotations

import argparse
import hashlib
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

    @staticmethod
    def _body(row: dict[str, Any]) -> str:
        return "## Automationsstatus\n\n```json\n" + json.dumps(row, ensure_ascii=False, indent=2) + "\n```"

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
        rows.sort(key=lambda x: int(x.get("episode_no", 0)))
        return [x for x in rows if not status or x.get("status") == status][:limit]

    def create_automatic_idea(self) -> dict[str, Any]:
        rows = self.episodes(limit=1000)
        episode_no = max((int(row["episode_no"]) for row in rows), default=0) + 1
        series = os.getenv("AUTO_SERIES_NAME", "Die letzte Leitung")
        bible = os.getenv(
            "SERIES_BIBLE",
            "Eine nächtliche Notrufzentrale empfängt unmögliche Anrufe aus anderen Zeiten und Wirklichkeiten. "
            "Jede Folge löst ein eigenes Rätsel teilweise und erweitert zugleich das übergeordnete Geheimnis."
        )
        payload = {
            "owner_id": self.owner_id, "series_name": series, "episode_no": episode_no,
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
        saved = self._request("PATCH", f"issues/{episode_id}", json={"title": f"[Folge {row.get('episode_no')}] {row.get('title')}", "body": self._body(row), "labels": labels})
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
    github_token, repository = os.getenv("GITHUB_TOKEN", ""), os.getenv("GITHUB_REPOSITORY", "")
    if github_token and repository:
        response = httpx.post(
            f"https://api.github.com/repos/{repository}/issues",
            headers={"Authorization": f"Bearer {github_token}", "Accept": "application/vnd.github+json"},
            json={"title": subject[:240], "body": body}, timeout=30,
        )
        response.raise_for_status()


def ollama_story(episode: dict[str, Any]) -> dict[str, Any]:
    CostGuard().require_free("ollama")
    prompt = f"""Du bist Headwriter einer deutschen Mystery-Hörspielserie.
Serie: {episode['series_name']}, Folge {episode['episode_no']}.
Prämisse: {episode['premise'] or episode['title']}.

Schreibe eine originelle fiktionale Folge mit 1.350 bis 1.650 deutschen Wörtern. Sie benötigt einen sofortigen
Cold Open, drei Eskalationen, eine beantwortete Teilfrage und am Ende einen präzisen Cliffhanger. Keine realen
Behauptungen, keine geschützten Figuren, keine Imitation lebender Autoren. Gib ausschließlich JSON zurück:
{{"title":"...","description":"...","thumbnail_text":"maximal 4 Wörter","tags":["..."],"script":"..."}}
"""
    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "thumbnail_text": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "script": {"type": "string"},
        },
        "required": ["title", "description", "thumbnail_text", "tags", "script"],
    }
    payload = {
        "model": os.getenv("OLLAMA_MODEL", "qwen3:4b"), "prompt": prompt, "stream": False,
        "format": schema, "think": False,
        "options": {"num_predict": 6000, "temperature": 0.8},
    }
    failures: list[str] = []
    for attempt in range(3):
        response = httpx.post("http://127.0.0.1:11434/api/generate", json=payload, timeout=900)
        response.raise_for_status()
        try:
            package = json.loads(response.json()["response"])
            words = len(package["script"].split())
            if 1050 <= words <= 2000 and all(package.get(key) for key in schema["required"]):
                revision = hashlib.sha256(package["script"].encode("utf-8")).hexdigest()[:16]
                return {**package, "revision": revision, "generator": payload["model"], "word_count": words}
            failures.append(f"attempt {attempt + 1}: {words} words")
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            failures.append(f"attempt {attempt + 1}: {type(exc).__name__}")
        payload["prompt"] += (
            "\nDie vorige Ausgabe war ungültig oder zu kurz. Liefere mindestens 1.350 Wörter im Feld script, "
            "ohne Vorrede und ohne Markdown; alle fünf JSON-Felder sind Pflicht."
        )
    raise RuntimeError("Local story model failed validation: " + "; ".join(failures))


def paragraphs_to_srt(script: str, destination: Path) -> None:
    chunks = [c.strip() for c in re.split(r"\n+|(?<=[.!?])\s+(?=[A-ZÄÖÜ])", script) if c.strip()]
    words_total = max(1, sum(len(c.split()) for c in chunks))
    cursor = 0.0
    lines: list[str] = []
    for index, chunk in enumerate(chunks, 1):
        duration = max(2.2, len(chunk.split()) / words_total * (words_total / 2.25))
        start, end = cursor, cursor + duration
        def stamp(seconds: float) -> str:
            millis = int(seconds * 1000); hours, rem = divmod(millis, 3_600_000); minutes, rem = divmod(rem, 60_000); secs, ms = divmod(rem, 1000)
            return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"
        lines.extend([str(index), f"{stamp(start)} --> {stamp(end)}", chunk, ""])
        cursor = end
    destination.write_text("\n".join(lines), encoding="utf-8")


def synthesize_voice(script: str, output: Path) -> None:
    CostGuard().require_free("piper")
    command = ["piper", "--model", os.environ["PIPER_MODEL_PATH"], "--output_file", str(output)]
    subprocess.run(command, input=script, text=True, encoding="utf-8", check=True, timeout=1800)


def render_video(package: dict[str, Any], voice: Path, output: Path, workdir: Path) -> None:
    CostGuard().require_free("ffmpeg")
    subtitle = workdir / "subtitles.srt"
    paragraphs_to_srt(package["script"], subtitle)
    escaped = str(subtitle.resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    filters = (
        "[0:v]noise=alls=8:allf=t+u,drawbox=x=110:y=90:w=1700:h=900:color=black@0.18:t=fill[bg];"
        "[1:a]showwaves=s=1450x170:mode=cline:colors=0x6e7dff@0.75:scale=sqrt[wave];"
        f"[bg][wave]overlay=(W-w)/2:760,subtitles='{escaped}':force_style='FontName=DejaVu Sans,FontSize=24,"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H90000000,BorderStyle=3,Outline=1,Shadow=0,MarginV=56'[v]"
    )
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x080b16:s=1920x1080:r=30",
        "-i", str(voice), "-filter_complex", filters, "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-c:a", "aac", "-b:a", "160k",
        "-shortest", "-movflags", "+faststart", str(output)
    ], check=True, timeout=3600)


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
            "title": package["title"][:100], "description": package["description"] + "\n\nFiktionale, KI-unterstützte Geschichte.",
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
        package = ollama_story(episode)
        with tempfile.TemporaryDirectory() as tmp:
            workdir = Path(tmp); voice = workdir / "voice.wav"; video = workdir / "episode.mp4"
            synthesize_voice(package["script"], voice)
            render_video(package, voice, video, workdir)
            youtube_id = YouTube().upload_private(video, package)
        deadline = datetime.now(timezone.utc) + timedelta(hours=72)
        control.update(episode["id"], {
            "title": package["title"], "script": package["script"], "package": package,
            "preview_youtube_id": youtube_id, "approval_deadline": deadline.isoformat(),
            "status": "awaiting_approval", "rejected_reason": "",
        }, "producing")
        control.event("approval_requested", episode["id"], deadline=deadline.isoformat(), youtube_id=youtube_id)
        notify(f"Freigabe benötigt: {package['title']}", f"Prüffassung: https://youtu.be/{youtube_id}\nFrist: {deadline.isoformat()}")
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
        YouTube().schedule(episode["preview_youtube_id"], slot)
        control.update(episode["id"], {"status": "scheduled", "planned_at": iso}, "approved_reserve")
        control.event("youtube_scheduled", episode["id"], planned_at=iso)


def tick(control: ControlPlane) -> None:
    schedule_approved(control)
    now = datetime.now(timezone.utc)
    for episode in control.episodes("awaiting_approval"):
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
    CostGuard(); control = ControlPlane(); control.ensure_labels()
    {"produce": produce, "tick": tick, "metrics": metrics}[args.command](control)


if __name__ == "__main__":
    main()
