#!/usr/bin/env python3
"""Fotobox für den Raspberry Pi.

Knopf drücken -> 15 s Countdown -> 30 Fotos mit dem Kameramodul ->
jedes Foto wird sofort in den Branch "photos" des GitHub-Repos hochgeladen.
Die GitHub-Pages-Seite liest status.json aus diesem Branch und zeigt die Bilder live an.
"""
import argparse
import base64
import json
import queue
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parent
API = "https://api.github.com"
STATUS_PATH = "status.json"

DEFAULTS = {
    "branch": "photos",
    "button_pin": 17,
    "countdown": 15,
    "count": 30,
    "interval": 1.0,
    "resolution": [1920, 1080],
    "jpeg_quality": 85,
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
            r.raise_for_status()
        repo = self.s.get(self.repo_url, timeout=20)
        repo.raise_for_status()
        default = repo.json()["default_branch"]
        ref = self.s.get(f"{self.repo_url}/git/ref/heads/{default}", timeout=20)
        ref.raise_for_status()
        r = self.s.post(f"{self.repo_url}/git/refs", timeout=20, json={
            "ref": f"refs/heads/{self.branch}",
            "sha": ref.json()["object"]["sha"],
        })
        r.raise_for_status()
        print(f"Branch '{self.branch}' angelegt.")

    def get_sha(self, path):
        r = self.s.get(f"{self.repo_url}/contents/{path}", params={"ref": self.branch}, timeout=20)
        if r.status_code == 404:
            return None
        r.raise_for_status()
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
        r.raise_for_status()
        return r.json()["content"]["sha"]


class Uploader(threading.Thread):
    """Arbeitet Uploads nacheinander ab, damit die Aufnahme nie auf das Netzwerk warten muss.

    Nur dieser Thread verändert self.status – dadurch keine Race Conditions.
    """

    def __init__(self, gh):
        super().__init__(daemon=True)
        self.gh = gh
        self.jobs = queue.Queue()
        self.status = {"state": "idle", "photos": []}
        self.status_sha = gh.get_sha(STATUS_PATH)

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

    def start_session(self, session, total, countdown_end):
        self.status = {
            "session": session,
            "state": "countdown",
            "countdown_end": int(countdown_end * 1000),
            "total": total,
            "photos": [],
        }
        self.write_status()

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
        self.status["state"] = "done" if len(photos) >= self.status["total"] else "capturing"
        self.write_status()
        print(f"  ↑ {remote} hochgeladen ({len(photos)}/{self.status['total']})")


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


def run_session(cam, up, cfg):
    session = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    total = cfg["count"]
    countdown_end = time.time() + cfg["countdown"]
    up.submit(up.start_session, session, total, countdown_end)

    print(f"\nSession {session}: Countdown {cfg['countdown']} s")
    remaining = cfg["countdown"]
    while remaining > 0:
        print(f"  {remaining} …", flush=True)
        time.sleep(max(0, countdown_end - remaining + 1 - time.time()))
        remaining -= 1

    local_dir = BASE / "fotos" / session
    local_dir.mkdir(parents=True, exist_ok=True)
    next_shot = time.monotonic()
    for n in range(1, total + 1):
        name = f"{n:03d}.jpg"
        local = local_dir / name
        cam.capture_file(str(local))
        print(f"  📸 Foto {n}/{total}")
        up.submit(up.add_photo, local, f"sessions/{session}/{name}")
        next_shot += cfg["interval"]
        time.sleep(max(0, next_shot - time.monotonic()))

    print("  Aufnahme fertig, warte auf restliche Uploads …")
    up.jobs.join()
    print("  Alles hochgeladen ✔")


def main():
    parser = argparse.ArgumentParser(description="Raspberry-Pi-Fotobox mit GitHub-Pages-Anzeige")
    parser.add_argument("--keyboard", action="store_true", help="Enter-Taste statt GPIO-Knopf verwenden")
    args = parser.parse_args()

    cfg = load_config()
    gh = GitHub(cfg["token"], cfg["owner"], cfg["repo"], cfg["branch"])
    gh.ensure_branch()

    up = Uploader(gh)
    up.start()
    cam = open_camera(cfg)

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
            run_session(cam, up, cfg)
    except KeyboardInterrupt:
        print("\nBeendet.")
    finally:
        cam.stop()


if __name__ == "__main__":
    main()
