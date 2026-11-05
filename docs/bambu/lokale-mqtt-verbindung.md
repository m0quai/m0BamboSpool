# Lokale Bambu-MQTT-Verbindung

> Einstieg: [START.md](../START.md) · Entscheidungen: [Design](../../DESIGN.md)

m0BamboSpool verbindet sich ausschließlich im lokalen LAN mit dem Drucker.

- MQTT nutzt `PRINTER_IP`, `PRINTER_ID` und `PRINTER_ACCESS_LAN`.
- Benutzername ist `bblp`, Port ist 8883.
- Reports kommen über `device/<SERIAL>/report`, Befehle gehen an
  `device/<SERIAL>/request`.
- Befehle werden als normales JSON über die lokale TLS-Verbindung übertragen.
- Es gibt keine Cloud-Anmeldung, keine Online-Modus-Auswahl und keine Cloud-
  Zertifikatssignierung.
