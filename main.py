"""
main.py
RezeptCheck – Hauptprogramm
UI via Browser (kein tkinter), funktioniert auf allen macOS-Versionen.
"""

from __future__ import annotations
import json
import logging
import os
import subprocess
import sys
import threading
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from core.ocr import scan_und_extrahiere
from core.rules_engine import pruefe_rezept, Pruefergebnis
from core.ampel import zeige_farbe, blinke, ampel_aus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(ROOT / "rezeptcheck.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Globaler State
status_data = {"status": "BEREIT", "haupttext": "Rezept auflegen, dann scannen", "zeilen": []}
scan_running = False


def pruefe_updates():
    try:
        r = subprocess.run(["git", "pull", "--quiet"], cwd=ROOT, capture_output=True, text=True, timeout=10)
        if "Already up to date" not in r.stdout:
            logger.info(f"Update: {r.stdout.strip()}")
    except Exception as e:
        logger.warning(f"Update-Check fehlgeschlagen: {e}")


def starte_scan(bild_pfad=None):
    global status_data, scan_running
    if scan_running:
        return
    scan_running = True
    status_data = {"status": "SCANNEN", "haupttext": "Scannen...", "zeilen": []}

    def worker():
        global status_data, scan_running
        try:
            daten = scan_und_extrahiere(bild_pfad)
            if not daten:
                status_data = {"status": "ROT", "haupttext": "Systemfehler", "zeilen": ["Scanner nicht erreichbar oder OCR fehlgeschlagen."]}
                zeige_farbe("ROT")
                return
            ergebnis = pruefe_rezept(daten)
            if ergebnis.status == "GRUEN":
                status_data = {"status": "GRUEN", "haupttext": "Rezept korrekt ✓", "zeilen": []}
                blinke("GRUEN", 2); zeige_farbe("GRUEN")
            elif ergebnis.status == "GELB":
                status_data = {"status": "GELB", "haupttext": f"{len(ergebnis.warnungen)} Hinweis(e)", "zeilen": ergebnis.warnungen}
                blinke("GELB", 2); zeige_farbe("GELB")
            else:
                zeilen = ergebnis.fehler + [f"⚠ {w}" for w in ergebnis.warnungen]
                status_data = {"status": "ROT", "haupttext": f"{len(ergebnis.fehler)} Fehler gefunden", "zeilen": zeilen}
                blinke("ROT", 3); zeige_farbe("ROT")
        except Exception as e:
            logger.exception(e)
            status_data = {"status": "ROT", "haupttext": "Systemfehler", "zeilen": [str(e)]}
        finally:
            scan_running = False

    threading.Thread(target=worker, daemon=True).start()


HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="2">
<title>RezeptCheck – Muster 13</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family: -apple-system, sans-serif; min-height:100vh; display:flex; flex-direction:column; align-items:center; justify-content:center; transition: background 0.4s; }
body.BEREIT  { background:#1e1e2e; color:#aaa; }
body.SCANNEN { background:#0a3a6e; color:#fff; }
body.GRUEN   { background:#1a6b1a; color:#fff; }
body.GELB    { background:#7a5a00; color:#fff; }
body.ROT     { background:#8a1010; color:#fff; }
.symbol { font-size: 96px; margin-bottom: 16px; }
.haupttext { font-size: 32px; font-weight: bold; margin-bottom: 32px; text-align:center; }
.fehler-liste { list-style:none; max-width:700px; width:100%; }
.fehler-liste li { background: rgba(0,0,0,0.25); border-radius:8px; padding:14px 20px; margin:8px 0; font-size:18px; }
.scan-btn { margin-top:40px; padding:18px 48px; font-size:22px; font-weight:bold; background:#0055cc; color:#fff; border:none; border-radius:12px; cursor:pointer; }
.scan-btn:hover { background:#0077ee; }
.scan-btn:disabled { background:#444; cursor:not-allowed; }
</style>
</head>
<body class="STATUS_CLASS">
<div class="symbol">SYMBOL</div>
<div class="haupttext">HAUPTTEXT</div>
<ul class="fehler-liste">FEHLER_ITEMS</ul>
<button class="scan-btn" onclick="scan()" DISABLED>Rezept scannen</button>
<script>
function scan() {
  fetch('/scan').then(() => {});
}
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # kein Logging für HTTP-Requests

    def do_GET(self):
        if self.path == "/scan":
            starte_scan()
            self.send_response(200)
            self.end_headers()
            return

        if self.path == "/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(status_data).encode())
            return

        # Hauptseite
        s = status_data
        symbole = {"GRUEN": "✓", "GELB": "⚠", "ROT": "✗", "BEREIT": "◎", "SCANNEN": "⟳"}
        symbol = symbole.get(s["status"], "◎")
        items = "".join(f"<li>{z}</li>" for z in s["zeilen"])
        disabled = "disabled" if scan_running else ""
        html = (HTML
            .replace("STATUS_CLASS", s["status"])
            .replace("SYMBOL", symbol)
            .replace("HAUPTTEXT", s["haupttext"])
            .replace("FEHLER_ITEMS", items)
            .replace("DISABLED", disabled))

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())


def main():
    ampel_aus()
    threading.Thread(target=pruefe_updates, daemon=True).start()

    port = 8765
    server = HTTPServer(("127.0.0.1", port), Handler)
    logger.info(f"RezeptCheck läuft auf http://127.0.0.1:{port}")

    # Testmodus
    if len(sys.argv) > 1:
        threading.Thread(target=lambda: starte_scan(sys.argv[1]), daemon=True).start()

    webbrowser.open(f"http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
