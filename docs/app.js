// Leer lassen = automatisch aus der GitHub-Pages-URL (https://<owner>.github.io/<repo>/) erkennen.
// Zum lokalen Testen: index.html?owner=NAME&repo=REPO (optional &topic=… für ein Test-Thema)
const CONFIG = {
  owner: "",
  repo: "",
  branch: "photos",
  ntfyTopic: "pi-fotobox-f5849b5b30bccc45", // muss zu "ntfy_topic" in pi/photobooth.py passen
  ntfy: "https://ntfy.sh",
  imageMaxSide: 1600, // Bilder an die Fotobox bleiben im Originalformat, längste Seite höchstens so groß
  maxUploadBytes: 1_900_000, // ntfy.sh erlaubt ohne Konto 2 MB pro Anhang
};

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);

function detectRepo() {
  CONFIG.owner = params.get("owner") || CONFIG.owner;
  CONFIG.repo = params.get("repo") || CONFIG.repo;
  CONFIG.ntfyTopic = params.get("topic") || CONFIG.ntfyTopic;
  if (!CONFIG.owner && location.hostname.endsWith(".github.io")) {
    CONFIG.owner = location.hostname.split(".")[0];
  }
  if (!CONFIG.repo && CONFIG.owner) {
    const first = location.pathname.split("/").filter(Boolean)[0];
    CONFIG.repo = first && !first.includes(".") ? first : `${CONFIG.owner}.github.io`;
  }
}

const photoUrl = (path) =>
  `https://raw.githubusercontent.com/${CONFIG.owner}/${CONFIG.repo}/${CONFIG.branch}/${path}`;

let status = null;
let shownSession = null;
let lastCountdownValue = null;

// ---------------------------------------------------------------- Live-Status vom Pi

const isSmallInt = (n) => Number.isInteger(n) && n >= 0 && n < 1000;

// ntfy-Themen sind öffentlich – nur gültig aussehende Daten übernehmen
function isValidStatus(s) {
  return (
    s && typeof s.session === "string" && /^[\w-]+$/.test(s.session) &&
    Array.isArray(s.photos) &&
    s.photos.every((p) => typeof p === "string" && /^sessions\/[\w-]+\/\d{3}\.jpg$/.test(p)) &&
    (s.tasks === undefined || isSmallInt(s.tasks)) &&
    (s.batch_starts === undefined || (Array.isArray(s.batch_starts) && s.batch_starts.every(isSmallInt))) &&
    (s.review == null || (Array.isArray(s.review.missing) && s.review.missing.every(isSmallInt) &&
      (s.review.reasons == null || typeof s.review.reasons === "object")))
  );
}

function applyStatus(s) {
  if (!isValidStatus(s)) return;
  status = s;
  render();
}

// Live-Kanal: der Pi schickt jeden Statuswechsel an ntfy.sh, die Seite bekommt ihn per Server-Sent Events.
// since=latest liefert beim (Wieder-)Verbinden sofort den letzten Stand (ntfy.sh speichert 12 h).
function connectLive() {
  const es = new EventSource(`${CONFIG.ntfy}/${CONFIG.ntfyTopic}/sse?since=latest`);
  es.onmessage = (e) => {
    try {
      const msg = JSON.parse(e.data);
      if (msg.event === "message") applyStatus(JSON.parse(msg.message));
    } catch {
      // fremde oder kaputte Nachricht ignorieren
    }
  };
  es.onerror = () => {
    if (!status) setMessage("Verbinde …"); // EventSource verbindet sich selbst neu
  };
}

// Fallback für ältere Sessions (> 12 h): einmalig status.json aus dem Branch "photos" lesen.
// Nur eine Anfrage pro Seitenaufruf – bleibt weit unter GitHubs 60 Anfragen/Stunde.
async function loadArchive() {
  if (status) return;
  try {
    const res = await fetch(
      `https://api.github.com/repos/${CONFIG.owner}/${CONFIG.repo}/contents/status.json?ref=${CONFIG.branch}`
    );
    if (res.ok && !status) {
      const json = await res.json();
      const bytes = Uint8Array.from(atob(json.content.replace(/\n/g, "")), (c) => c.charCodeAt(0));
      applyStatus(JSON.parse(new TextDecoder().decode(bytes)));
    }
  } catch {
    // egal – dann eben nur live
  }
  if (!status) setMessage("Noch keine Aufnahmen – drück den Knopf an der Fotobox!");
}

function setMessage(text) {
  $("message").textContent = text;
}

