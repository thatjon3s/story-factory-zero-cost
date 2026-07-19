# Story Factory ohne monatliche Zusatzkosten

Diese Variante benötigt keinen dauerhaft laufenden eigenen Server und keine bezahlte KI-API. Supabase speichert Zustand und Freigaben, GitHub Actions erzeugt und veröffentlicht Videos, GitHub Pages hostet das Dashboard.

## Kostenregel

Die Workflows setzen fest:

```env
MAX_EXTERNAL_MONTHLY_EUR=0
ALLOW_PAID_API=false
```

Der Worker beendet sich sofort, falls ein positiver Kostenrahmen oder ein nicht freigegebener Produktionsanbieter konfiguriert wird. Krea und andere nutzungsabhängig berechnete APIs sind daher im Null-Euro-Pfad technisch gesperrt.

## 1. Supabase-Free-Projekt

1. Ein kostenloses Projekt erstellen.
2. Im SQL Editor den vollständigen Inhalt von `supabase/schema.sql` ausführen.
3. Unter Authentication die eigene E-Mail einmal anmelden.
4. Die eigene User-ID aus Authentication → Users kopieren.
5. Project URL, Publishable Key und Service Role Key aus den API-Einstellungen notieren.
6. Öffentliche Registrierungen nach der eigenen Anmeldung deaktivieren.

Das Schema vergibt explizite Data-API-Rechte und aktiviert RLS auf allen öffentlichen Tabellen. Der Service Role Key darf niemals im Browser oder in `docs/config.js` stehen.

## 2. GitHub-Repository

Für garantierte kostenlose Standard-Runner sollte das Repository öffentlich sein. Der Code enthält keine Zugangsdaten oder privaten Videos.

In Settings → Secrets and variables → Actions folgende Secrets anlegen:

| Secret | Inhalt |
| --- | --- |
| `SUPABASE_URL` | Project URL |
| `SUPABASE_SERVICE_ROLE_KEY` | geheimer Server-Schlüssel |
| `OWNER_USER_ID` | UUID des eigenen Supabase-Nutzers |
| `YOUTUBE_CLIENT_ID` | Google OAuth Client ID |
| `YOUTUBE_CLIENT_SECRET` | Google OAuth Client Secret |
| `YOUTUBE_REFRESH_TOKEN` | einmalig erzeugtes Refresh-Token |
| `TELEGRAM_BOT_TOKEN` | optional |
| `TELEGRAM_CHAT_ID` | optional |

Ohne Telegram erzeugt der Workflow GitHub-Issues für Freigaben und Warnungen. Dafür sind in den Workflows ausschließlich `issues: write` und `contents: read` freigegeben.

## 3. YouTube einmalig verbinden

1. In Google Cloud die YouTube Data API v3 und YouTube Analytics API aktivieren.
2. Einen OAuth-Client vom Typ Desktop erstellen.
3. Client-ID und Client-Secret lokal als Umgebungsvariablen setzen.
4. Einmal ausführen:

```powershell
.venv\Scripts\python zero_cost\youtube_auth.py
```

5. Das ausgegebene Refresh-Token als GitHub Secret speichern.

Prüffassungen werden privat hochgeladen. Erst nach einer revisionsgebundenen Freigabe wird YouTube angewiesen, das Video zum geplanten Zeitpunkt öffentlich zu schalten.

## 4. Dashboard über GitHub Pages

In `docs/config.js` eintragen:

```js
window.STORY_FACTORY_CONFIG = {
  supabaseUrl: "https://PROJEKT.supabase.co",
  publishableKey: "sb_publishable_..."
};
```

Der Publishable Key darf im Browser stehen; der Zugriff wird durch RLS auf den angemeldeten Eigentümer begrenzt. Anschließend in Settings → Pages die Bereitstellung aus dem Ordner `/docs` des Hauptbranches aktivieren.

Die bei Supabase konfigurierte Site URL muss auf die GitHub-Pages-Adresse zeigen, damit Magic Links zurück zum Dashboard führen.

## 5. Betrieb

- `produce.yml` sucht täglich nach genau einer neuen oder abgelehnten Story-Idee. Ist keine vorhanden und die Pipeline enthält weniger als acht aktive Folgen, legt es selbstständig die nächste Serienfolge an.
- Das lokale Modell `qwen3:4b` schreibt die Geschichte.
- Piper erzeugt die deutsche Stimme.
- FFmpeg rendert eine horizontale 1080p-Hörspielfassung mit Untertiteln und Audiowellen.
- YouTube erhält eine private Prüffassung.
- Du bekommst eine Nachricht beziehungsweise ein GitHub-Issue.
- Nach Freigabe plant `control.yml` die Folge automatisch ein.
- Der Kontrolljob läuft alle sechs Stunden und sammelt einmal täglich Kennzahlen.

GitHub-Zeitpläne können sich verspäten. Das beeinträchtigt die Veröffentlichung nicht, weil der eigentliche Veröffentlichungstermin vorab bei YouTube hinterlegt wird.

## Reserve- und Freigaberegel

Die Freigabe ist an den SHA-Hash der geprüften Skriptrevision gebunden. Eine nachträglich veränderte Fassung kann nicht mit einer alten Freigabe veröffentlicht werden.

Erinnerungen erfolgen:

- unmittelbar nach Fertigstellung,
- 24 Stunden vor Ablauf,
- 6 Stunden vor Ablauf,
- nach einer verpassten Freigabe.

Eine ungeprüfte Folge bleibt privat. Der nächste Termin wird ausschließlich mit einer bereits freigegebenen Reservefolge besetzt.

## Optionale bestehende Dienste

Krea API wird nicht automatisch eingesetzt, weil App-Kontingente und API-Guthaben getrennt abgerechnet werden. Canva kann später als optionaler Template-Renderer ergänzt werden, darf jedoch nie der einzige Renderer sein. Der kostenlose FFmpeg-Pfad bleibt immer der Ausfallschutz.

## Einschränkungen

- Kostenlose Dienste geben keine Verfügbarkeitsgarantie.
- Der visuelle Standardstil ist bewusst prozedural und kein generiertes Filmmaterial.
- Lokale Modelle auf CPU können deutlich länger als Cloud-KI benötigen.
- Vor dem öffentlichen Start sollte mindestens eine komplette Folge privat geprüft werden.
- Die kommerzielle Nutzbarkeit verwendeter Musik oder zusätzlicher Assets muss separat sichergestellt werden.
