#!/usr/bin/env python3
"""Fotobox für den Raspberry Pi.

Knopf drücken -> 15 s Countdown -> 20 Fotos mit dem Kameramodul ->
jedes Foto wird sofort in den Branch "photos" des GitHub-Repos hochgeladen.
Live-Updates (Countdown, neue Fotos) gehen über ntfy.sh an die GitHub-Pages-Seite;
status.json im Branch "photos" hält die letzte fertige Session für spätere Besucher fest.
Über einen zweiten, passwortgeschützten ntfy-Kanal (Backchannel) schickt die Webseite
Rückmeldungen zu den Aufgaben und Bilder an den Pi zurück.
"""
import argparse
import base64
import hashlib
import hmac
import json
import queue
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
API = "https://api.github.com"
STATUS_PATH = "status.json"
NTFY = "https://ntfy.sh"

DEFAULTS = {
    "branch": "photos",
    "button_pin": 17,
    "countdown": 15,
    "count": 20,
    "interval": 1.0,
    "resolution": [1920, 1080],
    "jpeg_quality": 85,
    "ntfy_topic": "pi-fotobox-f5849b5b30bccc45",  # muss zu docs/app.js passen
    "ntfy_token": "",  # leer = anonym (250 Nachrichten/Tag), sonst Limit des ntfy.sh-Kontos
    "publish_every": 8,  # s zwischen Live-Meldungen; 0 = nach jedem Foto
    "clear_old": True,  # bei jedem Start alle alten Fotos auf GitHub löschen
    "tasks": 5,  # vorgeschlagene Anzahl Aufgaben (am Pi mit −/+ änderbar)
    "resend_count": 10,  # Fotos beim Nachschicken
    "resend_countdown": 15,  # Countdown beim Nachschicken
    "web_password": "",  # Passwort der Spielleitung auf der Webseite (Rückkanal)
    # Passwort, mit dem die Spielleitung die gespeicherten Bilder am Pi löschen darf.
    # Nur in config.json eintragen – diese Datei landet nicht im (öffentlichen) Repo.
    "clear_password": "",
}


def load_config():
    path = BASE / "config.json"
    if not path.exists():
        raise SystemExit("config.json fehlt – kopiere config.example.json nach config.json und trage Token/Repo ein.")
    with open(path, encoding="utf-8") as f:
        cfg = {**DEFAULTS, **json.load(f)}
    for key in ("token", "owner", "repo"):
        if not cfg.get(key):
            raise SystemExit(f"config.json: '{key}' ist nicht gesetzt.")
    return cfg


def check(r):
    """Wie raise_for_status(), aber mit der Fehlermeldung von GitHub."""
    if r.ok:
        return
    try:
        msg = r.json().get("message", "")
    except ValueError:
        msg = r.text[:200]
    hint = ""
    if r.status_code in (401, 403):
        hint = "\n  → Token prüfen: Zugriff auf dieses Repo und 'Contents: Read and write'?"
    raise requests.HTTPError(f"GitHub {r.status_code}: {msg} ({r.request.method} {r.url}){hint}", response=r)


class GitHub:
    """Minimaler Client für die GitHub Contents API."""

    def __init__(self, token, owner, repo, branch):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        self.repo_url = f"{API}/repos/{owner}/{repo}"
        self.branch = branch

    def ensure_branch(self):
        """Legt den Foto-Branch an, falls es ihn noch nicht gibt."""
        r = self.s.get(f"{self.repo_url}/git/ref/heads/{self.branch}", timeout=20)
        if r.status_code == 200:
            return
        if r.status_code != 404:
            check(r)
        r = self.s.post(f"{self.repo_url}/git/refs", timeout=20, json={
            "ref": f"refs/heads/{self.branch}",
            "sha": self._default_head(),
        })
        check(r)
        print(f"Branch '{self.branch}' angelegt.")

    def reset_branch(self):
        """Setzt den Foto-Branch auf den Stand des Hauptbranches zurück – alle alten Fotos verschwinden."""
        r = self.s.patch(f"{self.repo_url}/git/refs/heads/{self.branch}", timeout=20,
                         json={"sha": self._default_head(), "force": True})
        if r.status_code == 422:  # Branch existiert (noch) nicht
            self.ensure_branch()
            return
        check(r)

    def _default_head(self):
        repo = self.s.get(self.repo_url, timeout=20)
        check(repo)
        default = repo.json()["default_branch"]
        ref = self.s.get(f"{self.repo_url}/git/ref/heads/{default}", timeout=20)
        check(ref)
        return ref.json()["object"]["sha"]

    def get_sha(self, path):
        r = self.s.get(f"{self.repo_url}/contents/{path}", params={"ref": self.branch}, timeout=20)
        if r.status_code == 404:
            return None
        check(r)
        return r.json()["sha"]

    def put(self, path, data, message, sha=None):
        body = {
            "message": message,
            "content": base64.b64encode(data).decode("ascii"),
            "branch": self.branch,
        }
        if sha:
            body["sha"] = sha
        r = self.s.put(f"{self.repo_url}/contents/{path}", json=body, timeout=60)
        check(r)
        return r.json()["content"]["sha"]


