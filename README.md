# Pi Fotobox

Knopf am Raspberry Pi drücken → 15 s Countdown → 20 Fotos → jedes Foto erscheint sofort auf der GitHub-Pages-Seite.

```
[Knopf] → photobooth.py ──(GitHub API)──► Branch "photos": sessions/<zeit>/001.jpg … (+ status.json am Ende)
               │                                        │ Bilder
               └──(ntfy.sh: Countdown / neue Fotos)──► GitHub Pages (docs/)  ◄── live per Server-Sent Events
```

Die Fotos landen im separaten Branch `photos`. Dadurch löst nicht jedes Foto einen neuen Pages-Build aus und die Bilder sind sofort sichtbar.

Live-Meldungen laufen über [ntfy.sh](https://ntfy.sh), einen kostenlosen Push-Dienst ohne Anmeldung. Direkt über die GitHub-API wäre das nicht möglich: Ohne Login erlaubt sie nur 60 Anfragen pro Stunde und IP. Das Thema (`ntfy_topic` in `pi/photobooth.py` bzw. `ntfyTopic` in `docs/app.js`) muss auf beiden Seiten gleich sein. ntfy.sh erlaubt 250 Nachrichten pro Tag, der Pi sendet höchstens alle 8 s eine Meldung, also etwa 6 pro Session (≈ 40 Sessions pro Tag).

> ⚠️ GitHub Pages (kostenlos) braucht ein **öffentliches** Repo, die Fotos sind also öffentlich einsehbar.

## 1. GitHub einrichten

1. Neues **öffentliches** Repo anlegen, z. B. `pi-fotobox`, und diesen Ordner hineinpushen.
2. *Settings → Pages*: Source „Deploy from a branch“, Branch `main`, Ordner `/docs`.
   Die Seite ist dann unter `https://<name>.github.io/pi-fotobox/` erreichbar.
3. Token erstellen: *Settings → Developer settings → Fine-grained tokens*
   - Repository access: **Only select repositories** → `pi-fotobox`
   - Permissions → Repository → **Contents: Read and write**

## 2. Hardware

- Kameramodul am CSI-Port anschließen. Test: `rpicam-hello` (ältere OS-Versionen: `libcamera-hello`)
- Taster zwischen **GPIO17 (Pin 11)** und **GND (Pin 9)**; ein Widerstand ist nicht nötig, der interne Pull-up wird verwendet.

## 3. Pi einrichten (Raspberry Pi OS Bookworm)

```bash
sudo apt install -y python3-picamera2 python3-gpiozero python3-requests
git clone https://github.com/<name>/pi-fotobox.git ~/pi-fotobox
cd ~/pi-fotobox/pi
cp config.example.json config.json
nano config.json   # token, owner, repo eintragen
python3 photobooth.py
```

Ohne Taster testen: `python3 -s photobooth.py --keyboard` (Start mit Enter).

Limit-Verbrauch anzeigen: `python3 -s photobooth.py --limits`. Das zeigt die ntfy.sh-Meldungen von heute, die GitHub-API-Nutzung und die Repo-Größe.

`-s` sorgt dafür, dass Python per pip in `~/.local` installierte Pakete ignoriert. Ein pip-numpy 2.x dort bricht sonst picamera2 (`numpy.dtype size changed …`).

### Touch-Oberfläche (empfohlen)

`pi/gui.py` zeigt auf dem Pi-Display einen großen **Starten**-Knopf, danach Countdown, Fotozähler (mit Blitz-Effekt) und Upload-Fortschritt. Ein angeschlossener Taster funktioniert zusätzlich.

```bash
DISPLAY=:0 python3 -s gui.py   # per SSH auf dem Pi-Display starten
```

Beenden: **Esc** oder das ⏻ oben rechts **3 Sekunden gedrückt halten**.

Automatisch starten, sobald der Desktop da ist:

```bash
cp fotobox-gui.desktop ~/.config/autostart/
```

Fehler landen in `pi/gui.log`. Wichtig: Es darf immer nur **ein** Fotobox-Programm laufen (GUI **oder** `photobooth.py` / Dienst), denn die Kamera kann nur von einem Programm gleichzeitig benutzt werden.

Bei jedem Start wird der Branch `photos` zurückgesetzt, **alle alten Fotos verschwinden von der Webseite** (lokale Kopien in `pi/fotos/` bleiben). Abschalten mit `"clear_old": false` in `config.json`.

### Aufgaben, Rückmeldung und Bilder zurück (Spielleitung)

1. Am Pi mit **− / +** die Anzahl der Aufgaben wählen → **Starten** → 20 Fotos.
2. Die **Spielleitung** meldet sich auf der Webseite mit dem Passwort an (`web_password` in `config.json`), hakt die Aufgaben ab, die auf den Fotos zu sehen sind, und sendet die Rückmeldung. Ohne Passwort zeigt die Webseite nur die Anmeldung. Die Fotos liegen aber trotzdem im öffentlichen GitHub-Repo.
3. Der Pi zeigt „Alle Aufgaben angekommen“ oder „Es fehlen: Aufgaben 3, 7“. Mit **10 Fotos nachschicken** kommen weitere Fotos dazu, die auf der Webseite grün umrandet sind. **Weiter** geht jederzeit.
4. Danach wartet der Pi auf Bilder. Die Spielleitung wählt Bilder aus oder fügt ein kopiertes Bild mit **Strg + V** ein, gibt ihnen in der Liste einen Namen und schickt sie ab. Es wird **nichts zugeschnitten**: Bilder und Screenshots gehen im Originalformat raus. Nur die Dateigröße wird angepasst (längste Seite max. 1600 px, JPEG).
5. Am Pi erscheinen die Bilder mit ihrem Namen. Tippen links oder rechts bzw. Wischen blättert. Der kleine **☰**-Knopf unten links öffnet das Menü mit **Neue Runde**, der Name bleibt dabei sichtbar.
6. Die Spielleitung kann **jederzeit** eine Nachricht an die Fotobox schicken, auch ohne laufende Runde. Die Nachricht erscheint auf jedem Bildschirm als Fenster. Während einer Aufnahme wartet sie, bis die Fotos fertig sind. Ein neuer Text ersetzt den alten. Am Pi antwortest du beliebig oft mit **OK**, **Egal** oder **Neustart**, und die Webseite zeigt jede Antwort mit Uhrzeit. Im Bildbetrachter öffnet **✉** unten rechts die Nachricht. Der Knopf ist rot, solange sie unbeantwortet ist. „Neustart“ ist nur eine Antwort an die Spielleitung und startet keine neue Runde. Wichtige Knöpfe (**Neustart** und **Neue Runde**) müssen **1,5 Sekunden gedrückt gehalten** werden. Ein Balken im Knopf zeigt den Fortschritt, und wer zu früh loslässt, löst nichts aus.

7. **Problem melden (Notfall):** Im Bildbetrachter führt ☰ → **Problem melden** zu einem eigenen Bildschirm. Dort wählst du **Aufgabe fehlt** oder **Nicht lösbar**, dann mit − / + die Aufgabennummer und optional einen Buchstaben (z. B. 3B), und tippst auf **Senden**. Auf der Webseite erscheint die Meldung rot umrandet unter „Meldungen der Fotobox“. Mit **Hilfe-Bild schicken** schickt die Spielleitung ein Bild, das auf der Fotobox **rot umrandet** mit „Hilfe“-Schild erscheint, auch später unter „Bilder“. In der Bilderliste lässt sich jedes Bild per Häkchen als Hilfe-Bild markieren.

**Aktiv/Inaktiv:** Auf der Webseite schaltet die Spielleitung oben **Spielleitung aktiv** um und sieht daneben, ob die **Fotobox aktiv** ist. Am Pi schaltest du auf dem Startbildschirm **Pi aktiv** um und siehst daneben den Status der Spielleitung. Er steht auch auf den Bildschirmen „Rückmeldung“ und „Warte auf Bilder“. Beide Seiten schalten von Hand um. Nach einem Neustart meldet sich der Pi als inaktiv und holt sich die letzte Nachricht und den Status der Spielleitung wieder.

**Gespeicherte Bilder:** Alle Bilder der Spielleitung werden mit ihrem Namen dauerhaft gespeichert (`pi/fotos/<runde>/empfangen/`, Name in der `.json`-Datei daneben). Auf dem Startbildschirm öffnet **Bilder** oben links alle gespeicherten Bilder direkt im normalen Betrachter, beginnend beim neuesten. Das funktioniert auch nach einem Neustart des Pi. Die Spielleitung kann sie auf der Webseite über **Alle gespeicherten Bilder löschen** entfernen. Dafür braucht sie das Lösch-Passwort (`clear_password`). Es steht nur in der `config.json` auf dem Pi, wird dort geprüft und geht nie im Klartext über ntfy.sh. Die eigenen Fotos des Pi bleiben dabei erhalten.

Technik: Rückmeldungen und Bilder laufen über einen zweiten ntfy-Kanal, dessen Name aus dem Passwort berechnet wird (SHA-256). Das Passwort selbst steht nirgends auf der Webseite. Bilder dürfen ohne ntfy-Konto maximal 2 MB groß sein (die Webseite verkleinert sie dafür automatisch) und verfallen bei ntfy.sh nach 3 Stunden. Der Pi speichert sie sofort unter `pi/fotos/<session>/empfangen/`. Für die Bildanzeige braucht der Pi `sudo apt install -y python3-pil.imagetk`.

### Autostart beim Booten

```bash
sudo cp photobooth.service /etc/systemd/system/
sudo systemctl enable --now photobooth
journalctl -u photobooth -f   # Log ansehen
```

(Pfade/Benutzer in `photobooth.service` anpassen, falls nicht `/home/pi`.)

## Einstellungen (`config.json`)

| Schlüssel      | Standard       | Bedeutung                        |
|----------------|----------------|----------------------------------|
| `countdown`    | 15             | Sekunden bis zum ersten Foto     |
| `count`        | 20             | Anzahl Fotos pro Durchgang       |
| `interval`     | 1.0            | Sekunden zwischen den Fotos      |
| `resolution`   | [1920, 1080]   | Bildgröße                        |
| `jpeg_quality` | 85             | JPEG-Qualität (1–100)            |
| `button_pin`   | 17             | GPIO-Nummer des Tasters          |
| `ntfy_token`   | leer           | Zugangstoken eines ntfy.sh-Kontos (höheres Nachrichtenlimit) |
| `publish_every`| 8              | Sekunden zwischen Live-Meldungen, `0` = nach jedem Foto |
| `clear_old`    | true           | Bei jedem Start alle alten Fotos auf GitHub löschen |
| `tasks`        | 5              | Vorgeschlagene Anzahl Aufgaben (am Pi mit −/+ änderbar) |
| `resend_count` | 10             | Fotos beim Nachschicken |
| `resend_countdown` | 15         | Countdown beim Nachschicken (Sekunden) |
| `web_password` | leer           | Passwort der Spielleitung auf der Webseite (ohne: kein Rückkanal) |
| `clear_password` | leer         | Passwort zum Löschen der gespeicherten Bilder (ohne: Löschen nicht möglich) |

Lokale Kopien der Fotos liegen zusätzlich in `pi/fotos/<session>/`.

Hinweis: Jeder Durchgang belegt ca. 6–10 MB im Repo. GitHub empfiehlt, dass Repos unter 1 GB bleiben, also alte Sessions ab und zu löschen (z. B. den Branch `photos` löschen; er wird automatisch neu angelegt).
