// Leer lassen = automatisch aus der GitHub-Pages-URL (https://<owner>.github.io/<repo>/) erkennen.
// Zum lokalen Testen: index.html?owner=NAME&repo=REPO
const CONFIG = {
  owner: "",
  repo: "",
  branch: "photos",
  ntfyTopic: "pi-fotobox-f5849b5b30bccc45", // muss zu "ntfy_topic" in pi/photobooth.py passen
};

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);

function detectRepo() {
  CONFIG.owner = params.get("owner") || CONFIG.owner;
  CONFIG.repo = params.get("repo") || CONFIG.repo;
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

// ntfy-Themen sind öffentlich – nur gültig aussehende Daten übernehmen
function isValidStatus(s) {
  return (
    s && typeof s.session === "string" && /^[\w-]+$/.test(s.session) &&
    Array.isArray(s.photos) && s.photos.every((p) => /^sessions\/[\w-]+\/\d{3}\.jpg$/.test(p))
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
  const es = new EventSource(`https://ntfy.sh/${CONFIG.ntfyTopic}/sse?since=latest`);
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
    $("gallery").replaceChildren();
    $("session-title").hidden = false;
    $("session-title").textContent = `Session ${formatSession(status.session)}`;
  }

  const have = new Set([...$("gallery").children].map((img) => img.dataset.path));
  for (const path of status.photos) {
    if (!have.has(path)) addPhoto(path);
  }
  updateStage();
}

// Läuft zusätzlich jede 200 ms, damit der Countdown flüssig herunterzählt
function updateStage() {
  if (!status || !status.session) return;
  const total = status.total || 0;
  const count = status.photos.length;
  const secondsLeft = Math.ceil((status.countdown_end - Date.now()) / 1000);
  const inCountdown = status.state === "countdown" && secondsLeft > 0;
  const running = status.state !== "done";

  $("live").hidden = !running;
  $("countdown").hidden = !inCountdown;
  $("progress").hidden = !running || inCountdown;
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
    setMessage("Gleich geht's los – lächeln!");
  } else if (running) {
    setMessage(`Aufnahme läuft · ${count} / ${total} Fotos`);
  } else {
    setMessage(`Fertig · ${count} Fotos`);
  }
}

function addPhoto(path) {
  const img = document.createElement("img");
  let attempt = 0;
  img.dataset.path = path;
  img.alt = path.split("/").pop();
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

// --- Lightbox ---
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

// --- Start ---
detectRepo();
if (!CONFIG.owner || !CONFIG.repo) {
  setMessage("Repo unbekannt – setze owner/repo in app.js oder öffne die Seite mit ?owner=…&repo=…");
} else {
  connectLive();
  setTimeout(loadArchive, 3000);
  setInterval(updateStage, 200);
}