def ntfy_headers(cfg):
    return {"Authorization": f"Bearer {cfg['ntfy_token']}"} if cfg["ntfy_token"] else {}


class Uploader(threading.Thread):
    """Arbeitet Uploads nacheinander ab, damit die Aufnahme nie auf das Netzwerk warten muss.

    Nur dieser Thread verändert self.status – dadurch keine Race Conditions.
    """

    def __init__(self, gh, cfg):
        super().__init__(daemon=True)
        self.gh = gh
        self.topic = cfg["ntfy_topic"]
        self.ntfy_headers = ntfy_headers(cfg)
        self.publish_every = cfg["publish_every"]
        self.clear_old = cfg["clear_old"]
        self.jobs = queue.Queue()
        self.status = {"state": "idle", "photos": []}
        self.status_sha = gh.get_sha(STATUS_PATH)
        self.last_publish = 0.0
        self.notify = lambda *event: None  # wird von run_batch gesetzt (z. B. für die GUI)

    def submit(self, fn, *args):
        self.jobs.put((fn, args))

    def run(self):
        while True:
            fn, args = self.jobs.get()
            for attempt in range(1, 6):
                try:
                    fn(*args)
                    break
                except requests.RequestException as e:
                    print(f"  Upload-Fehler (Versuch {attempt}/5): {e}")
                    time.sleep(2 * attempt)
            else:
                print("  Upload endgültig fehlgeschlagen – übersprungen.")
            self.jobs.task_done()

    def write_status(self):
        data = json.dumps(self.status, indent=2).encode("utf-8")
        try:
            self.status_sha = self.gh.put(STATUS_PATH, data, "Status aktualisiert", self.status_sha)
        except requests.HTTPError as e:
            # SHA veraltet (z. B. Datei von Hand geändert) -> neu holen und nochmal
            if e.response is not None and e.response.status_code in (409, 422):
                self.status_sha = self.gh.get_sha(STATUS_PATH)
                self.status_sha = self.gh.put(STATUS_PATH, data, "Status aktualisiert", self.status_sha)
            else:
                raise

    def publish(self, force=False):
        """Schickt den Status live an die Webseite (gedrosselt, Fehler sind nicht fatal)."""
        now = time.monotonic()
        if not force and now - self.last_publish < self.publish_every:
            return
        self.last_publish = now
        data = json.dumps(self.status).encode("utf-8")
        for attempt in range(3 if force else 1):  # Start/Ende sind wichtig -> wiederholen
            try:
                r = requests.post(f"{NTFY}/{self.topic}", data=data, headers=self.ntfy_headers, timeout=10)
                r.raise_for_status()
                return
            except requests.RequestException as e:
                print(f"  Live-Update fehlgeschlagen: {e}")
                time.sleep(1)

    def start_session(self, session, tasks, total, countdown_end):
        self.status = {
            "session": session,
            "state": "countdown",
            "countdown_end": int(countdown_end * 1000),
            "total": total,
            "tasks": tasks,
            "batch_starts": [0],  # Foto-Nummern, nach denen eine Nachschub-Serie beginnt
            "review": None,  # Rückmeldung der Spielleitung, z. B. {"missing": [5]}
            "photos": [],
        }
        self.publish(force=True)
        if self.clear_old:
            self.gh.reset_branch()  # alte Fotos von der Webseite entfernen
            self.status_sha = None
            print("  Alte Fotos gelöscht.")

    def start_batch(self, first_number, total, countdown_end):
        """Nachschub-Serie in derselben Runde (Fotos werden auf der Webseite grün umrandet)."""
        self.status.update(state="countdown", countdown_end=int(countdown_end * 1000), total=total, review=None)
        self.status["batch_starts"].append(first_number)
        self.publish(force=True)

    def set_review(self, review):
        self.status["review"] = review
        self.publish(force=True)

    def set_state(self, state):
        self.status["state"] = state
        self.publish(force=True)

    def add_photo(self, local, remote):
        try:
            self.gh.put(remote, local.read_bytes(), f"Foto {remote}")
        except requests.HTTPError as e:
            # 422 = Datei existiert schon (vorheriger Versuch kam doch an)
            if e.response is None or e.response.status_code != 422:
                raise
        photos = self.status["photos"]
        if remote not in photos:
            photos.append(remote)
        self.status["state"] = "capturing"
        self.publish()
        print(f"  ↑ {remote} hochgeladen ({len(photos)}/{self.status['total']})")
        self.notify("uploaded", len(photos), self.status["total"])

    def finish_batch(self):
        self.status["state"] = "confirm"  # Pi wartet jetzt auf die Rückmeldung zu den Aufgaben
        self.publish(force=True)
        self.write_status()
        self.notify("done", len(self.status["photos"]), self.status["total"])