function render() {
  if (!status || !status.session) return;

  if (status.session !== shownSession) {
    shownSession = status.session;
    checked = new Set();
    reasons = {};
    lastReviewKey = null;
    loadRotation(status.session);
    $("rotate-all").hidden = false;
    $("gallery").replaceChildren();
    $("session-title").hidden = false;
    $("session-title").textContent = `Session ${formatSession(status.session)}`;
    $("review-status").textContent = "";
    $("upload-status").textContent = "";
  }

  const have = new Set([...$("gallery").children].map((img) => img.dataset.path));
  for (const path of status.photos) {
    if (!have.has(path)) addPhoto(path);
  }
  renderTasksInfo();
  syncReview();
  renderAdmin();
  updateStage();
}

// Läuft zusätzlich jede 200 ms, damit der Countdown flüssig herunterzählt
function updateStage() {
  if (!status || !status.session) return;
  const total = status.total || 0;
  const count = status.photos.length;
  const secondsLeft = Math.ceil((status.countdown_end - Date.now()) / 1000);
  const capturing = status.state === "countdown" || status.state === "capturing";
  const inCountdown = status.state === "countdown" && secondsLeft > 0;
  const resend = (status.batch_starts || []).length > 1;

  $("live").hidden = !capturing;
  $("countdown").hidden = !inCountdown;
  $("progress").hidden = !capturing || inCountdown;
  $("bar").style.width = total ? `${(count / total) * 100}%` : "0";

  if (inCountdown) {
    if (secondsLeft !== lastCountdownValue) {
      lastCountdownValue = secondsLeft;
      const el = $("countdown");
      el.textContent = secondsLeft;
      el.classList.remove("tick");
      void el.offsetWidth; // Animation neu starten
      el.classList.add("tick");
    }
    setMessage(resend ? "Nachschub kommt – lächeln!" : "Gleich geht's los – lächeln!");
  } else if (capturing) {
    setMessage(`Aufnahme läuft · ${count} / ${total} Fotos`);
  } else if (status.state === "confirm") {
    setMessage(`${count} Fotos angekommen · die Fotobox wartet auf die Rückmeldung`);
  } else if (status.state === "images") {
    setMessage("Die Fotobox wartet auf Bilder der Spielleitung");
  } else {
    setMessage(`Fertig · ${count} Fotos`);
  }
}

function renderTasksInfo() {
  const el = $("tasks-info");
  el.hidden = !status.tasks;
  if (!status.tasks) return;
  el.replaceChildren(`${status.tasks} Aufgaben`);
  if (status.review) {
    const span = document.createElement("span");
    const missing = status.review.missing;
    span.className = missing.length ? "missing" : "ok";
    const why = status.review.reasons || {};
    span.textContent = missing.length
      ? ` · fehlt: Aufgabe ${missing.map((n) => (REASONS.includes(why[n]) ? `${n} (${why[n]})` : n)).join(", ")}`
      : " · alle Aufgaben angekommen ✓";
    el.append(span);
  }
}

const photoNumber = (path) => Number(path.match(/(\d{3})\.jpg$/)[1]);

// Fotos aus Nachschub-Serien bekommen einen grünen Rahmen
function isResent(path) {
  const starts = status.batch_starts || [];
  return starts.length > 1 && photoNumber(path) > starts[1];
}

function addPhoto(path) {
  const box = document.createElement("div");
  box.className = "photo";
  box.dataset.path = path;
  if (isResent(path)) {
    box.classList.add("resent");
    box.title = "Nachgeschickt";
  }
  const img = document.createElement("img");
  let attempt = 0;
  img.alt = `Foto ${photoNumber(path)}`;
  img.onload = () => img.classList.add("loaded");
  img.onerror = () => {
    // Direkt nach dem Upload liefert raw.githubusercontent.com manchmal kurz noch 404
    if (attempt >= 6) return;
    attempt++;
    setTimeout(() => (img.src = `${photoUrl(path)}?r=${attempt}`), 2000);
  };
  img.src = photoUrl(path);
  box.append(img);
  box.addEventListener("click", () => openLightbox(path));
  applyRotation(box, path);
  $("gallery").append(box);
}

// --- Fotos der Fotobox drehen (nur die Anzeige hier; die Dateien bleiben unverändert) ---
// "Alle drehen" gilt für die ganze Runde (z. B. Kamera quer eingebaut), ↻ in der Großansicht für ein Foto.
// Der Browser merkt sich das je Runde; neue Runden übernehmen die zuletzt gewählte Drehung für alle.
let rotation = { all: 0, photos: {} };

