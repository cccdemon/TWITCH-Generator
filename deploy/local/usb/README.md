# ClipBox USB-Installer

Ein Ventoy-Stick mit dem offiziellen Ubuntu-Server-ISO und einer Autoinstall-Datei.
Nach dem Booten musst du **genau zwei Dinge** tun: die Platte wählen und einmal bestätigen.
Danach läuft alles von selbst, inklusive Modell-Download. Am Ende steht die Web-UI unter
`http://<IP>:9443` und das Passwort auf der Konsole.

> ## ⚠️ Platte 2 wird vollständig gelöscht
> Der Installer formatiert die Platte, die du im Dialog auswählst. Beide SSDs sind 256 GB groß
> und sehen im Installer fast gleich aus. **Sichere vorher alle Daten von Platte 2.**
> Windows (Platte 1) bleibt unberührt, solange du im Dialog die richtige Platte wählst.
> Woran du sie erkennst, steht in Schritt 5.

## Du brauchst

- Windows-PC (zum Stick-Bauen), USB-Stick ≥ 8 GB (wird gelöscht).
- [Ventoy](https://www.ventoy.net/en/download.html) für Windows (`ventoy-x.x.xx-windows.zip`).
- Ubuntu Server ISO: **`ubuntu-24.04.4-live-server-amd64.iso`** (oder neueres 24.04.x) von
  <https://releases.ubuntu.com/24.04/>. Mindestens 24.04.3; die 24.04.4 bringt Kernel 6.17,
  den die RX 9070 XT (RDNA4) braucht. Ältere 24.04-ISOs booten mit schwarzem Bild.
- Diesen Ordner (`deploy/local/usb/`) aus dem Repo, Branch `clipbox`.
- Netzwerkkabel am Ziel-PC (DHCP). WLAN wird nicht eingerichtet.

## 1. Stick mit Ventoy vorbereiten

1. Ventoy-ZIP entpacken, `Ventoy2Disk.exe` starten.
2. Stick auswählen, **Option → Partition Style → GPT**, **Option → Secure Boot Support** anhaken.
3. **Install**. Zweimal bestätigen. Der Stick hat danach eine große Datenpartition (Laufwerksbuchstabe erscheint).

## 2. Dateien auf den Stick

Entweder mit dem Skript (aus diesem Ordner, PowerShell):

```powershell
.\build-usb.ps1 -Drive E: -Iso "C:\Downloads\ubuntu-24.04.4-live-server-amd64.iso"
```

Das Skript kopiert das ISO und die Ordner `ventoy\` und `autoinstall\` auf den Stick, fragt ein
Passwort für den Linux-Benutzer `clip` ab, erzeugt den Hash und trägt ihn in `user-data` ein.
Für den Hash braucht es `openssl.exe` (kommt mit Git for Windows) oder WSL; sonst siehe Schritt 2b.

Oder von Hand:

1. ISO in das **Wurzelverzeichnis** des Sticks kopieren. Dateiname beibehalten; steht er anders,
   den Pfad in `ventoy\ventoy.json` → `"image"` anpassen.
2. Ordner `ventoy\` (mit `ventoy.json`) und `autoinstall\` (mit `user-data`) ins Wurzelverzeichnis kopieren.
   Ergebnis:
   ```
   E:\ubuntu-24.04.4-live-server-amd64.iso
   E:\ventoy\ventoy.json
   E:\autoinstall\user-data
   ```
3. **2b. Passwort-Hash** erzeugen und in `E:\autoinstall\user-data` statt `REPLACE_WITH_PASSWORD_HASH` eintragen
   (Anführungszeichen bleiben):
   - Git-Bash / WSL / Linux: `openssl passwd -6` → Passwort eingeben, Ausgabe beginnt mit `$6$`.
   - PowerShell mit Git for Windows: `& "C:\Program Files\Git\usr\bin\openssl.exe" passwd -6`
   - Kein openssl? `python -c "import crypt,getpass;print(crypt.crypt(getpass.getpass(),crypt.mksalt(crypt.METHOD_SHA512)))"` unter Linux/WSL.
4. Optional in `user-data` den Kopf prüfen: Repo-URL und Branch (`clipbox`) müssen stimmen, sonst klont der Installer ins Leere.

## 3. BIOS / Secure Boot am Ziel-PC

- Boot-Menü (meist `F11`/`F12`/`Del`) → vom USB-Stick booten (UEFI-Eintrag, nicht „Legacy").
- **Secure Boot aktiv?** Ventoy zeigt beim ersten Start einen blauen MOK-Dialog („Verification failed"):
  `OK` → `Enroll key from disk` → `VTOYEFI` → `ENROLL_THIS_KEY_IN_MOKMANAGER.cer` → `Continue` → `Yes` → `Reboot`.
  Danach erneut vom Stick booten. Alternative: Secure Boot im BIOS ausschalten (Windows 11 startet trotzdem,
  nur ohne Secure-Boot-Status).
- Kein BitLocker auf der Windows-Platte (Voraussetzung, sonst fragt Windows nach dem Dual-Boot nach dem Key).

## 4. Booten und Autoinstall starten

1. Ventoy-Menü: Eintrag `ubuntu-24.04.4-live-server-amd64.iso` wählen (ist nach 10 s automatisch gewählt).
2. Ventoy fragt „Boot with template / without": **erster Eintrag** (mit `/autoinstall/user-data`), auch
   automatisch nach 10 s.
3. GRUB des Ubuntu-ISO: `Try or Install Ubuntu Server`.
4. Der Installer startet, lädt die Autoinstall-Datei und fragt einmal:
   **„Continue with autoinstall? (yes|no)"** → `yes` + Enter. (Das ist die eine Bestätigung.)

## 5. Platte wählen (der einzige interaktive Schritt)

Der Installer springt direkt zu **Guided storage configuration**.

1. `Use an entire disk` ist vorausgewählt. Im Dropdown steht eine Platte. Mit **Pfeiltasten/Enter**
   die Liste öffnen: es erscheinen zwei Einträge, beide ≈ 238 GiB.
2. **Richtige Platte erkennen:** Die Zeile zeigt Hersteller/Modell und bei NVMe den Pfad (`nvme0n1`, `nvme1n1`).
   - Wenn beide SSDs unterschiedliche Modelle sind: das Modell der Datenplatte wählen.
   - Wenn gleich: Zurück (`Back`) zur Übersicht ist nicht nötig; im nächsten Bildschirm
     (**Storage configuration**, Zusammenfassung) steht unter `AVAILABLE DEVICES` / `USED DEVICES`
     für jede Platte, welche Partitionen sie **jetzt** hat. Die Windows-Platte zeigt eine kleine
     `Microsoft reserved`/`EFI`-Partition und eine große `ntfs`-Partition mit Windows. **Die darf es nicht sein.**
     Falls die falsche gewählt ist: `Back`, andere Platte nehmen.
3. **`Set up this disk as an LVM group` abhaken** (kein Haken). Keine Verschlüsselung.
4. `Done` → Zusammenfassung → `Done` → Rückfrage **„Confirm destructive action"** → `Continue`.
   Ab hier wird Platte 2 gelöscht.

Alles weitere (Benutzer, Pakete, Kernel, GRUB, Repo-Klon) läuft ohne Eingaben. Dauer ≈ 10–15 min je nach Leitung.
Am Ende: `Reboot Now` → Enter, Stick abziehen wenn gefragt.

## 6. Erster Boot

- GRUB-Menü: **Ubuntu** (Standard, 10 s) und **Windows Boot Manager**. Startet stattdessen direkt Windows,
  im BIOS die Boot-Reihenfolge auf `ubuntu` stellen (der Installer trägt den UEFI-Eintrag ein, manche
  Boards ignorieren die Reihenfolge).
- Ubuntu bootet zur Konsole. Der Bootstrap läuft im Hintergrund: baut das App-Image, lädt die
  Ollama- und whisper-Images und ≈ 15 GB Modelle. **Dauer 20–60 min**, je nach Leitung.
  Fortschritt: einloggen (`clip` + dein Passwort), dann
  ```bash
  journalctl -u clipbox-bootstrap -f
  ```
- Fertig, wenn beim Login steht: `ClipBox is up.  Web UI: http://192.168.x.y:9443`.
  Das Web-Passwort steht in `/opt/twitch-generator/FIRST-LOGIN.txt` (`cat` darauf) und in der gleichen Meldung.
- Browser am anderen Rechner: `http://192.168.x.y:9443`, Login `clip` + Web-Passwort (nicht das Linux-Passwort).

Schlägt der Bootstrap fehl, steht der Grund beim Login (`/etc/motd`); der nächste Neustart versucht es
erneut. Typische Ursachen und Befehle: [../README.md](../README.md).

## 7. Zweiter Boot ohne Stick

Alle Container haben `restart: unless-stopped`: nach einem Neustart laufen `ollama`, `whisper`, `app`
von selbst wieder. Nichts zu tun.

## Sicherheit

- UI nur im LAN, HTTP ohne TLS. Kein Port nach außen freigeben.
- SSH ist an (Passwort-Login). Linux-Passwort = das aus dem Hash; Web-Passwort = generiert.
- `FIRST-LOGIN.txt` nach dem ersten Login löschen oder das Passwort in `.env` (`USERS=`) ändern.