def open_camera(cfg):
    from picamera2 import Picamera2

    cam = Picamera2()
    cam.configure(cam.create_still_configuration(main={"size": tuple(cfg["resolution"])}))
    cam.options["quality"] = cfg["jpeg_quality"]
    cam.start()
    if "AfMode" in cam.camera_controls:  # Camera Module 3: Dauer-Autofokus
        from libcamera import controls
        cam.set_controls({"AfMode": controls.AfModeEnum.Continuous})
    time.sleep(2)  # Belichtung/Weißabgleich/Fokus einpendeln lassen
    return cam


class Session:
    """Eine Runde: Aufgaben-Anzahl + alle Fotos (erste Serie und Nachschub)."""

    def __init__(self, tasks):
        self.id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.tasks = tasks
        self.captured = 0  # bisher aufgenommene Fotos (für die fortlaufende Nummerierung)


def run_batch(cam, up, cfg, session, count, countdown, notify=lambda *event: None):
    """Eine Foto-Serie. Die erste Serie einer Session startet die Runde (löscht alte Fotos),
    weitere Serien werden angehängt. notify(event, ...) meldet den Fortschritt, z. B. an die GUI:
    ("countdown", s), ("photo", n, count), ("uploading", n, total), ("uploaded", n, total), ("done", n, total)."""
    up.notify = notify
    first = session.captured == 0
    total = session.captured + count
    countdown_end = time.time() + countdown
    if first:
        up.submit(up.start_session, session.id, session.tasks, total, countdown_end)
    else:
        up.submit(up.start_batch, session.captured, total, countdown_end)

    print(f"\nSession {session.id}: {'Serie' if first else 'Nachschub'} mit {count} Fotos, Countdown {countdown} s")
    remaining = countdown
    while remaining > 0:
        print(f"  {remaining} …", flush=True)
        notify("countdown", remaining)
        time.sleep(max(0, countdown_end - remaining + 1 - time.time()))
        remaining -= 1

    local_dir = BASE / "fotos" / session.id
    local_dir.mkdir(parents=True, exist_ok=True)
    next_shot = time.monotonic()
    for i in range(1, count + 1):
        session.captured += 1
        name = f"{session.captured:03d}.jpg"
        local = local_dir / name
        cam.capture_file(str(local))
        print(f"  📸 Foto {i}/{count}")
        notify("photo", i, count)
        up.submit(up.add_photo, local, f"sessions/{session.id}/{name}")
        next_shot += cfg["interval"]
        time.sleep(max(0, next_shot - time.monotonic()))

    print("  Aufnahme fertig, warte auf restliche Uploads …")
    notify("uploading", len(up.status["photos"]), total)
    up.submit(up.finish_batch)
    up.jobs.join()
    print("  Alles hochgeladen ✔")