function loadRotation(session) {
  let saved = null;
  let fallback = 0;
  try {
    saved = JSON.parse(localStorage.getItem(`fotobox-rot-${session}`));
    fallback = Number(localStorage.getItem("fotobox-rot-default")) || 0;
  } catch {
    // ohne Speicher: dann eben ungedreht
  }
  rotation = saved && typeof saved === "object"
    ? { all: (Number(saved.all) || 0) % 4, photos: saved.photos && typeof saved.photos === "object" ? saved.photos : {} }
    : { all: fallback % 4, photos: {} };
}

function saveRotation() {
  try {
    localStorage.setItem(`fotobox-rot-${status.session}`, JSON.stringify(rotation));
    localStorage.setItem("fotobox-rot-default", String(rotation.all));
  } catch {
    // egal
  }
}

const rotOf = (path) => (rotation.all + (Number(rotation.photos[path]) || 0)) % 4;

function applyRotation(el, path) {
  el.classList.remove("r1", "r2", "r3");
  const r = rotOf(path);
  if (r) el.classList.add(`r${r}`);
}

$("rotate-all").onclick = () => {
  rotation.all = (rotation.all + 1) % 4; // immer 90° im Uhrzeigersinn
  saveRotation();
  for (const box of $("gallery").children) applyRotation(box, box.dataset.path);
  if (!$("lightbox").hidden) applyRotation($("lb-img"), lbPaths()[lbIndex]);
};

function formatSession(s) {
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})$/);
  return m ? `${m[3]}.${m[2]}.${m[1]}, ${m[4]}:${m[5]} Uhr` : s;
}

// ---------------------------------------------------------------- Spielleitung (Rückkanal zum Pi)

// Das Rückkanal-Thema wird aus dem Passwort berechnet, damit es nicht im Quelltext steht.
// Muss exakt zu back_topic() in pi/photobooth.py passen.
async function deriveBackTopic(password) {
  const data = new TextEncoder().encode(`${password}:${CONFIG.ntfyTopic}`);
  const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", data));
  const hex = [...hash].map((b) => b.toString(16).padStart(2, "0")).join("");
  return `${CONFIG.ntfyTopic}-r-${hex.slice(0, 24)}`;
}

// Richtiges Passwort = im Rückkanal liegt ein "hello" vom Pi (schickt er beim Start und alle 6 h)
async function verifyBackTopic(topic) {
  const res = await fetch(`${CONFIG.ntfy}/${topic}/json?poll=1&since=12h`);
  if (!res.ok) return false;
  return (await res.text()).split("\n").some((line) => {
    try {
      return JSON.parse(JSON.parse(line).message).type === "hello";
    } catch {
      return false;
    }
  });
}

let backTopic = null;

function loadBackTopic() {
  try {
    backTopic = localStorage.getItem(`fotobox-back-${CONFIG.ntfyTopic}`);
  } catch {
    backTopic = null;
  }
}

function setBackTopic(topic) {
  backTopic = topic;
  try {
    if (topic) localStorage.setItem(`fotobox-back-${CONFIG.ntfyTopic}`, topic);
    else localStorage.removeItem(`fotobox-back-${CONFIG.ntfyTopic}`);
  } catch {
    // ohne Speicher: gilt eben nur bis zum Neuladen
  }
}

async function publishBack(payload) {
  const res = await fetch(`${CONFIG.ntfy}/${backTopic}`, { method: "POST", body: JSON.stringify(payload) });
  if (!res.ok) throw new Error(`ntfy.sh antwortet mit ${res.status}`);
}

function renderAdmin() {
  const show = !!(backTopic && status && status.session);
  $("review-panel").hidden = !(show && status.tasks);
  $("images-panel").hidden = !show;
  if (!show) return;
  $("review-panel").classList.toggle("highlight", status.state === "confirm");
  $("images-panel").classList.toggle("highlight", status.state === "images");
  renderProblems();
  renderTaskList();
  updateUploadButton(); // die Liste selbst nicht neu bauen – sonst verliert ein gerade getipptes Namensfeld den Fokus
}

// Ohne Passwort zeigt die Seite nur die Anmeldung – erst danach wird überhaupt etwas geladen.
// (Die Fotos selbst liegen trotzdem im öffentlichen GitHub-Repo.)
function showGate() {
  $("app").hidden = true;
  $("logout-btn").hidden = true;
  $("gate").hidden = false;
  $("login-password").focus();
}

