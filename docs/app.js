// Leer lassen = automatisch aus der GitHub-Pages-URL (https://<owner>.github.io/<repo>/) erkennen.
// Zum lokalen Testen: index.html?owner=NAME&repo=REPO (optional &topic=… für ein Test-Thema)
const CONFIG = {
  owner: "",
  repo: "",
  branch: "photos",
  ntfyTopic: "pi-fotobox-f5849b5b30bccc45", // muss zu "ntfy_topic" in pi/photobooth.py passen
  ntfy: "https://ntfy.sh",
  imageWidth: 1280, // Bilder an die Fotobox: immer 16:9
  imageHeight: 720,
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
    (s.review == null || (Array.isArray(s.review.missing) && s.review.missing.every(isSmallInt)))
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
    lastReviewKey = null;
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
    span.textContent = missing.length
      ? ` · fehlt: Aufgabe ${missing.join(", ")}`
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
  const img = document.createElement("img");
  let attempt = 0;
  img.dataset.path = path;
  img.alt = `Foto ${photoNumber(path)}`;
  if (isResent(path)) {
    img.classList.add("resent");
    img.title = "Nachgeschickt";
  }
  img.onload = () => img.classList.add("loaded");
  img.onerror = () => {
    // Direkt nach dem Upload liefert raw.githubusercontent.com manchmal kurz noch 404
    if (attempt >= 6) return;
    attempt++;
    setTimeout(() => (img.src = `${photoUrl(path)}?r=${attempt}`), 2000);
  };
  img.src = photoUrl(path);
  img.addEventListener("click", () => openLightbox(path));
  $("gallery").append(img);
}

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
let checked = new Set(); // als "angekommen" markierte Aufgaben
let lastReviewKey = null;

// Kommt eine Rückmeldung (auch von einem anderen Gerät der Spielleitung), die Checkliste daran angleichen
function syncReview() {
  const key = JSON.stringify(status.review);
  if (key === lastReviewKey) return;
  lastReviewKey = key;
  if (!status.review || !status.tasks) return;
  const missing = new Set(status.review.missing);
  checked = new Set(Array.from({ length: status.tasks }, (_, i) => i + 1).filter((n) => !missing.has(n)));
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
      checked.has(n) ? checked.delete(n) : checked.add(n);
      renderTaskList();
    };
    list.append(b);
  }
  const missing = missingTasks();
  $("send-review").textContent = missing.length
    ? `Senden – fehlt: ${missing.join(", ")}`
    : "✓ Alle angekommen – senden";
  $("all-tasks").textContent = missing.length ? "Alle markieren" : "Keine markieren";
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
  try {
    await publishBack({ type: "review", session: status.session, missing: missingTasks() });
    $("review-status").textContent = "Gesendet ✓ – die Fotobox zeigt die Rückmeldung an.";
  } catch (err) {
    $("review-status").textContent = `Senden fehlgeschlagen: ${err.message}`;
  } finally {
    button.disabled = false;
  }
};

// --- Bilder vorbereiten (16:9 zuschneiden + benennen) und an die Fotobox schicken ---
let prepared = []; // { source, zoom, cx, cy, caption, blob, url }
let uploading = false;