def back_topic(cfg):
    """Rückkanal-Thema: aus dem Passwort abgeleitet, damit es nicht im Quelltext der Webseite steht.
    Muss exakt zu deriveBackTopic() in docs/app.js passen."""
    password = cfg["web_password"].strip()
    if not password:
        return None
    digest = hashlib.sha256(f"{password}:{cfg['ntfy_topic']}".encode("utf-8")).hexdigest()
    return f"{cfg['ntfy_topic']}-r-{digest[:24]}"


REPLIES = ("OK", "Egal", "Neustart")  # Antworten des Pi auf eine Nachricht der Spielleitung


class Backchannel(threading.Thread):
    """Empfängt Rückmeldungen, Bilder und Nachrichten der Spielleitung von der Webseite (über ntfy.sh)
    und schickt die Antworten des Pi zurück.

    on_event("review", {"missing": [...]}), on_event("image", pfad, überschrift, upload_id, hilfe) und
    on_event("message", {"id": ..., "text": ...}) – leerer Text = Nachricht entfernt.
    Angenommen wird nur, was zur aktuellen Runde (self.session) gehört.
    """

    PROBLEM_KINDS = ("missing", "unsolvable")  # "Aufgabe fehlt" / "Aufgabe nicht lösbar"

    MAX_IMAGE_BYTES = 3_000_000

    def __init__(self, cfg, on_event):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.topic = back_topic(cfg)
        self.on_event = on_event
        self.session = None
        self.since = str(int(time.time()))  # nur Neues; nach Verbindungsabbrüchen ab der letzten Nachricht

    def hello(self):
        """Die Webseite prüft das Passwort, indem sie diese Nachricht im Rückkanal sucht."""
        try:
            r = requests.post(f"{NTFY}/{self.topic}", data=json.dumps({"type": "hello"}),
                              headers=ntfy_headers(self.cfg), timeout=10)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"Rückkanal: Hallo fehlgeschlagen ({e})")

    def publish(self, payload):
        """Nachricht vom Pi an die Webseite; True = angekommen."""
        try:
            r = requests.post(f"{NTFY}/{self.topic}", data=json.dumps(payload),
                              headers=ntfy_headers(self.cfg), timeout=10)
            r.raise_for_status()
            return True
        except requests.RequestException as e:
            print(f"Rückkanal: Senden fehlgeschlagen ({e})")
            return False

    def reply(self, message_id, answer):
        """Antwort (OK / Egal / Neustart) auf die aktuelle Nachricht; die Webseite zeigt sie an."""
        return self.publish({"type": "reply", "session": self.session, "message_id": message_id,
                             "answer": answer, "at": int(time.time() * 1000)})

    def report(self, kind, task, letter=""):
        """Notfall-Meldung an die Spielleitung: Aufgabe fehlt / ist nicht lösbar (z. B. Aufgabe 3B)."""
        assert kind in self.PROBLEM_KINDS
        return self.publish({"type": "problem", "session": self.session, "id": f"{time.time():.3f}",
                             "kind": kind, "task": int(task), "letter": letter, "at": int(time.time() * 1000)})

    def presence(self, active):
        """Pi aktiv / inaktiv – die Webseite zeigt es der Spielleitung an."""
        return self.publish({"type": "presence", "who": "pi", "active": bool(active), "at": int(time.time() * 1000)})

    def restore_state(self):
        """Nach einem (Neu-)Start: letzte Nachricht und Aktiv-Status der Spielleitung wiederherstellen
        (ntfy.sh hält Nachrichten 12 h vor)."""
        try:
            r = requests.get(f"{NTFY}/{self.topic}/json", params={"poll": "1", "since": "12h"}, timeout=15)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"Rückkanal: Wiederherstellen fehlgeschlagen ({e})")
            return
        last_message = last_presence = None
        for line in r.text.splitlines():
            try:
                data = json.loads(json.loads(line).get("message") or "null")
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("type") == "message":
                last_message = data
            elif isinstance(data, dict) and data.get("type") == "presence" and data.get("who") == "leitung":
                last_presence = data
        if last_message:
            self.handle_message(last_message)
        if last_presence:
            self.on_event("presence", last_presence.get("active") is True)

    def handle_message(self, data):
        text = str(data.get("text", "")).replace("\r", "").strip()[:300]
        self.on_event("message", {"id": str(data.get("id", ""))[:40], "text": text})

    def run(self):
        self.hello()
        self.presence(False)  # frisch gestartet = erst mal inaktiv, bis am Pi "Pi aktiv" gedrückt wird
        self.restore_state()
        while True:
            try:
                with requests.get(f"{NTFY}/{self.topic}/json", params={"since": self.since},
                                  stream=True, timeout=(10, 120)) as r:
                    r.raise_for_status()
                    for line in r.iter_lines():
                        if not line:
                            continue
                        msg = json.loads(line)
                        if msg.get("event") != "message":
                            continue
                        self.since = msg["id"]
                        try:
                            self.handle(msg)
                        except (requests.RequestException, ValueError, OSError) as e:
                            print(f"Rückkanal: Nachricht übersprungen ({e})")
            except (requests.RequestException, ValueError) as e:
                print(f"Rückkanal getrennt ({e}) – verbinde neu …")
            time.sleep(5)

    def handle(self, msg):
        data = json.loads(msg.get("message") or "null")
        # Unabhängig von der Runde: Löschen, Nachrichten (immer möglich) und Aktiv-Status der Spielleitung
        if isinstance(data, dict) and data.get("type") == "clear_archive":
            self.handle_clear(data)
            return
        if isinstance(data, dict) and data.get("type") == "message":
            self.handle_message(data)
            return
        if isinstance(data, dict) and data.get("type") == "presence" and data.get("who") == "leitung":
            self.on_event("presence", data.get("active") is True)
            return
        if not isinstance(data, dict) or not self.session or data.get("session") != self.session:
            return
        if data.get("type") == "review":
            missing = sorted({int(x) for x in data.get("missing", []) if isinstance(x, int) and 0 < x < 1000})
            self.on_event("review", {"missing": missing})
        elif data.get("type") == "image" and msg.get("attachment"):
            caption = " ".join(str(data.get("caption", "")).split())[:80]
            upload = str(data.get("upload", ""))[:40]
            help_image = data.get("help") is True  # Antwort auf eine Notfall-Meldung -> rot umrandet
            path = self.download(msg["attachment"], msg["id"])
            # Überschrift neben dem Bild speichern -> Archiv am Pi (übersteht Neustarts)
            meta = {"caption": caption, "received": int(time.time()), "upload": upload, "help": help_image}
            path.with_suffix(".json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            self.on_event("image", str(path), caption, upload, help_image)

    def handle_clear(self, data):
        """Spielleitung will alle gespeicherten Bilder löschen – nur mit richtigem Lösch-Passwort."""
        expected = clear_hash(self.cfg)
        ok = bool(expected) and hmac.compare_digest(str(data.get("hash", "")), expected)
        count = delete_received_images() if ok else 0
        reason = "" if ok else "Passwort falsch" if expected else "Auf der Fotobox ist kein Lösch-Passwort eingerichtet"
        print(f"Rückkanal: Bilder löschen -> {'gelöscht: ' + str(count) if ok else reason}")
        self.publish({"type": "cleared", "request": str(data.get("request", ""))[:40], "ok": ok, "count": count,
                      "reason": reason})
        if ok:
            self.on_event("archive_cleared", count)

    def download(self, attachment, msg_id):
        url = attachment.get("url", "")
        if not url.startswith(f"{NTFY}/file/"):
            raise ValueError(f"unerwartete Anhang-URL {url!r}")
        r = requests.get(url, timeout=30)
        r.raise_for_status()
        if len(r.content) > self.MAX_IMAGE_BYTES:
            raise ValueError("Bild zu groß")
        folder = BASE / "fotos" / self.session / "empfangen"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{msg_id}.jpg"
        path.write_bytes(r.content)
        return path


def clear_hash(cfg):
    """Das Lösch-Passwort geht nie im Klartext über ntfy.sh. Muss zu clearHash() in docs/app.js passen."""
    password = cfg["clear_password"].strip()
    if not password:
        return ""
    return hashlib.sha256(f"{password}:{cfg['ntfy_topic']}:clear".encode("utf-8")).hexdigest()


def received_images():
    """Alle je von der Spielleitung empfangenen Bilder, ältestes zuerst: [(pfad, überschrift, hilfe), ...]"""
    images = []
    for path in (BASE / "fotos").glob("*/empfangen/*.jpg"):
        try:
            meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        received = meta.get("received") or path.stat().st_mtime
        images.append((received, str(path), str(meta.get("caption", "")), meta.get("help") is True))
    images.sort()
    return [(p, c, h) for _, p, c, h in images]


def delete_received_images():
    """Löscht alle empfangenen Bilder (die eigenen Fotos des Pi bleiben). Gibt die Anzahl zurück."""
    folders = list((BASE / "fotos").glob("*/empfangen"))
    count = sum(len(list(folder.glob("*.jpg"))) for folder in folders)
    for folder in folders:
        shutil.rmtree(folder, ignore_errors=True)
    return count


def show_limits(gh, cfg):
    """Zeigt, wie viel von den Limits bei ntfy.sh und GitHub schon verbraucht ist."""
    try:
        r = requests.get(f"{NTFY}/v1/account", headers=ntfy_headers(cfg), timeout=10)
        r.raise_for_status()
        d = r.json()
        used, limit = d["stats"]["messages"], d["limits"]["messages"]
        left = d["stats"]["messages_remaining"]
        per_session = cfg["count"] + 2 if cfg["publish_every"] <= 0 else 6
        who = f"Konto {d['username']}" if d.get("username") and d["username"] != "*" else "anonym"
        print(f"ntfy.sh Live-Meldungen heute: {used} / {limit} ({who})  "
              f"übrig {left}, reicht für ca. {left // per_session} Durchgänge")
    except (requests.RequestException, KeyError, ValueError) as e:
        print(f"ntfy.sh: nicht abrufbar ({e})")

    try:
        r = gh.s.get(f"{API}/rate_limit", timeout=10)
        check(r)
        core = r.json()["resources"]["core"]
        reset = datetime.fromtimestamp(core["reset"]).strftime("%H:%M")
        print(f"GitHub API (Token):           {core['used']} / {core['limit']} diese Stunde  (Reset {reset} Uhr)")
        print("GitHub Uploads:               max. 500 pro Stunde (wird von GitHub nicht angezeigt)")
        r = gh.s.get(gh.repo_url, timeout=10)
        check(r)
        print(f"Repo-Größe:                   ca. {r.json()['size'] / 1024:.0f} MB von empfohlenen 1024 MB"
              "  (GitHub aktualisiert den Wert mit Verzögerung)")
    except requests.RequestException as e:
        print(f"GitHub: nicht abrufbar ({e})")


def setup(cfg):
    """Verbindet mit GitHub, startet den Upload-Thread und die Kamera."""
    gh = GitHub(cfg["token"], cfg["owner"], cfg["repo"], cfg["branch"])
    try:
        gh.ensure_branch()
        up = Uploader(gh, cfg)
    except requests.RequestException as e:
        raise SystemExit(f"Verbindung zu GitHub fehlgeschlagen:\n  {e}")
    up.start()
    return up, open_camera(cfg)


def main():
    parser = argparse.ArgumentParser(description="Raspberry-Pi-Fotobox mit GitHub-Pages-Anzeige")
    parser.add_argument("--keyboard", action="store_true", help="Enter-Taste statt GPIO-Knopf verwenden")
    parser.add_argument("--limits", action="store_true", help="Verbrauch der ntfy.sh- und GitHub-Limits anzeigen")
    args = parser.parse_args()

    cfg = load_config()
    if args.limits:
        show_limits(GitHub(cfg["token"], cfg["owner"], cfg["repo"], cfg["branch"]), cfg)
        return
    up, cam = setup(cfg)

    button = None
    if not args.keyboard:
        from gpiozero import Button
        button = Button(cfg["button_pin"], bounce_time=0.1)

    print("Fotobox bereit.")
    try:
        while True:
            if button:
                print("Warte auf Knopfdruck …")
                button.wait_for_press()
            else:
                input("Enter drücken zum Starten … ")
            run_batch(cam, up, cfg, Session(cfg["tasks"]), cfg["count"], cfg["countdown"])
    except KeyboardInterrupt:
        print("\nBeendet.")
    finally:
        cam.stop()


if __name__ == "__main__":
    main()