let started = false;
function startApp() {
  $("gate").hidden = true;
  $("app").hidden = false;
  $("logout-btn").hidden = false;
  if (started) return;
  started = true;
  if (!CONFIG.owner || !CONFIG.repo) {
    setMessage("Repo unbekannt – setze owner/repo in app.js oder öffne die Seite mit ?owner=…&repo=…");
    return;
  }
  connectLive();
  connectBack();
  renderChat();
  renderPresence();
  setTimeout(loadArchive, 3000);
  setInterval(updateStage, 200);
}

$("logout-btn").onclick = () => {
  if (!confirm("Abmelden? Danach ist die Seite erst wieder mit dem Passwort sichtbar.")) return;
  setBackTopic(null);
  location.reload(); // Live-Verbindung, Fotos und vorbereitete Bilder komplett verwerfen
};

$("login-form").onsubmit = async (e) => {
  e.preventDefault();
  const password = $("login-password").value.trim();
  if (!password) return;
  $("login-error").hidden = true;
  $("login-submit").disabled = true;
  $("login-submit").textContent = "Prüfe …";
  try {
    const topic = await deriveBackTopic(password);
    if (await verifyBackTopic(topic)) {
      setBackTopic(topic);
      $("login-password").value = "";
      startApp();
    } else {
      $("login-error").textContent =
        "Passwort falsch – oder die Fotobox war in den letzten 12 Stunden nicht eingeschaltet.";
      $("login-error").hidden = false;
    }
  } catch {
    $("login-error").textContent = "Keine Verbindung zu ntfy.sh – bitte nochmal versuchen.";
    $("login-error").hidden = false;
  } finally {
    $("login-submit").disabled = false;
    $("login-submit").textContent = "Anmelden";
  }
};

// --- Aufgaben prüfen ---
const REASONS = ["abgeschnitten", "zu weit weg", "zu nah", "verschwommen"]; // muss zu pi/photobooth.py passen
let checked = new Set(); // als "angekommen" markierte Aufgaben
let reasons = {}; // Aufgabe -> Begründung, warum sie fehlt (optional)
let lastReviewKey = null;

// Kommt eine Rückmeldung (auch von einem anderen Gerät der Spielleitung), die Checkliste daran angleichen
function syncReview() {
  const key = JSON.stringify(status.review);
  if (key === lastReviewKey) return;
  lastReviewKey = key;
  if (!status.review || !status.tasks) return;
  const missing = new Set(status.review.missing);
  checked = new Set(Array.from({ length: status.tasks }, (_, i) => i + 1).filter((n) => !missing.has(n)));
  reasons = {};
  for (const [n, why] of Object.entries(status.review.reasons || {})) {
    if (REASONS.includes(why)) reasons[n] = why;
  }
}

function missingTasks() {
  return Array.from({ length: status.tasks }, (_, i) => i + 1).filter((n) => !checked.has(n));
}

function renderTaskList() {
  const list = $("task-list");
  list.replaceChildren();
  for (let n = 1; n <= status.tasks; n++) {
    const b = document.createElement("button");
    b.className = `task${checked.has(n) ? " on" : ""}`;
    b.textContent = n;
    b.setAttribute("aria-pressed", checked.has(n));
    b.onclick = () => {
      if (checked.has(n)) {
        checked.delete(n);
      } else {
        checked.add(n);
        delete reasons[n]; // angekommen -> keine Begründung mehr
      }
      renderTaskList();
    };
    list.append(b);
  }
  const missing = missingTasks();
  renderReasons(missing);
  $("send-review").textContent = missing.length
    ? `Senden – fehlt: ${missing.join(", ")}`
    : "✓ Alle angekommen – senden";
  $("all-tasks").textContent = missing.length ? "Alle markieren" : "Keine markieren";
}

// Je fehlender Aufgabe optional eine Begründung (nochmal antippen = abwählen)
function renderReasons(missing) {
  const box = $("reason-list");
  box.replaceChildren();
  box.hidden = !missing.length || missing.length === status.tasks;
  if (box.hidden) return;
  box.append(Object.assign(document.createElement("p"), {
    className: "hint",
    textContent: "Warum fehlt die Aufgabe? (optional)",
  }));
  for (const n of missing) {
    const row = document.createElement("div");
    row.className = "reason-row";
    row.append(Object.assign(document.createElement("span"), { className: "reason-task", textContent: `Aufgabe ${n}` }));
    for (const why of REASONS) {
      const b = document.createElement("button");
      b.className = `reason${reasons[n] === why ? " on" : ""}`;
      b.textContent = why;
      b.onclick = () => {
        reasons[n] === why ? delete reasons[n] : (reasons[n] = why);
        renderReasons(missingTasks());
      };
      row.append(b);
    }
    box.append(row);
  }
}

