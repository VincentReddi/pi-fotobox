# Pi Fotobox

Knopf am Raspberry Pi drücken → 15 s Countdown → 30 Fotos → jedes Foto erscheint sofort auf der GitHub-Pages-Seite.

```
[Knopf] → photobooth.py ──(GitHub API)──► Branch "photos": sessions/<zeit>/001.jpg … (+ status.json am Ende)
               │                                        │ Bilder
               └──(ntfy.sh: Countdown / neue Fotos)──► GitHub Pages (docs/)  ◄── live per Server-Sent Events
```

Die Fotos landen im separaten Branch `photos`. Dadurch löst nicht jedes Foto einen neuen Pages-Build aus und die Bilder sind sofort sichtbar.

Live-Meldungen laufen über [ntfy.sh](https://ntfy.sh), einen kostenlosen Push-Dienst ohne Anmeldung. Direkt über die GitHub-API wäre das nicht möglich: Ohne Login erlaubt sie nur 60 Anfragen pro Stunde und IP. Das Thema (`ntfy_topic` in `pi/photobooth.py` bzw. `ntfyTopic` in `docs/app.js`) muss auf beiden Seiten gleich sein. ntfy.sh erlaubt 250 Nachrichten pro Tag, der Pi sendet pro Session ungefähr 10.

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

`-s` sorgt dafür, dass Python per pip in `~/.local` installierte Pakete ignoriert. Ein pip-numpy 2.x dort bricht sonst picamera2 (`numpy.dtype size changed …`).

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
| `count`        | 30             | Anzahl Fotos pro Durchgang       |
| `interval`     | 1.0            | Sekunden zwischen den Fotos      |
| `resolution`   | [1920, 1080]   | Bildgröße                        |
| `jpeg_quality` | 85             | JPEG-Qualität (1–100)            |
| `button_pin`   | 17             | GPIO-Nummer des Tasters          |

Lokale Kopien der Fotos liegen zusätzlich in `pi/fotos/<session>/`.

Hinweis: Jeder Durchgang belegt ca. 10–15 MB im Repo. GitHub empfiehlt, dass Repos unter 1 GB bleiben, also alte Sessions ab und zu löschen (z. B. den Branch `photos` löschen; er wird automatisch neu angelegt).
