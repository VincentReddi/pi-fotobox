#!/usr/bin/env python3
"""Touch-Oberfläche der Fotobox (Vollbild auf dem Pi-Display).

Ablauf einer Runde:
  Aufgaben wählen (− / +) → Starten → Countdown + Fotos → Warten auf die Rückmeldung der Spielleitung
  → (Fotos nachschicken → …) → Weiter → Warten auf Bilder → Bilder ansehen → Neue Runde
Bildbetrachter: links/rechts tippen oder wischen = blättern, ☰ unten links = Menü (Neue Runde).
Beenden: Escape-Taste oder das ⏻ oben rechts 3 Sekunden gedrückt halten.
"""
import queue
import threading
import tkinter as tk

import photobooth as pb

BG = "#111317"
FG = "#eceef2"
MUTED = "#8a909c"
ACCENT = "#ff5a5f"
GREEN = "#2e9e5b"
BLUE = "#3b6fd4"
GREY = "#2a2e36"
FLASH = "#ffffff"
TASKS_MIN, TASKS_MAX = 1, 50
HELLO_EVERY_MS = 6 * 3600 * 1000  # Rückkanal-"Hallo" auffrischen (ntfy.sh speichert 12 h)


def tk_safe(text):
    """Tk 8.6 kann Zeichen außerhalb U+FFFF (die meisten Emoji) nicht zuverlässig darstellen."""
    return "".join(ch for ch in text if ord(ch) <= 0xFFFF).strip()