$("all-tasks").onclick = () => {
  checked = missingTasks().length
    ? new Set(Array.from({ length: status.tasks }, (_, i) => i + 1))
    : new Set();
  renderTaskList();
};

$("send-review").onclick = async () => {
  const button = $("send-review");
  button.disabled = true;
  const missing = missingTasks();
  const why = Object.fromEntries(missing.filter((n) => reasons[n]).map((n) => [String(n), reasons[n]]));
  try {
    await publishBack({ type: "review", session: status.session, missing, reasons: why });
    $("review-status").textContent = "Gesendet ✓ – die Fotobox zeigt die Rückmeldung an.";
  } catch (err) {
    $("review-status").textContent = `Senden fehlgeschlagen: ${err.message}`;
  } finally {
    button.disabled = false;
  }
};

// --- Nachricht an die Fotobox (jederzeit) + Antworten OK / Egal / Neustart, Aktiv-Status, Meldungen ---
// Die Seite liest den Rückkanal mit: so sieht jedes Gerät der Spielleitung die aktuelle Nachricht und alle Antworten.
const chat = { message: null, replies: [] }; // unabhängig von der Runde: {id, text} + [{message_id, answer, at}]
const presence = { leitung: false, pi: false }; // Aktiv-Knöpfe von Spielleitung und Fotobox
const chats = {}; // session -> { problems: [{id, kind, task, letter, at}] }

function chatFor(session) {
  return (chats[session] ||= { problems: [] });
}

function renderPresence() {
  const button = $("leitung-toggle");
  button.textContent = presence.leitung ? "✓ Spielleitung aktiv" : "Spielleitung inaktiv";
  button.classList.toggle("on", presence.leitung);
  const pill = $("pi-presence");
  pill.textContent = presence.pi ? "Fotobox aktiv" : "Fotobox inaktiv";
  pill.classList.toggle("on", presence.pi);
}

$("leitung-toggle").onclick = async () => {
  const active = !presence.leitung;
  presence.leitung = active; // sofort anzeigen, das Echo aus dem Rückkanal bestätigt es
  renderPresence();
  try {
    await publishBack({ type: "presence", who: "leitung", active, at: Date.now() });
  } catch {
    presence.leitung = !active;
    renderPresence();
  }
};

function connectBack() {
  const es = new EventSource(`${CONFIG.ntfy}/${backTopic}/sse?since=12h`);
  es.onmessage = (e) => {
    let data;
    try {
      const msg = JSON.parse(e.data);
      if (msg.event !== "message") return;
      data = JSON.parse(msg.message);
    } catch {
      return;
    }
    if (data && data.type === "cleared") {
      const pending = pendingClears.get(data.request);
      if (pending) pending(data);
      return;
    }
    if (!data) return;
    // unabhängig von der Runde
    if (data.type === "presence" && (data.who === "leitung" || data.who === "pi")) {
      presence[data.who] = data.active === true;
      renderPresence();
      return;
    }
    if (data.type === "message" && typeof data.text === "string") {
      chat.message = data.text ? { id: String(data.id), text: data.text } : null;
      renderChat();
      return;
    }
    if (data.type === "reply" && ["OK", "Egal", "Neustart"].includes(data.answer)) {
      chat.replies.push({ message_id: String(data.message_id), answer: data.answer, at: Number(data.at) || Date.now() });
      renderChat();
      return;
    }
    // gehört zu einer Runde
    if (typeof data.session !== "string") return;
    if (data.type === "problem" && PROBLEM_TEXT[data.kind] && isSmallInt(data.task) &&
        /^[A-Z]?$/.test(data.letter ?? "")) {
      chatFor(data.session).problems.push({ id: String(data.id), kind: data.kind, task: data.task,
                                            letter: data.letter || "", at: Number(data.at) || Date.now() });
      if (status && data.session === status.session) renderProblems();
    }
  };
}

// --- Notfall-Meldungen der Fotobox ("Problem melden") + Hilfe-Bild als Antwort ---
const PROBLEM_TEXT = { missing: "fehlt", unsolvable: "ist nicht lösbar" };
let helpContext = null; // gesetzt über "Hilfe-Bild schicken": neue Bilder sind dann Hilfe-Bilder mit passendem Namen

