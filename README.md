# Story Factory

Serverseitige Produktion fortlaufender fiktionaler YouTube-Serien mit
Telegram-Freigabe, automatischer Terminplanung und hartem Kostenlimit.

Supabase ist die fachliche Grundplattform. Dort liegen Folgen, Status,
Freigaben, Ereignisse, Kanon, Szenen-Queue, Anbieter und Ergebnisse. GitHub
Actions ist lediglich ein kostenloser, zustandsloser Worker und kann später
ohne Datenmigration ersetzt werden.

## Produktionsregeln

- Das kanonische Hauptvideo wird ausschließlich als `1920x1080`-Master in 16:9 produziert.
- Die Handlung besteht aus gespielten Dialogszenen; Erzähler und Voice-over sind verboten.
- Ein Supabase-Studio-Router verteilt Szenen an austauschbare, geprüfte Videostudios.
- Es gibt keinen Standbild- oder Diashow-Fallback. Ein Qualitätsausfall ist ein Produktionsfehler.
- Alte 9:16-/Erzähler-Pakete werden nicht mehr terminiert.
- Shorts sind spätere, nicht-kanonische Teaser und dürfen das Gedächtnis nicht verändern.

## Kanonisches Gedächtnis

Supabase hält drei getrennte Ebenen:

1. gemeinsamen Universums-Kanon,
2. stark gewichteten Kanon jeder Serie,
3. Zusammenfassung und Zustandsänderung jeder freigegebenen Folge.

Das Drehbuch erhält Universumsregeln, Serienfakten und die letzten fünf
freigegebenen Folgen. Eine neue Folge liefert ein `memory_delta`. Dieses wird
erst nach Telegram-Freigabe und vor der YouTube-Terminierung idempotent
kanonisch gespeichert. Zusätzlich bleibt jede Änderung in
`canon_entry_revisions` revisionssicher erhalten.

## Studio-Zulassung

Ein Anbieter kann in `studio_providers` nur aktiviert werden, wenn alle
folgenden Bedingungen erfüllt sind:

- keine zusätzlichen Kosten,
- kommerzielle Nutzung erlaubt,
- Automatisierung erlaubt,
- wasserzeichenfreier Export,
- Dialog, Figurenreferenzen und 16:9 unterstützt,
- Bedingungen mit URL, Prüfdatum und Fingerprint dokumentiert.

Eine Anbieterprüfung verfällt nach 30 Tagen automatisch. Damit kann ein
geänderter Gratisplan nicht unbemerkt weiterverwendet werden.

Die eigentliche Produktion wird als dauerhafte Szenen-Queue in Supabase
gespeichert. Worker reservieren Jobs atomar mit Ablaufzeit. Ausgefallene
Worker verlieren ihre Reservierung automatisch; die Szene wird später erneut
vergeben. Anbieter-Credits und Reset-Zeitpunkte werden zentral gezählt.

## Erforderliche GitHub-Secrets

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- Zugangsdaten ausschließlich für tatsächlich freigegebene Studio-Adapter,
  beispielsweise `KREA_API_TOKEN`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- bestehende YouTube-OAuth-Secrets

Der Supabase-Service-Role-Key gehört ausschließlich in GitHub Secrets und
niemals in Browser-Code oder Repository-Dateien.

## Kostenbremse

Kostenpflichtige Videogenerierung bleibt fest deaktiviert. Die Datenbank
verhindert zusätzlich, dass ein Anbieter mit positiven Zusatzkosten aktiviert
werden kann. Fehlt ein zulässiger Gratisanbieter, bleiben die Szenen in der
Queue und Telegram meldet den Stillstand; es wird kein minderwertiger
Fallback veröffentlicht.

## Datenbank

Das Schema liegt unter `supabase/schema.sql`. Alle Tabellen haben RLS aktiviert,
verweigern `anon` und `authenticated` den Zugriff und sind nur für den
serverseitigen `service_role` zugänglich.
