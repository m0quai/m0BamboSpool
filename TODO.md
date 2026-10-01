# m0BamboSpool – zentrale To-do-Liste

Diese Liste enthält nur noch offene oder ausdrücklich zu verifizierende Aufgaben.
Erledigte Punkte werden entfernt und nicht als erledigt weitergeführt.

## AMSHelper / NFC

- NDEF-URL-Lesen und -Schreiben für NTAG215 abschließen und auf dem Zielgerät testen.
- SPI-Pinbelegung für vier PN532 festlegen.
- PN532-SPI-Pfad zunächst mit einem Reader testen.
- Danach auf vier Reader mit gemeinsamem SPI-Bus und vier CS-Leitungen erweitern.
- Hotspot-Modus zum Öffnen beziehungsweise Anbieten eines WLAN-Hotspots implementieren,
  falls dieser weiterhin benötigt wird.

## m0BamboSpool / AMS-Kommunikation

- Unterschiede zwischen AMS-Status und Spoolman-Zuordnung abschließend behandeln und
  mit realen Druck-/Wechselvorgängen verifizieren.
- Verhalten bei einem durch Bambu zurückgesetzten External-Spool-Zustand abschließend
  korrigieren; dazu gehören Wiederherstellung, Zuordnung und Logging.
- External-Spool-Druckstatus und Zuordnung bei laufenden und beendeten Jobs verifizieren.
- Profil-/PA-Index sowie Filamentwechsel über mehrere reale AMS-Antworten verifizieren.

## Weboberfläche / Verlauf

- Print-History gegen alle gewünschten Zustände, Filter, Sortierungen, Anker-Sprünge und
  UI-Aktionen vollständig testen.
- Automatische Aktualisierung ausschließlich während laufender Druckjobs verifizieren.
- Inventar-Links zu Spool, Hersteller und Filament mit den jeweiligen Spoolman-IDs testen.
- Mehrsprachigkeit auf allen Seiten und bei allen sichtbaren Buttons vollständig prüfen.

## Eigene Verwaltung / Spulenbestand

- Die am 30.09.2026 in der Inventaransicht dargestellten Spulen in die eigene
  Verwaltung übernehmen. Bestehende Spulen-IDs, Filament-/Herstellerdaten,
  Restgewichte und AMS-Zuordnungen erhalten.
- Referenzbestand aus der geprüften Inventaransicht: Spulen #1, #2, #3, #4,
  #9, #10, #11, #12, #13, #14 und #15. Vor der tatsächlichen Übernahme den
  aktuellen Bestand erneut abgleichen; die eigene Verwaltung soll diese
  Spulen weiterführen und keine parallelen Duplikate anlegen.
- Zugeordnete Drucke mit Datum, Verbrauch und Auftrag weiter bei der jeweiligen
  Spule anzeigen. Bei der Übernahme keine Verbrauchsbuchungen erneut ausführen.

## Wartung / Betrieb

- Docker-Log-Leerung beim Recreate dokumentieren und prüfen.
- Versionierung bei Releases konsistent aktualisieren.
