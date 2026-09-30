# NFC-/AMS-/Spoolman-Architektur

> Einstieg: [START.md](../START.md) · Entscheidungen: [Design](../../DESIGN.md)

Die NFC-Lösung verbindet Bambu-AMS-Slots über ESP32-S3 und NTAG215 mit m0BamboSpool/Spoolman.

## Festgelegtes Modell

- Controller: ESP32-S3, C#/.NET nanoFramework.
- Zieltag: NTAG215.
- Hardware-UID ist die stabile Identität.
- Bei einem neuen/leeren Tag wird NDEF geprüft und einmalig eine URL mit unveränderlicher UID geschrieben, z. B. `/nfc/<UID>`.
- UID und URL bleiben bei späterer Wiederverwendung unverändert.
- UID→Spule-Zuordnung liegt ausschließlich serverseitig in m0BamboSpool/Spoolman.
- Das iPhone öffnet die URL direkt vom NFC-Tag.
- AMSHelper meldet NFC-UID und AMS-Slot sowie den Zustand „Tray leer“.
- AMSHelper führt keine Spool-ID-Auflösung durch.

**Designregel:** Tag-UID und Tag-URL bleiben unverändert; die Spulenzuordnung ist serverseitig veränderbar.