function renderProblems() {
  if (!status || !status.session) return;
  const problems = chatFor(status.session).problems;
  $("problems-panel").hidden = !problems.length;
  const list = $("problems-list");
  list.replaceChildren();
  for (const p of [...problems].reverse()) {
    const label = `Aufgabe ${p.task}${p.letter}`;
    const row = document.createElement("div");
    row.className = "problem";
    const what = Object.assign(document.createElement("span"), {
      className: "what",
      textContent: `⚠ ${label} ${PROBLEM_TEXT[p.kind]}`,
    });
    const when = Object.assign(document.createElement("span"), { className: "when", textContent: formatTime(p.at) });
    const help = Object.assign(document.createElement("button"), {
      className: "btn danger",
      textContent: "Hilfe-Bild schicken",
    });
    help.onclick = () => {
      helpContext = { caption: `Hilfe zu ${label}` };
      $("file-input").click();
    };
    row.append(what, when, help);
    list.append(row);
  }
}

const formatTime = (ms) => new Date(ms).toLocaleTimeString("de-DE");

function renderChat() {
  const { message, replies } = chat;
  $("chat-current").hidden = !message;
  $("chat-clear").hidden = !message;
  if (message) $("chat-current-text").textContent = message.text;

  const box = $("chat-replies");
  box.replaceChildren();
  const mine = message ? replies.filter((r) => r.message_id === message.id).reverse() : [];
  box.hidden = !message;
  if (!message) return;
  if (!mine.length) {
    box.append(Object.assign(document.createElement("div"), { className: "reply", textContent: "Noch keine Antwort" }));
    return;
  }
  mine.slice(0, 8).forEach((r, i) => {
    const line = document.createElement("div");
    line.className = `reply${i === 0 ? " latest" : ""}`;
    const answer = document.createElement("b");
    answer.className = r.answer;
    answer.textContent = r.answer;
    line.append(i === 0 ? "Antwort: " : "", answer, ` · ${formatTime(r.at)}`);
    box.append(line);
  });
}

async function sendChat(text) {
  $("chat-send").disabled = $("chat-clear").disabled = true;
  try {
    await publishBack({ type: "message", session: status?.session ?? "", id: Date.now().toString(36), text });
    $("chat-status").textContent = text ? "Gesendet ✓" : "Nachricht entfernt";
    if (text) $("chat-text").value = "";
  } catch (err) {
    $("chat-status").textContent = `Senden fehlgeschlagen: ${err.message}`;
  } finally {
    $("chat-send").disabled = $("chat-clear").disabled = false;
  }
}

$("chat-send").onclick = () => {
  const text = $("chat-text").value.trim();
  if (text) sendChat(text);
};
$("chat-clear").onclick = () => sendChat("");

// --- Gespeicherte Bilder auf der Fotobox löschen (eigenes Lösch-Passwort) ---
// Das Passwort steht nur in der config.json auf dem Pi und wird auch dort geprüft – hier im öffentlichen
// Code steht es nicht. Über ntfy.sh geht nur ein Hash. Muss zu clear_hash() in pi/photobooth.py passen.
const pendingClears = new Map(); // Anfrage-ID -> Callback für die Antwort des Pi

async function clearHash(password) {
  const data = new TextEncoder().encode(`${password}:${CONFIG.ntfyTopic}:clear`);
  const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", data));
  return [...hash].map((b) => b.toString(16).padStart(2, "0")).join("");
}

$("clear-archive").onclick = () => {
  $("clear-error").hidden = true;
  $("clear-password").value = "";
  $("clear-dialog").showModal();
};
$("clear-cancel").onclick = () => $("clear-dialog").close();

$("clear-form").onsubmit = async (e) => {
  e.preventDefault();
  const password = $("clear-password").value.trim();
  if (!password) return;
  const request = Date.now().toString(36);
  $("clear-submit").disabled = true;
  $("clear-submit").textContent = "Lösche …";
  const answer = new Promise((resolve) => {
    pendingClears.set(request, resolve);
    setTimeout(() => resolve(null), 15000); // keine Antwort -> Fotobox aus?
  });
  try {
    await publishBack({ type: "clear_archive", request, hash: await clearHash(password) });
    const result = await answer;
    if (!result) {
      $("clear-error").textContent = "Keine Antwort von der Fotobox – ist sie eingeschaltet?";
      $("clear-error").hidden = false;
    } else if (!result.ok) {
      $("clear-error").textContent = result.reason || "Löschen abgelehnt";
      $("clear-error").hidden = false;
    } else {
      $("clear-dialog").close();
      const n = Number(result.count) || 0;
      $("clear-status").textContent = `${n} ${n === 1 ? "Bild" : "Bilder"} auf der Fotobox gelöscht ✓`;
    }
  } catch (err) {
    $("clear-error").textContent = `Senden fehlgeschlagen: ${err.message}`;
    $("clear-error").hidden = false;
  } finally {
    pendingClears.delete(request);
    $("clear-submit").disabled = false;
    $("clear-submit").textContent = "Löschen";
  }
};

