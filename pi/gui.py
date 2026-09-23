#!/usr/bin/env python3
"""Touch-Oberfläche der Fotobox (Vollbild auf dem Pi-Display).

Ablauf einer Runde:
  Aufgaben wählen (− / +) → Starten → Countdown + Fotos → Warten auf die Rückmeldung der Spielleitung
  → (Fotos nachschicken → …) → Weiter → Warten auf Bilder → Bilder ansehen → Neue Runde
Bildbetrachter: links/rechts tippen oder wischen = blättern, ☰ unten links = Menü (Neue Runde),
✉ unten rechts = Nachricht der Spielleitung (Antwort OK / Egal / Neustart).
Startbildschirm: "Bilder" oben links = alle je empfangenen Bilder am Stück (bleiben auch nach Neustarts
gespeichert; die Spielleitung kann sie über die Webseite mit dem Lösch-Passwort löschen).
Beenden: Escape-Taste oder das ⏻ oben rechts 3 Sekunden gedrückt halten.
"""
import queue
import threading
import time
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
HOLD_MS = 1500  # so lange müssen wichtige Knöpfe (Neustart, Neue Runde) gedrückt werden
HOLD_REPLIES = ("Neustart",)  # Antworten, die Gedrückthalten brauchen
LETTERS = ["—"] + [chr(c) for c in range(ord("A"), ord("Z") + 1)]  # optionaler Buchstabe bei "Problem melden"
REPORT_TEXT = {"missing": "fehlt", "unsolvable": "ist nicht lösbar"}


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
        self.viewer_mode = "live"  # "live" = aktuelle Runde, "archive" = gespeicherte Bilder
        self.archive_images = []
        self.archive_index = 0
        self.chat = None  # aktuelle Nachricht der Spielleitung: {"id", "text"}
        self.chat_status = ""
        self.chat_answered = False
        self.chat_widgets = []  # (Text-Label, Status-Label) je Nachrichten-Block
        self.report_kind = None  # Notfall-Meldung: "missing" (Aufgabe fehlt) / "unsolvable" (nicht lösbar)
        self.report_task = 1
        self.report_letter = 0  # Index in LETTERS, 0 = kein Buchstabe
        self.report_busy = False

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
        self.f_info_bold = (font, max(10, H // 26), "bold")
        self.f_chat = ((font, max(14, H // 14), "bold"), (font, max(11, H // 22), "bold"), (font, max(9, H // 30)))
        self.f_caption = (font, max(14, H // 16), "bold")
        self.f_counter = (font, max(8, H // 40))

        self.frames = {}
        for name in ("message", "setup", "capture", "confirm", "wait_images", "viewer", "report"):
            frame = tk.Frame(root, bg=BG)
            frame.place(x=0, y=0, relwidth=1, relheight=1)
            self.frames[name] = frame
        self.build_message()
        self.build_setup()
        self.build_capture()
        self.build_confirm()
        self.build_wait_images()
        self.build_viewer()
        self.build_report()

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

    def hold_button(self, parent, text, color, font, action):
        """Knopf für wichtige Aktionen (Neustart, Neue Runde): löst erst nach HOLD_MS Gedrückthalten aus.
        Ein Balken im Knopf zeigt den Fortschritt; zu früh losgelassen = nichts passiert."""
        button = self.button(parent, text, color, font, None)
        bar = tk.Frame(parent, bg=FG)
        state = {"job": None, "start": 0.0, "hint": None}

        def tick():
            progress = (time.monotonic() - state["start"]) * 1000 / HOLD_MS
            if progress >= 1:
                stop()
                action()
                return
            bar.place(in_=button, relx=0, rely=1, anchor="sw", relwidth=progress, relheight=0.12)
            bar.lift()
            state["job"] = self.root.after(30, tick)

        def stop():
            if state["job"]:
                self.root.after_cancel(state["job"])
                state["job"] = None
            bar.place_forget()

        def press(_event):
            if state["hint"]:
                self.root.after_cancel(state["hint"])
                state["hint"] = None
            button.config(text=text, font=font)
            state["start"] = time.monotonic()
            tick()

        def release(_event):
            if not state["job"]:
                return  # schon ausgelöst
            stop()
            button.config(text="Gedrückt halten", font=self.f_info_bold)
            state["hint"] = self.root.after(1200, lambda: button.config(text=text, font=font))

        button.bind("<ButtonPress-1>", press, add="+")
        button.bind("<ButtonRelease-1>", release, add="+")
        return button

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
            # lange Knopftexte ("Zurück zu den Bildern") in kleinerer Schrift, sonst werden sie abgeschnitten
            self.msg_button.config(text=button, font=self.f_button if len(button) <= 10 else self.f_button_small)
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
        size = max(36, self.H // 10)
        self.button(f, "Bilder", GREY, self.f_info_bold, self.show_archive).place(
            x=8, y=8, width=int(self.W * 0.2), height=size)

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

    # ---------- Warten auf Bilder (+ Nachricht der Spielleitung) ----------

    def build_wait_images(self):
        f = self.frames["wait_images"]
        self.label(f, self.f_medium, text="Warte auf Bilder …").place(relx=0.5, rely=0.07, anchor="center")
        self.wait_info = self.label(f, self.f_info, fg=MUTED, wraplength=self.W - 40)
        self.wait_chat = self.build_chat_panel(f)
        self.hold_button(f, "Neue Runde", GREY, self.f_button_small, self.new_round).place(
            relx=0.5, rely=0.91, relwidth=0.5, relheight=0.14, anchor="center")

    def build_chat_panel(self, parent):
        """Nachricht der Spielleitung + Antwort-Knöpfe. Gibt es zweimal: im Warte-Bildschirm und über dem Bild."""
        panel = tk.Frame(parent, bg=BG)
        self.label(panel, self.f_info, fg=MUTED, text="Nachricht der Spielleitung").place(
            relx=0.5, rely=0.08, anchor="center")
        text = self.label(panel, self.f_chat[0], wraplength=int(self.W * 0.92), justify="center")
        text.place(relx=0.5, rely=0.36, anchor="center")
        colors = {"OK": GREEN, "Egal": GREY, "Neustart": BLUE}
        for i, answer in enumerate(pb.REPLIES):
            make = self.hold_button if answer in HOLD_REPLIES else self.button
            make(panel, answer, colors[answer], self.f_button_small, lambda a=answer: self.send_reply(a)).place(
                relx=(0.18, 0.5, 0.82)[i], rely=0.72, relwidth=0.3, relheight=0.28, anchor="center")
        status = self.label(panel, self.f_info, fg=MUTED)
        status.place(relx=0.5, rely=0.94, anchor="center")
        self.chat_widgets.append((text, status))
        return panel

    def update_chat(self):
        text = tk_safe(self.chat["text"]) if self.chat else ""
        font = self.f_chat[0] if len(text) <= 45 else self.f_chat[1] if len(text) <= 120 else self.f_chat[2]
        for text_label, status_label in self.chat_widgets:
            text_label.config(text=text, font=font)
            status_label.config(text=self.chat_status)
        self.update_mail_button()

    def show_wait_images(self):
        if self.chat:
            self.wait_info.place_forget()
            self.wait_chat.place(relx=0.5, rely=0.14, relwidth=1, relheight=0.66, anchor="n")
        else:
            self.wait_chat.place_forget()
            self.wait_info.config(text="Die Spielleitung schickt die Bilder über die Webseite" if self.backchannel
                                  else "Rückkanal aus: web_password fehlt in config.json")
            self.wait_info.place(relx=0.5, rely=0.45, anchor="center")
        self.show("wait_images")

    def send_reply(self, answer):
        if not self.chat or not self.backchannel:
            return
        self.chat_status = f"Sende „{answer}“ …"
        self.update_chat()
        message_id = self.chat["id"]

        def worker():
            ok = self.backchannel.reply(message_id, answer)
            self.events.put(("reply_sent", answer, ok, time.strftime("%H:%M:%S")))

        threading.Thread(target=worker, daemon=True).start()

    # ---------- Bildbetrachter (live = aktuelle Runde, archive = gespeicherte Bilder) ----------

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
        # Kleine Knöpfe in den unteren Ecken – die Überschrift dazwischen bleibt immer frei lesbar
        size = max(36, self.H // 10)
        self.button(f, "☰", GREY, self.f_button_small, self.toggle_menu).place(
            x=8, rely=1.0, y=-8, width=size, height=size, anchor="sw")
        self.mail_button = self.button(f, "✉", ACCENT, self.f_button_small, self.toggle_chat)
        self.mail_size = size
        self.caption_width = self.W - 2 * (size + 16)
        # Menüs und Nachricht erscheinen über dem Bild, nie über der Überschrift
        self.menu_live = tk.Frame(f, bg=BG, highlightthickness=2, highlightbackground=GREY)
        self.menu_buttons(self.menu_live, (("Problem melden", ACCENT, self.show_report),
                                           ("Neue Runde", GREEN, self.new_round, True),
                                           ("Zurück zum Bild", GREY, self.hide_overlays)))
        self.menu_archive = tk.Frame(f, bg=BG, highlightthickness=2, highlightbackground=GREY)
        self.menu_buttons(self.menu_archive, (("Startbildschirm", GREEN, self.leave_archive),
                                              ("Zurück zum Bild", GREY, self.hide_overlays)))
        self.chat_overlay = tk.Frame(f, bg=BG, highlightthickness=2, highlightbackground=GREY)
        self.build_chat_panel(self.chat_overlay).place(x=0, y=0, relwidth=1, relheight=1)
        self.button(self.chat_overlay, "✕", GREY, self.f_info, self.hide_overlays).place(
            relx=1.0, x=-6, y=6, width=size - 8, height=size - 8, anchor="ne")
        self.press_x = 0

    def menu_buttons(self, frame, entries):
        """entries: (Text, Farbe, Aktion[, gedrückt halten?])"""
        step = 1 / len(entries)
        for i, (text, color, command, *hold) in enumerate(entries):
            make = self.hold_button if hold and hold[0] else self.button
            make(frame, text, color, self.f_button_small, command).place(
                relx=0.5, rely=step * (i + 0.5), relwidth=0.86, relheight=step * 0.8, anchor="center")

    def viewer_items(self):
        return self.images if self.viewer_mode == "live" else self.archive_images

    def show_images(self):
        """Bilder der laufenden Runde (oder Warte-Bildschirm, solange noch keins da ist)."""
        self.viewer_mode = "live"
        if not self.images:
            self.show_wait_images()
            return
        self.image_index = max(0, min(self.image_index, len(self.images) - 1))
        self.render_image()
        self.update_mail_button()
        self.show("viewer")

    def render_image(self):
        items = self.viewer_items()
        index = self.image_index if self.viewer_mode == "live" else self.archive_index
        path, caption, help_image = items[index]
        caption = tk_safe(caption)
        c = self.canvas
        c.delete("all")
        try:
            from PIL import Image, ImageOps, ImageTk

            with Image.open(path) as img:
                fitted = ImageOps.contain(img.convert("RGB"), (self.img_w, self.img_h))
            self.photo = ImageTk.PhotoImage(fitted)
            c.create_image(self.W // 2, self.img_h // 2, image=self.photo)
            if help_image:  # Hilfe-Bild der Spielleitung (Antwort auf "Problem melden"): rot umrandet
                w, h = fitted.size
                x0, y0 = (self.W - w) // 2, (self.img_h - h) // 2
                c.create_rectangle(x0 + 3, y0 + 3, x0 + w - 3, y0 + h - 3, outline=ACCENT, width=6)
                c.create_rectangle(x0 + 6, y0 + 6, x0 + 6 + self.W // 7, y0 + 6 + self.H // 16, fill=ACCENT,
                                   outline=ACCENT)
                c.create_text(x0 + 6 + self.W // 14, y0 + 6 + self.H // 32, text="Hilfe", fill="white",
                              font=self.f_info_bold)
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
        if len(items) > 1:
            c.create_text(self.W - 8, self.img_h + 4, anchor="ne", fill=MUTED, font=self.f_counter,
                          text=f"{index + 1}/{len(items)}")

    def step(self, delta):
        items = self.viewer_items()
        if not items:
            return
        if self.viewer_mode == "live":
            self.image_index = (self.image_index + delta) % len(items)
        else:
            self.archive_index = (self.archive_index + delta) % len(items)
        self.render_image()

    def viewer_press(self, event):
        self.press_x = event.x

    def viewer_release(self, event):
        if self.overlay_open():  # Tippen neben Menü/Nachricht schließt sie
            self.hide_overlays()
            return
        dx = event.x - self.press_x
        if abs(dx) > self.W * 0.12:  # wischen
            self.step(-1 if dx > 0 else +1)
        else:  # linke Hälfte = zurück, rechte Hälfte = weiter
            self.step(-1 if event.x < self.W / 2 else +1)

    def overlay_open(self):
        return any(w.winfo_ismapped() for w in (self.menu_live, self.menu_archive, self.chat_overlay))

    def place_overlay(self, frame, rel_w, rel_h):
        self.hide_overlays()
        frame.place(relx=0.5, y=self.img_h // 2, width=int(self.W * rel_w), height=int(self.img_h * rel_h),
                    anchor="center")

    def toggle_menu(self):
        menu = self.menu_live if self.viewer_mode == "live" else self.menu_archive
        if menu.winfo_ismapped():
            self.hide_overlays()
            return
        self.place_overlay(menu, 0.7, 0.86 if menu is self.menu_live else 0.62)  # 3 bzw. 2 Einträge
        self.menu_timer = self.root.after(8000, self.hide_overlays)

    def toggle_chat(self):
        if self.chat_overlay.winfo_ismapped():
            self.hide_overlays()
        elif self.chat:
            self.place_overlay(self.chat_overlay, 1.0, 1.0)

    def hide_overlays(self):
        if self.menu_timer:
            self.root.after_cancel(self.menu_timer)
            self.menu_timer = None
        for w in (self.menu_live, self.menu_archive, self.chat_overlay):
            w.place_forget()

    def update_mail_button(self):
        if self.chat and self.viewer_mode == "live":
            color = GREY if self.chat_answered else ACCENT  # rot = noch nicht beantwortet
            self.mail_button.config(bg=color, activebackground=color)
            self.mail_button.place(relx=1.0, x=-8, rely=1.0, y=-8, width=self.mail_size, height=self.mail_size,
                                   anchor="se")
        else:
            self.mail_button.place_forget()
            if self.chat_overlay.winfo_ismapped():
                self.hide_overlays()

    # ---------- Problem melden (Notfall): Aufgabe fehlt / nicht lösbar + Nummer + optional Buchstabe ----------

    def build_report(self):
        f = self.frames["report"]
        self.label(f, self.f_medium, text="Problem melden").place(relx=0.5, rely=0.07, anchor="center")
        self.report_kind_buttons = {}
        for i, (kind, text) in enumerate((("missing", "Aufgabe fehlt"), ("unsolvable", "Nicht lösbar"))):
            b = self.button(f, text, GREY, self.f_info_bold, lambda k=kind: self.set_report_kind(k))
            b.place(relx=(0.27, 0.73)[i], rely=0.21, relwidth=0.44, relheight=0.14, anchor="center")
            self.report_kind_buttons[kind] = b
        kw = dict(repeatdelay=400, repeatinterval=120)  # gedrückt halten = schnell zählen
        self.report_values = {}
        for row, (name, key, change) in enumerate((("Aufgabe", "task", self.change_report_task),
                                                   ("Buchstabe", "letter", self.change_report_letter))):
            y = 0.42 + row * 0.19
            self.label(f, self.f_info_bold, fg=MUTED, text=name).place(relx=0.17, rely=y, anchor="center")
            self.button(f, "−", GREY, self.f_medium, lambda c=change: c(-1), **kw).place(
                relx=0.44, rely=y, relwidth=0.15, relheight=0.15, anchor="center")
            value = self.label(f, self.f_medium, fg=ACCENT)
            value.place(relx=0.63, rely=y, anchor="center")
            self.report_values[key] = value
            self.button(f, "+", GREY, self.f_medium, lambda c=change: c(+1), **kw).place(
                relx=0.82, rely=y, relwidth=0.15, relheight=0.15, anchor="center")
        self.report_status = self.label(f, self.f_info, fg=MUTED)
        self.report_status.place(relx=0.5, rely=0.745, anchor="center")
        self.button(f, "Abbrechen", GREY, self.f_button_small, self.show_images).place(
            relx=0.27, rely=0.885, relwidth=0.44, relheight=0.17, anchor="center")
        self.button(f, "Senden", ACCENT, self.f_button_small, self.send_report).place(
            relx=0.73, rely=0.885, relwidth=0.44, relheight=0.17, anchor="center")

    def report_label(self):
        letter = LETTERS[self.report_letter] if self.report_letter else ""
        return f"Aufgabe {self.report_task}{letter}"

    def show_report(self):
        self.hide_overlays()
        self.report_kind = None
        self.report_letter = 0
        self.report_task = max(1, min(self.report_task, self.max_task()))
        self.set_report_kind(None)
        self.update_report_values()
        self.report_status.config(text="Was ist los?", fg=MUTED)
        self.show("report")

    def max_task(self):
        return self.session.tasks if self.session else TASKS_MAX

    def set_report_kind(self, kind):
        self.report_kind = kind
        for k, b in self.report_kind_buttons.items():
            color = ACCENT if k == kind else GREY
            b.config(bg=color, activebackground=color)
        if kind:
            self.report_status.config(text=f"{self.report_label()} {REPORT_TEXT[kind]}", fg=FG)

    def change_report_task(self, delta):
        self.report_task = max(1, min(self.max_task(), self.report_task + delta))
        self.update_report_values()

    def change_report_letter(self, delta):
        self.report_letter = max(0, min(len(LETTERS) - 1, self.report_letter + delta))
        self.update_report_values()

    def update_report_values(self):
        self.report_values["task"].config(text=str(self.report_task))
        self.report_values["letter"].config(text=LETTERS[self.report_letter])
        if self.report_kind:
            self.report_status.config(text=f"{self.report_label()} {REPORT_TEXT[self.report_kind]}", fg=FG)

    def send_report(self):
        if self.report_busy or not self.backchannel:
            return
        if not self.report_kind:
            self.report_status.config(text="Bitte zuerst wählen: Aufgabe fehlt oder Nicht lösbar", fg=ACCENT)
            return
        self.report_busy = True
        self.report_status.config(text="Sende …", fg=MUTED)
        kind, task = self.report_kind, self.report_task
        letter = LETTERS[self.report_letter] if self.report_letter else ""
        text = f"{self.report_label()} {REPORT_TEXT[kind]}"

        def worker():
            ok = self.backchannel.report(kind, task, letter)
            self.events.put(("report_sent", ok, text))

        threading.Thread(target=worker, daemon=True).start()

    # ---------- Gespeicherte Bilder: alle je empfangenen Bilder am Stück (übersteht Neustarts) ----------

    def show_archive(self):
        self.hide_overlays()
        self.archive_images = pb.received_images()
        if not self.archive_images:
            self.show_message("Gespeicherte Bilder", "Noch keine Bilder von der Spielleitung empfangen",
                              "Zurück", self.leave_archive)
            return
        self.viewer_mode = "archive"
        self.archive_index = len(self.archive_images) - 1  # mit dem neuesten Bild beginnen
        self.update_mail_button()
        self.render_image()
        self.show("viewer")

    def leave_archive(self):
        self.hide_overlays()
        self.viewer_mode = "live"
        self.show_setup()

    # ---------- Aktionen ----------

    def start_round(self):
        if self.busy:
            return
        self.session = pb.Session(self.tasks)
        self.review = None
        self.images = []
        self.image_index = 0
        self.last_upload = None
        self.chat = None
        self.update_chat()
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
        self.hide_overlays()
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
            path, caption, upload, help_image = args
            self.images.append((path, caption, help_image))
            if upload != self.last_upload:  # neue Sendung: direkt ihr erstes Bild zeigen
                self.last_upload = upload
                self.image_index = len(self.images) - 1
            live_viewer = self.screen == "viewer" and self.viewer_mode == "live"
            if self.screen == "wait_images" or live_viewer:
                self.show_images()
            elif self.screen == "confirm":
                self.show_confirm()
        elif kind == "message":
            data = args[0]
            self.chat = data if data["text"] else None  # leerer Text = Spielleitung hat die Nachricht entfernt
            self.chat_status = "Antworte mit einem Knopf – Neustart gedrückt halten"
            self.chat_answered = False
            self.update_chat()
            if self.screen == "wait_images":
                self.show_wait_images()
            elif self.chat and self.screen == "viewer" and self.viewer_mode == "live":
                self.place_overlay(self.chat_overlay, 1.0, 1.0)  # neue Nachricht sofort zeigen
        elif kind == "report_sent":
            ok, text = args
            self.report_busy = False
            if ok:
                self.show_message("Gemeldet ✓", f"{text} – die Spielleitung wurde informiert und kann dir ein "
                                  "Hilfe-Bild schicken (rot umrandet)", "Zurück zu den Bildern", self.show_images)
            elif self.screen == "report":
                self.report_status.config(text="Senden fehlgeschlagen – bitte nochmal", fg=ACCENT)
        elif kind == "archive_cleared":
            # Spielleitung hat alle gespeicherten Bilder gelöscht – auch die der laufenden Runde
            self.images = []
            self.image_index = 0
            self.archive_images = []
            if self.screen == "viewer":
                self.hide_overlays()
                if self.viewer_mode == "archive":
                    self.show_message("Gespeicherte Bilder", "Die Spielleitung hat alle Bilder gelöscht",
                                      "Zurück", self.leave_archive)
                else:
                    self.show_images()
            elif self.screen == "confirm":
                self.show_confirm()
        elif kind == "reply_sent":
            answer, ok, at = args
            if ok:
                self.chat_answered = True
                self.chat_status = f"Gesendet: {answer} · {at}"
            else:
                self.chat_status = f"„{answer}“ nicht gesendet – bitte nochmal drücken"
            self.update_chat()

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
