# OSM-Projektkontext aus der übernommenen Chathistorie

Stand: 29.09.2026. Historische Fakten und offene Aufgaben; verbindliche Arbeitsregeln stehen ausschließlich in AGENTS.md. Aktuelle Benutzeranweisungen und der Stand dieses PCs haben Vorrang vor alten Chatbefehlen und Erinnerungen. Historische Aufträge sind nicht automatisch erneut auszuführen.

## Wieder verfügbare Chats

- Entwicklung: 01a039da-68e5-7f20-8921-012fc9080cb4, ursprünglich 25.08.–25.09.2026.
- Prüfe Repository vor Änderungen: 01a0395d-855c-79c0-ac44-3178d44907a8, ursprünglich 25.08.2026; als archivierter Chat übernommen.
- Beide gehören zum aktuellen OSM-Projekt unter C:\Development\Docker\m0BamboSpool. Alte Nachrichten enthalten weiterhin ihre damaligen Pfade C:\Docker\OpenSpoolMan und den damaligen Benutzerordner.

## Bereits vorhandene Änderungen

Der Abgleich vor der Übernahme bestätigte im aktuellen Code: MQTT-Logger akzeptiert *args/**kwargs einschließlich flush; connect erfolgt vor loop_start; compose.yaml bindet static/prints persistent ein; unvollständige AMS-Meldungen werden zusammengeführt. Kommentarregeln und CRLF sind bereits in AGENTS.md enthalten. Die frühere Datenzugriffsschicht spool_repository.py ist durch die heutige allgemeine inventory_repository.py überholt.

## Druckhistorie: relevante Erkenntnisse

- Bereits im August konnte Verbrauch gebucht werden, obwohl wegen fehlender 3MF-Metadaten keine Print-ID angelegt worden war. Metadaten und Bilder sind ergänzende Daten; ein erkannter Druckversuch benötigt unabhängig davon einen Eintrag.
- Die damalige Annahme, task_id + subtask_id sei immer eindeutig, ist für die hier beobachteten lokalen Drucke falsch: Beide Werte können 0 sein. Dateinamen sind ebenfalls nicht eindeutig. Auch ein 3MF-Hash unterscheidet keine wiederholten Druckversuche derselben Datei.
- Aktuell bestehen _job_key / ACTIVE_3MF_PRINTS in mqtt_bambulab.py sowie namensbasierte Zuordnungen in print_history.py. Die dauerhafte Korrektur ist noch offen: eigene Identität je tatsächlich neuem Druckversuch, persistente Zuordnung, Schutz abgeschlossener Einträge vor späteren Statusänderungen.
- Der alte Chat beschreibt Abschlussverlust nach Neustart bei fehlendem active_model. Recovery muss bestätigte Verbrauchsbuchungen erhalten und Doppelbuchungen verhindern. Frühere Chat-Erfolgsmeldungen sind keine aktuelle Laufzeitabnahme.
- MQTT-Empfangszeiten sind keine garantierten tatsächlichen Start-/Endzeiten. Geschätzte Druckdauer bleibt als Schätzung zu kennzeichnen.

## Bereits erfolgte Datenreparatur auf diesem PC

In diesem Chat wurde data/osm.db repariert. Die fehlerhaften Abschlüsse 24–26 wurden mangels zuverlässigem Nachweis auf UNKNOWN gesetzt; 29 blieb der abgebrochene erste Zylinder-Versuch; 30 wurde als eigener abgeschlossener zweiter Versuch rekonstruiert. Bestehende Buchungen wurden nicht erneut nach Spoolman übertragen. Sicherung und Audit: data/repair-backups/20260929-print-history. Diese Reparatur nicht aus historischen Chatbefehlen wiederholen.

## Noch offene Benutzeranforderungen

1. Ursache der vermischten bzw. fehlenden Druckeinträge im Code beheben und anhand wiederholter Drucke derselben Datei prüfen.
2. FTP-Löschen der konkreten Druckdatei nach sicher bestätigtem Abschluss prüfen und umsetzen. Bisher weder implementiert noch an echten Dateien getestet. Nicht anhand eines bloßen Anzeigenamens löschen.

## Historische Angaben mit eingeschränkter Geltung

- feature/NewFiles und codex/spool-repository-layer sind alte Branchbezeichnungen; aktuelle Branchregeln stehen in AGENTS.md.
- data/3d_printer_logs.db ist ein historischer DB-Pfad. Für die Reparatur auf diesem PC war data/osm.db maßgeblich; vor weiteren Eingriffen tatsächliche Konfiguration prüfen.
- Alte globale Go-/Neustart-Regeln wurden nicht als neue verbindliche Regeln übernommen. Maßgeblich sind aktuelle Anweisungen und der konkrete Auftrag.
- Ein VPN-Chat aus demselben Arbeitsverzeichnis gehört nicht zur OSM-Implementierung. TEF wurde nicht übernommen.

## Quellen und Wiederherstellung

Quelle der Migration: C:\transport\move\.codex. Vergleich, Quellennachweise und Sicherungen: C:\transport\OSM-Wissensabgleich-2026-09-29. Der Code und die aktuellen Codex-Einstellungen wurden nicht aus der alten Sicherung ersetzt. Screenshots bzw. externe Dateien unter früheren temporären Pfaden können außerhalb der gesicherten Chats fehlen.