def short(text, limit=110):
    """Fehlertexte kürzen, damit sie auf dem kleinen Display nicht in den Knopf ragen (voll im Terminal)."""
    print(text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()  # Worker-Threads -> GUI (Tk darf nur im Hauptthread angefasst werden)
        self.cfg = self.up = self.cam = self.backchannel = None
        self.gpio_button = None
        self.busy = False
        self.phase = None
        self.screen = None
        self.quit_timer = None
        self.menu_timer = None

        self.session = None  # pb.Session der laufenden Runde
        self.tasks = None  # gewählte Aufgaben-Anzahl
        self.review = None  # letzte Rückmeldung der Spielleitung
        self.images = []  # empfangene Bilder: (pfad, überschrift)
        self.image_index = 0
        self.last_upload = None
        self.photo = None  # Referenz auf das angezeigte Bild (sonst räumt Tk es weg)

        root.title("Pi Fotobox")
        root.configure(bg=BG, cursor="none")
        root.attributes("-fullscreen", True)
        root.bind("<Escape>", lambda e: self.quit())

        self.W, self.H = root.winfo_screenwidth(), root.winfo_screenheight()
        H, font = self.H, "DejaVu Sans"
        self.f_title = (font, max(14, H // 11), "bold")
        self.f_big = (font, max(28, H // 3), "bold")
        self.f_value = (font, max(24, H // 5), "bold")
        self.f_medium = (font, max(14, H // 14), "bold")
        self.f_button = (font, max(20, H // 7), "bold")
        self.f_button_small = (font, max(12, H // 18), "bold")
        self.f_info = (font, max(10, H // 26))
        self.f_caption = (font, max(14, H // 16), "bold")
        self.f_counter = (font, max(8, H // 40))

        self.frames = {}
        for name in ("message", "setup", "capture", "confirm", "wait_images", "viewer"):
            frame = tk.Frame(root, bg=BG)
            frame.place(x=0, y=0, relwidth=1, relheight=1)
            self.frames[name] = frame
        self.build_message()
        self.build_setup()
        self.build_capture()
        self.build_confirm()
        self.build_wait_images()
        self.build_viewer()

        self.power = tk.Label(root, text="⏻", font=(font, max(12, H // 20)), fg="#3a3f48", bg=BG)
        self.power.place(relx=1.0, x=-10, y=6, anchor="ne")
        self.power.bind("<ButtonPress-1>", self.power_down)
        self.power.bind("<ButtonRelease-1>", self.power_up)

        self.show_message("Einen Moment …", "Kamera und Verbindung werden vorbereitet")
        threading.Thread(target=self.init_backend, daemon=True).start()
        root.after(50, self.pump)

    # ---------- Bausteine ----------

    def label(self, parent, font, fg=FG, **kw):
        return tk.Label(parent, font=font, fg=fg, bg=BG, **kw)

    def button(self, parent, text, color, font, command, **kw):
        return tk.Button(
            parent, text=text, font=font, fg="white", bg=color, activeforeground="white",
            activebackground=color, relief="flat", bd=0, highlightthickness=0, command=command, **kw,
        )

    def show(self, name):
        self.screen = name
        self.frames[name].tkraise()
        if name == "viewer":
            self.power.lower()  # im Bildbetrachter nur Bild + Überschrift
        else:
            self.power.lift()

    # ---------- Bildschirme ----------

    def build_message(self):
        f = self.frames["message"]
        self.msg_title = self.label(f, self.f_title)
        self.msg_title.place(relx=0.5, rely=0.14, anchor="center")
        self.msg_info = self.label(f, self.f_info, fg=MUTED, wraplength=self.W - 40)
        self.msg_info.place(relx=0.5, rely=0.9, anchor="center")
        self.msg_button = self.button(f, "", GREEN, self.f_button, self.on_message_button)
        self.msg_action = None

    def show_message(self, title, info, button=None, action=None):
        self.msg_title.config(text=title)
        self.msg_info.config(text=info)
        self.msg_action = action
        if button:
            self.msg_button.config(text=button)
            self.msg_button.place(relx=0.5, rely=0.52, relwidth=0.8, relheight=0.45, anchor="center")
        else:
            self.msg_button.place_forget()
        self.show("message")

    def on_message_button(self):
        if self.msg_action:
            self.msg_action()

    def build_setup(self):
        f = self.frames["setup"]
        self.label(f, self.f_title, text="Pi Fotobox").place(relx=0.5, rely=0.09, anchor="center")
        self.label(f, self.f_medium, fg=MUTED, text="Aufgaben").place(relx=0.5, rely=0.225, anchor="center")
        kw = dict(repeatdelay=400, repeatinterval=120)  # gedrückt halten = schnell zählen
        self.button(f, "−", GREY, self.f_value, lambda: self.change_tasks(-1), **kw).place(
            relx=0.2, rely=0.46, relwidth=0.22, relheight=0.21, anchor="center")
        self.button(f, "+", GREY, self.f_value, lambda: self.change_tasks(+1), **kw).place(
            relx=0.8, rely=0.46, relwidth=0.22, relheight=0.21, anchor="center")
        self.tasks_label = self.label(f, self.f_value, fg=ACCENT)
        self.tasks_label.place(relx=0.5, rely=0.46, anchor="center")
        self.button(f, "Starten", GREEN, self.f_button, self.start_round).place(
            relx=0.5, rely=0.77, relwidth=0.8, relheight=0.25, anchor="center")
        self.setup_info = self.label(f, self.f_info, fg=MUTED)
        self.setup_info.place(relx=0.5, rely=0.955, anchor="center")

    def show_setup(self):
        if self.tasks is None:
            self.tasks = max(TASKS_MIN, min(TASKS_MAX, int(self.cfg["tasks"])))
        self.tasks_label.config(text=str(self.tasks))
        self.setup_info.config(text=f"{self.cfg['count']} Fotos · {self.cfg['countdown']} s Countdown")
        self.show("setup")

    def change_tasks(self, delta):
        self.tasks = max(TASKS_MIN, min(TASKS_MAX, self.tasks + delta))
        self.tasks_label.config(text=str(self.tasks))

    def build_capture(self):
        f = self.frames["capture"]
        self.cap_title = self.label(f, self.f_title)
        self.cap_title.place(relx=0.5, rely=0.12, anchor="center")
        self.cap_big = self.label(f, self.f_big, fg=ACCENT)
        self.cap_big.place(relx=0.5, rely=0.52, anchor="center")
        self.cap_info = self.label(f, self.f_info, fg=MUTED, wraplength=self.W - 40)
        self.cap_info.place(relx=0.5, rely=0.92, anchor="center")

    def show_capture(self, title, big, info, color=ACCENT):
        self.cap_title.config(text=title)
        self.cap_big.config(text=big, fg=color)
        self.cap_info.config(text=info)
        if self.screen != "capture":
            self.show("capture")

    def build_confirm(self):
        f = self.frames["confirm"]
        self.conf_title = self.label(f, self.f_title)
        self.conf_title.place(relx=0.5, rely=0.1, anchor="center")
        self.conf_status = self.label(f, self.f_medium, wraplength=self.W - 40)
        self.conf_status.place(relx=0.5, rely=0.33, anchor="center")
        self.conf_info = self.label(f, self.f_info, fg=MUTED, wraplength=self.W - 40)
        self.conf_info.place(relx=0.5, rely=0.53, anchor="center")
        self.resend_button = self.button(f, "", BLUE, self.f_button_small, self.resend)
        self.resend_button.place(relx=0.265, rely=0.79, relwidth=0.45, relheight=0.3, anchor="center")
        self.button(f, "Weiter", GREEN, self.f_button, self.go_images).place(
            relx=0.735, rely=0.79, relwidth=0.45, relheight=0.3, anchor="center")

    def show_confirm(self):
        if self.review is None:
            self.conf_title.config(text="Warte auf Rückmeldung …")
            self.conf_status.config(text="Die Spielleitung prüft die Aufgaben", fg=MUTED)
        elif not self.review["missing"]:
            self.conf_title.config(text="Rückmeldung")
            self.conf_status.config(text="✓ Alle Aufgaben angekommen", fg=GREEN)
        else:
            missing = ", ".join(str(n) for n in self.review["missing"])
            words = "fehlt: Aufgabe" if len(self.review["missing"]) == 1 else "fehlen: Aufgaben"
            self.conf_title.config(text="Rückmeldung")
            self.conf_status.config(text=f"Es {words} {missing}", fg=ACCENT)
        info = f"{self.session.tasks} Aufgaben · {self.session.captured} Fotos gesendet"
        if self.images:
            info += f" · {len(self.images)} {'Bild' if len(self.images) == 1 else 'Bilder'} empfangen"
        if not self.backchannel:
            info += "\nRückkanal aus: web_password fehlt in config.json"
        self.conf_info.config(text=info)
        self.resend_button.config(text=f"{self.cfg['resend_count']} Fotos\nnachschicken")
        self.show("confirm")

    def build_wait_images(self):
        f = self.frames["wait_images"]
        self.label(f, self.f_title, text="Warte auf Bilder …").place(relx=0.5, rely=0.28, anchor="center")
        self.wait_info = self.label(f, self.f_info, fg=MUTED, wraplength=self.W - 40)
        self.wait_info.place(relx=0.5, rely=0.47, anchor="center")
        self.button(f, "Neue Runde", GREY, self.f_button_small, self.new_round).place(
            relx=0.5, rely=0.8, relwidth=0.55, relheight=0.2, anchor="center")

    def build_viewer(self):
        f = self.frames["viewer"]
        f.config(bg="black")
        self.canvas = tk.Canvas(f, bg="black", highlightthickness=0, bd=0)
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.canvas.bind("<ButtonPress-1>", self.viewer_press)
        self.canvas.bind("<ButtonRelease-1>", self.viewer_release)
        # 16:9-Bild über die volle Breite, darunter die Überschrift (auf 640×480: 640×360 + 120 px)
        self.img_h = int(min(self.W * 9 / 16, self.H * 0.8))
        self.img_w = int(self.img_h * 16 / 9)
        # Kleiner Menü-Knopf unten links – die Überschrift bleibt immer frei lesbar
        size = max(36, self.H // 10)
        self.button(f, "☰", GREY, self.f_button_small, self.toggle_menu).place(
            x=8, rely=1.0, y=-8, width=size, height=size, anchor="sw")
        self.caption_width = self.W - 2 * (size + 16)  # Abstand zu Menü-Knopf und Bildzähler
        # Menü erscheint über dem Bild, nicht über der Überschrift
        self.menu = tk.Frame(f, bg=BG, highlightthickness=2, highlightbackground=GREY)
        self.button(self.menu, "Neue Runde", GREEN, self.f_button_small, self.new_round).place(
            relx=0.5, rely=0.29, relwidth=0.86, relheight=0.38, anchor="center")
        self.button(self.menu, "Zurück zum Bild", GREY, self.f_button_small, self.hide_menu).place(
            relx=0.5, rely=0.73, relwidth=0.86, relheight=0.38, anchor="center")
        self.press_x = 0

    def show_images(self):
        if not self.images:
            if self.backchannel:
                self.wait_info.config(text="Die Spielleitung schickt die Bilder über die Webseite")
            else:
                self.wait_info.config(text="Rückkanal aus: web_password fehlt in config.json")
            self.show("wait_images")
            return
        self.image_index = max(0, min(self.image_index, len(self.images) - 1))
        self.render_image()
        self.show("viewer")

    def render_image(self):
        path, caption = self.images[self.image_index]
        caption = tk_safe(caption)
        c = self.canvas
        c.delete("all")
        try:
            from PIL import Image, ImageOps, ImageTk

            with Image.open(path) as img:
                fitted = ImageOps.contain(img.convert("RGB"), (self.img_w, self.img_h))
            self.photo = ImageTk.PhotoImage(fitted)
            c.create_image(self.W // 2, self.img_h // 2, image=self.photo)
        except ImportError:
            c.create_text(self.W // 2, self.img_h // 2, fill=ACCENT, font=self.f_info, width=self.W - 40,
                          text="Pillow fehlt: sudo apt install python3-pil.imagetk")
        except OSError as e:
            c.create_text(self.W // 2, self.img_h // 2, fill=ACCENT, font=self.f_info, width=self.W - 40,
                          text=short(f"Bild kaputt: {e}"))
        caption_mid = (self.img_h + self.H) // 2
        if caption:
            c.create_text(self.W // 2, caption_mid, text=caption, fill=FG, font=self.f_caption,
                          width=self.caption_width, justify="center")
        if len(self.images) > 1:
            c.create_text(self.W - 8, self.H - 6, anchor="se", fill=MUTED, font=self.f_counter,
                          text=f"{self.image_index + 1}/{len(self.images)}")

    def step(self, delta):
        if self.images:
            self.image_index = (self.image_index + delta) % len(self.images)
            self.render_image()

    def viewer_press(self, event):
        self.press_x = event.x

    def viewer_release(self, event):
        if self.menu.winfo_ismapped():  # Tippen neben das Menü schließt es
            self.hide_menu()
            return
        dx = event.x - self.press_x
        if abs(dx) > self.W * 0.12:  # wischen
            self.step(-1 if dx > 0 else +1)
        else:  # linke Hälfte = zurück, rechte Hälfte = weiter
            self.step(-1 if event.x < self.W / 2 else +1)

    def toggle_menu(self):
        if self.menu.winfo_ismapped():
            self.hide_menu()
            return
        self.menu.place(relx=0.5, y=self.img_h // 2, width=int(self.W * 0.7), height=int(self.img_h * 0.62),
                        anchor="center")
        self.menu_timer = self.root.after(8000, self.hide_menu)

    def hide_menu(self):
        if self.menu_timer:
            self.root.after_cancel(self.menu_timer)
            self.menu_timer = None
        self.menu.place_forget()

    # ---------- Aktionen ----------

    def start_round(self):
        if self.busy:
            return
        self.session = pb.Session(self.tasks)
        self.review = None
        self.images = []
        self.image_index = 0
        self.last_upload = None
        if self.backchannel:
            self.backchannel.session = self.session.id
            threading.Thread(target=self.backchannel.hello, daemon=True).start()
        text = "Alte Fotos werden gelöscht …" if self.cfg["clear_old"] else ""
        self.run_batch(self.cfg["count"], self.cfg["countdown"], "Gleich geht's los!", text)

    def resend(self):
        if self.busy:
            return
        self.review = None  # die Spielleitung prüft nach dem Nachschub neu
        self.run_batch(self.cfg["resend_count"], self.cfg["resend_countdown"], "Nachschub!", "")

    def run_batch(self, count, countdown, title, info):
        self.busy = True
        self.show_capture(title, str(countdown), info)
        threading.Thread(target=self.batch_worker, args=(count, countdown), daemon=True).start()

    def batch_worker(self, count, countdown):
        try:
            pb.run_batch(self.cam, self.up, self.cfg, self.session, count, countdown,
                         notify=lambda *ev: self.events.put(ev))
            self.events.put(("finished",))
        except Exception as e:
            self.events.put(("error", f"{type(e).__name__}: {e}"))

    def go_images(self):
        if self.busy:
            return
        self.up.submit(self.up.set_state, "images")
        self.show_images()

    def new_round(self):
        self.hide_menu()
        self.show_setup()

    # ---------- Hintergrund ----------

    def init_backend(self):
        try:
            if self.cfg is None:
                self.cfg = pb.load_config()
                self.setup_gpio_button()
            if self.up is None:
                self.up, self.cam = pb.setup(self.cfg)
            if self.backchannel is None and pb.back_topic(self.cfg):
                self.backchannel = pb.Backchannel(self.cfg, lambda *ev: self.events.put(ev))
                self.backchannel.start()
                self.root.after(HELLO_EVERY_MS, self.refresh_hello)
            self.events.put(("ready",))
        except SystemExit as e:
            self.events.put(("fatal", str(e)))
        except Exception as e:  # Kamera fehlt, kein Netz, …
            self.events.put(("fatal", f"{type(e).__name__}: {e}"))

    def refresh_hello(self):
        threading.Thread(target=self.backchannel.hello, daemon=True).start()
        self.root.after(HELLO_EVERY_MS, self.refresh_hello)

    def setup_gpio_button(self):
        """Der Hardware-Taster startet eine Runde, genau wie der Starten-Knopf (falls angeschlossen)."""
        try:
            from gpiozero import Button

            self.gpio_button = Button(self.cfg["button_pin"], bounce_time=0.1)
            self.gpio_button.when_pressed = lambda: self.events.put(("trigger",))
        except Exception:
            self.gpio_button = None

    def pump(self):
        try:
            while True:
                self.handle(*self.events.get_nowait())
        except queue.Empty:
            pass
        self.root.after(50, self.pump)

    def handle(self, kind, *args):
        if kind in ("countdown", "photo", "uploading"):
            self.phase = kind
        if kind == "ready":
            self.show_setup()
        elif kind == "trigger":
            if self.screen == "setup":
                self.start_round()
        elif kind == "countdown":
            self.show_capture(self.cap_title.cget("text"), str(args[0]), "Schau in die Kamera und lächle!")
        elif kind == "photo":
            n, count = args
            self.show_capture("Foto", f"{n}/{count}", "Weiter lächeln!")
            self.flash()
        elif kind == "uploading":
            n, total = args
            self.show_capture("Lade hoch …", f"{n}/{total}", "Die Fotos erscheinen gerade auf der Webseite")
        elif kind == "uploaded":
            n, total = args
            if self.phase == "uploading":
                self.cap_big.config(text=f"{n}/{total}")
            else:
                self.cap_info.config(text=f"Hochgeladen: {n}/{total}")
        elif kind == "finished":
            self.busy = False
            self.phase = None
            self.show_confirm()
        elif kind == "error":
            self.busy = False
            self.phase = None
            after = self.show_confirm if self.session and self.session.captured else self.show_setup
            self.show_message("Fehler", short(args[0]), "Weiter", after)
        elif kind == "fatal":
            self.show_message("Fotobox nicht bereit", short(args[0]), "Erneut versuchen", self.retry_init)
        elif kind == "review":
            self.review = args[0]
            self.up.submit(self.up.set_review, self.review)  # auch allen Zuschauern auf der Webseite zeigen
            if self.screen == "confirm":
                self.show_confirm()
        elif kind == "image":
            path, caption, upload = args
            self.images.append((path, caption))
            if upload != self.last_upload:  # neue Sendung: direkt ihr erstes Bild zeigen
                self.last_upload = upload
                self.image_index = len(self.images) - 1
            if self.screen in ("wait_images", "viewer"):
                self.show_images()
            elif self.screen == "confirm":
                self.show_confirm()

    def retry_init(self):
        self.show_message("Einen Moment …", "Kamera und Verbindung werden vorbereitet")
        threading.Thread(target=self.init_backend, daemon=True).start()

    def flash(self):
        f = self.frames["capture"]
        widgets = (f, self.cap_title, self.cap_big, self.cap_info, self.power)
        for w in widgets:
            w.config(bg=FLASH)
        self.root.after(120, lambda: [w.config(bg=BG) for w in widgets])

    # ---------- Beenden ----------

    def power_down(self, _event):
        self.quit_timer = self.root.after(3000, self.quit)

    def power_up(self, _event):
        if self.quit_timer:
            self.root.after_cancel(self.quit_timer)
            self.quit_timer = None

    def quit(self):
        if self.cam:
            try:
                self.cam.stop()
            except Exception:
                pass
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