// --- Bilder an die Fotobox schicken: ganz normaler Upload, ohne Zuschneiden ---
// Bilder bleiben im Originalformat. Nur die Dateigröße wird angepasst (längste Seite max. 1600 px, JPEG),
// weil ntfy.sh ohne Konto höchstens 2 MB pro Bild annimmt.
let prepared = []; // { caption, help, blob, url }
let uploading = false;

// Datei laden und auf Upload-Größe bringen (längste Seite max. imageMaxSide) – ohne Zuschneiden.
async function loadSource(file) {
  const url = URL.createObjectURL(file);
  try {
    const img = new Image();
    img.src = url;
    await img.decode(); // berücksichtigt die EXIF-Drehung von Handyfotos
    const scale = Math.min(1, CONFIG.imageMaxSide / Math.max(img.naturalWidth, img.naturalHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(img.naturalWidth * scale);
    canvas.height = Math.round(img.naturalHeight * scale);
    const ctx = canvas.getContext("2d");
    ctx.fillStyle = "#fff"; // durchsichtige Bereiche (PNG) werden weiß statt schwarz
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.imageSmoothingQuality = "high";
    ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
    return canvas;
  } finally {
    URL.revokeObjectURL(url);
  }
}

// Upload-Datei (JPEG, höchstens maxUploadBytes) aus dem vorbereiteten Bild erzeugen
async function encodeItem(item) {
  let blob = null;
  for (const quality of [0.9, 0.8, 0.65, 0.5]) {
    blob = await new Promise((resolve) => item.source.toBlob(resolve, "image/jpeg", quality));
    if (blob.size <= CONFIG.maxUploadBytes) break;
  }
  item.blob = blob;
  item.url = URL.createObjectURL(blob);
  delete item.source; // wird nicht mehr gebraucht
}

// Bilder nacheinander vorbereiten – egal ob ausgewählt oder per Strg+V eingefügt
async function addFiles(files, context = null) {
  for (const file of files) {
    let source;
    try {
      source = await loadSource(file);
    } catch {
      $("upload-status").textContent = `„${file.name}“ kann dieser Browser nicht öffnen.`;
      continue;
    }
    const item = { source, caption: context ? context.caption : "", help: !!context, blob: null, url: null };
    await encodeItem(item);
    prepared.push(item);
    renderPrepared();
  }
  // Fokus ins Namensfeld des neuesten Bildes, damit man direkt tippen kann
  const inputs = $("prepared").querySelectorAll("input[type=text]");
  if (inputs.length) inputs[inputs.length - 1].focus();
}

$("file-input").addEventListener("change", (e) => {
  const files = [...e.target.files];
  e.target.value = ""; // dieselbe Datei später nochmal wählbar
  addFiles(files, helpContext);
  helpContext = null;
});
$("file-input").addEventListener("cancel", () => (helpContext = null)); // Auswahl abgebrochen

// Strg+V: kopiertes Bild (Screenshot, "Bild kopieren" im Browser, …) direkt übernehmen
document.addEventListener("paste", (e) => {
  if ($("images-panel").hidden || uploading) return;
  const files = [...(e.clipboardData?.files || [])].filter((f) => f.type.startsWith("image/"));
  if (!files.length) return; // Text normal ins Eingabefeld einfügen lassen
  e.preventDefault();
  addFiles(files);
});

function renderPrepared() {
  const box = $("prepared");
  box.replaceChildren();
  prepared.forEach((item, i) => {
    const card = document.createElement("div");
    card.className = `item${item.help ? " help" : ""}`;
    if (item.help) card.append(Object.assign(document.createElement("span"), { className: "help-tag", textContent: "Hilfe-Bild" }));
    const img = document.createElement("img");
    img.src = item.url;
    img.alt = item.caption || `Bild ${i + 1}`;
    const fields = document.createElement("div");
    fields.className = "fields";
    const caption = document.createElement("input");
    caption.type = "text";
    caption.maxLength = 80;
    caption.placeholder = "Name des Bildes";
    caption.value = item.caption;
    caption.oninput = () => (item.caption = caption.value);
    const remove = document.createElement("button");
    remove.className = "remove";
    remove.textContent = "✕";
    remove.title = "Entfernen";
    remove.onclick = () => {
      if (uploading) return;
      URL.revokeObjectURL(item.url);
      prepared = prepared.filter((p) => p !== item);
      renderPrepared();
    };
    fields.append(caption, remove);
    const help = document.createElement("label");
    help.className = "check item-help";
    const box2 = Object.assign(document.createElement("input"), { type: "checkbox", checked: item.help });
    box2.onchange = () => {
      item.help = box2.checked;
      card.classList.toggle("help", item.help);
      renderPrepared();
    };
    help.append(box2, " Hilfe-Bild (rot umrandet)");
    card.append(img, fields, help);
    box.append(card);
  });
  updateUploadButton();
}

function updateUploadButton() {
  const button = $("upload-images");
  button.hidden = !prepared.length;
  button.disabled = uploading || !status || !status.session;
  button.textContent = `${prepared.length} ${prepared.length === 1 ? "Bild" : "Bilder"} an die Fotobox schicken`;
}

$("upload-images").onclick = async () => {
  if (uploading || !prepared.length) return;
  uploading = true;
  renderPrepared();
  const upload = Date.now().toString(36);
  const total = prepared.length;
  let sent = 0;
  try {
    while (prepared.length) {
      const item = prepared[0];
      $("upload-status").textContent = `Sende Bild ${sent + 1} von ${total} …`;
      const meta = {
        type: "image",
        session: status.session,
        caption: item.caption.trim(),
        help: !!item.help, // auf der Fotobox rot umrandet
        upload,
        index: sent + 1,
        count: total,
      };
      const url = `${CONFIG.ntfy}/${backTopic}?m=${encodeURIComponent(JSON.stringify(meta))}&filename=bild-${sent + 1}.jpg`;
      const res = await fetch(url, { method: "PUT", body: item.blob, headers: { "Content-Type": "image/jpeg" } });
      if (!res.ok) {
        throw new Error(
          res.status === 413 ? "Bild zu groß" :
          res.status === 429 ? "Upload-Limit von ntfy.sh erreicht – später nochmal versuchen" :
          `ntfy.sh antwortet mit ${res.status}`
        );
      }
      sent++;
      URL.revokeObjectURL(item.url);
      prepared.shift();
      renderPrepared();
    }
    $("upload-status").textContent = `${sent} ${sent === 1 ? "Bild" : "Bilder"} gesendet ✓`;
  } catch (err) {
    $("upload-status").textContent = `${sent} von ${total} gesendet – Fehler: ${err.message}`;
  } finally {
    uploading = false;
    renderPrepared();
  }
};

// ---------------------------------------------------------------- Lightbox
let lbIndex = 0;

function lbPaths() {
  return [...$("gallery").children].map((img) => img.dataset.path);
}

function openLightbox(path) {
  lbIndex = lbPaths().indexOf(path);
  showLightbox();
  $("lightbox").hidden = false;
}

function showLightbox() {
  const paths = lbPaths();
  if (!paths.length) return;
  lbIndex = (lbIndex + paths.length) % paths.length;
  $("lb-img").src = photoUrl(paths[lbIndex]);
  applyRotation($("lb-img"), paths[lbIndex]);
}

// ↻ in der Großansicht: nur dieses Foto um 90° im Uhrzeigersinn drehen
$("lb-rotate").onclick = (e) => {
  e.stopPropagation();
  const path = lbPaths()[lbIndex];
  rotation.photos[path] = ((Number(rotation.photos[path]) || 0) + 1) % 4;
  saveRotation();
  applyRotation($("lb-img"), path);
  const box = [...$("gallery").children].find((b) => b.dataset.path === path);
  if (box) applyRotation(box, path);
};

$("lb-close").onclick = () => ($("lightbox").hidden = true);
$("lb-prev").onclick = () => { lbIndex--; showLightbox(); };
$("lb-next").onclick = () => { lbIndex++; showLightbox(); };
$("lightbox").addEventListener("click", (e) => {
  if (e.target.id === "lightbox") $("lightbox").hidden = true;
});
document.addEventListener("keydown", (e) => {
  if ($("lightbox").hidden) return;
  if (e.key === "Escape") $("lightbox").hidden = true;
  if (e.key === "ArrowLeft") { lbIndex--; showLightbox(); }
  if (e.key === "ArrowRight") { lbIndex++; showLightbox(); }
  if (e.key === "r" || e.key === "R") $("lb-rotate").click();
});

// ---------------------------------------------------------------- Start
detectRepo();
loadBackTopic();
if (backTopic) startApp();
else showGate();
