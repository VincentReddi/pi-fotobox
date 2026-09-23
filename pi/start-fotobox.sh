#!/bin/sh
# Startet die Fotobox-Oberfläche (wird vom Autostart aufgerufen, siehe fotobox-gui.desktop).
# Stürzt sie ab, startet sie nach 5 s neu. Absichtlich beenden (⏻ 3 s halten oder Esc) = bleibt aus.
# Nach einem Update per SSH:  pkill -f gui.py   -> startet nach 5 s mit dem neuen Code neu.
# Ganz anhalten per SSH:      pkill -f start-fotobox.sh; pkill -f gui.py

cd "$(dirname "$0")" || exit 1
export DISPLAY="${DISPLAY:-:0}"

sleep 6  # Desktop, Display-Drehung und Touch-Einstellung (display-setup.sh) zuerst fertig werden lassen

# Bildschirm nie abschalten (sonst wird das Display nach ein paar Minuten schwarz)
xset s off 2>/dev/null
xset s noblank 2>/dev/null
xset -dpms 2>/dev/null

while true; do
  # Log nicht endlos wachsen lassen: über 1 MB -> nur die letzten 200 KB behalten
  if [ -f gui.log ] && [ "$(wc -c < gui.log)" -gt 1000000 ]; then
    tail -c 200000 gui.log > gui.log.tmp && mv gui.log.tmp gui.log
  fi
  echo "=== Start $(date '+%d.%m.%Y %H:%M:%S') ===" >> gui.log
  python3 -s gui.py >> gui.log 2>&1
  code=$?
  if [ "$code" -eq 0 ]; then
    echo "=== absichtlich beendet ===" >> gui.log
    break
  fi
  echo "=== beendet mit Code $code – Neustart in 5 s ===" >> gui.log
  sleep 5
done