async function loadSource(file) {
  const url = URL.createObjectURL(file);
  try {
    const img = new Image();
    img.src = url;
    await img.decode(); // berücksichtigt die EXIF-Drehung von Handyfotos
    const scale = Math.min(1, 2560 / Math.max(img.naturalWidth, img.naturalHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(img.naturalWidth * scale);
    canvas.height = Math.round(img.naturalHeight * scale);
    canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
    return canvas;
  } finally {
    URL.revokeObjectURL(url);
  }
}

// Ausschnitt: Mittelpunkt (cx, cy) im Originalbild + Zoom (1 = Bild füllt den 16:9-Rahmen gerade so)
function geometry(item, fw, fh) {
  const sw = item.source.width;
  const sh = item.source.height;
  const s = Math.max(fw / sw, fh / sh) * item.zoom;
  const halfW = fw / (2 * s);
  const halfH = fh / (2 * s);
  item.cx = Math.min(Math.max(item.cx, halfW), sw - halfW);
  item.cy = Math.min(Math.max(item.cy, halfH), sh - halfH);
  return { s, x: fw / 2 - item.cx * s, y: fh / 2 - item.cy * s, w: sw * s, h: sh * s };
}

function drawInto(canvas, item) {
  const ctx = canvas.getContext("2d");
  const g = geometry(item, canvas.width, canvas.height);
  ctx.fillStyle = "#000";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(item.source, g.x, g.y, g.w, g.h);
}

async function renderOutput(item) {
  const canvas = document.createElement("canvas");
  canvas.width = CONFIG.imageWidth;
  canvas.height = CONFIG.imageHeight;
  drawInto(canvas, item);
  let blob = null;
  for (const quality of [0.85, 0.7, 0.55]) {
    blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", quality));
    if (blob.size <= CONFIG.maxUploadBytes) break;
  }
  if (item.url) URL.revokeObjectURL(item.url);
  item.blob = blob;
  item.url = URL.createObjectURL(blob);
}

const cropper = { item: null, isNew: false, resolve: null, drag: null };
const cropCanvas = $("crop-canvas");

function openCropper(item, isNew) {
  cropper.item = item;
  cropper.isNew = isNew;
  $("crop-caption").value = item.caption;
  $("crop-zoom").value = item.zoom;
  $("crop-dialog").showModal();
  requestAnimationFrame(drawCropper);
  return new Promise((resolve) => (cropper.resolve = resolve));
}

function drawCropper() {
  if (!cropper.item) return;
  const width = Math.round(cropCanvas.clientWidth * (window.devicePixelRatio || 1));
  if (cropCanvas.width !== width) {
    cropCanvas.width = width;
    cropCanvas.height = Math.round((width * 9) / 16);
  }
  drawInto(cropCanvas, cropper.item);
}

function closeCropper(ok) {
  const { resolve } = cropper;
  cropper.item = null;
  cropper.drag = null;
  if ($("crop-dialog").open) $("crop-dialog").close();
  if (resolve) resolve(ok);
  cropper.resolve = null;
}

cropCanvas.addEventListener("pointerdown", (e) => {
  cropCanvas.setPointerCapture(e.pointerId);
  cropper.drag = { x: e.clientX, y: e.clientY };
});
cropCanvas.addEventListener("pointermove", (e) => {
  if (!cropper.drag || !cropper.item) return;
  const k = cropCanvas.width / cropCanvas.clientWidth; // CSS-Pixel -> Canvas-Pixel
  const { s } = geometry(cropper.item, cropCanvas.width, cropCanvas.height);
  cropper.item.cx -= ((e.clientX - cropper.drag.x) * k) / s;
  cropper.item.cy -= ((e.clientY - cropper.drag.y) * k) / s;
  cropper.drag = { x: e.clientX, y: e.clientY };
  drawCropper();
});
for (const type of ["pointerup", "pointercancel"]) {
  cropCanvas.addEventListener(type, () => (cropper.drag = null));
}
cropCanvas.addEventListener(
  "wheel",
  (e) => {
    if (!cropper.item) return;
    e.preventDefault();
    const zoom = cropper.item.zoom * (e.deltaY < 0 ? 1.1 : 1 / 1.1);
    cropper.item.zoom = Math.min(4, Math.max(1, zoom));
    $("crop-zoom").value = cropper.item.zoom;
    drawCropper();
  },
  { passive: false }
);
$("crop-zoom").addEventListener("input", (e) => {
  if (!cropper.item) return;
  cropper.item.zoom = Number(e.target.value);
  drawCropper();
});
window.addEventListener("resize", drawCropper);

$("crop-cancel").onclick = () => closeCropper(false);
$("crop-dialog").addEventListener("cancel", (e) => {
  e.preventDefault(); // Esc
  closeCropper(false);
});
$("crop-ok").onclick = async () => {
  const item = cropper.item;
  if (!item) return;
  item.caption = $("crop-caption").value.trim();
  $("crop-ok").disabled = true;
  await renderOutput(item);
  $("crop-ok").disabled = false;
  if (cropper.isNew) prepared.push(item);
  closeCropper(true);
  renderPrepared();
};

// Bilder nacheinander zuschneiden – egal ob ausgewählt oder per Strg+V eingefügt
const pendingFiles = [];
let processingFiles = false;

async function addFiles(files) {
  pendingFiles.push(...files);
  if (processingFiles) return; // läuft schon – neue Bilder kommen einfach hinten dran
  processingFiles = true;
  while (pendingFiles.length) {
    const file = pendingFiles.shift();
    let source;
    try {
      source = await loadSource(file);
    } catch {
      $("upload-status").textContent = `„${file.name}“ kann dieser Browser nicht öffnen.`;
      continue;
    }
    const item = { source, zoom: 1, cx: source.width / 2, cy: source.height / 2, caption: "", blob: null, url: null };
    await openCropper(item, true);
  }
  processingFiles = false;
}

$("file-input").addEventListener("change", (e) => {
  const files = [...e.target.files];
  e.target.value = ""; // dieselbe Datei später nochmal wählbar
  addFiles(files);
});

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
    card.className = "item";
    const img = document.createElement("img");
    img.src = item.url;
    img.alt = item.caption || `Bild ${i + 1}`;
    img.title = "Tippen zum Neu-Zuschneiden";
    img.onclick = () => !uploading && openCropper(item, false);
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
    card.append(img, fields);
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
}

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
});

// ---------------------------------------------------------------- Start
detectRepo();
loadBackTopic();
if (backTopic) startApp();
else showGate();
