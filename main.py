"""
main.py - RezeptCheck
UI im Browser mit Datei-Upload, kein Terminal nach dem Start nötig.
"""

from __future__ import annotations
import json, logging, subprocess, sys, threading, webbrowser, tempfile, os
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import cgi

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from core.ocr import scan_und_extrahiere
from core.rules_engine import pruefe_rezept, Pruefergebnis
from core.ampel import zeige_farbe, blinke, ampel_aus

logging.basicConfig(level=logging.INFO,
    handlers=[logging.FileHandler(ROOT / "rezeptcheck.log"), logging.StreamHandler()])
logger = logging.getLogger(__name__)

state = {"status": "BEREIT", "haupttext": "Rezept hochladen oder scannen", "zeilen": []}
scan_running = False

def pruefe_updates():
    try:
        subprocess.run(["git", "pull", "--quiet"], cwd=ROOT, capture_output=True, timeout=10)
    except: pass

def verarbeite_bild(bild_pfad):
    global state, scan_running
    scan_running = True
    state = {"status": "SCANNEN", "haupttext": "Wird geprüft...", "zeilen": []}
    try:
        daten = scan_und_extrahiere(bild_pfad)
        if not daten:
            state = {"status": "ROT", "haupttext": "Fehler", "zeilen": ["OCR fehlgeschlagen – Bild unleserlich?"]}
            zeige_farbe("ROT"); return
        ergebnis = pruefe_rezept(daten)
        if ergebnis.status == "GRUEN":
            state = {"status": "GRUEN", "haupttext": "Rezept korrekt ✓", "zeilen": []}
            blinke("GRUEN", 2); zeige_farbe("GRUEN")
        elif ergebnis.status == "GELB":
            state = {"status": "GELB", "haupttext": f"{len(ergebnis.warnungen)} Hinweis(e)", "zeilen": ergebnis.warnungen}
            blinke("GELB", 2); zeige_farbe("GELB")
        else:
            zeilen = ergebnis.fehler + [f"⚠ {w}" for w in ergebnis.warnungen]
            state = {"status": "ROT", "haupttext": f"{len(ergebnis.fehler)} Fehler gefunden", "zeilen": zeilen}
            blinke("ROT", 3); zeige_farbe("ROT")
    except Exception as e:
        state = {"status": "ROT", "haupttext": "Systemfehler", "zeilen": [str(e)]}
        logger.exception(e)
    finally:
        scan_running = False

def starte_scanner():
    global state, scan_running
    if scan_running: return
    scan_running = True
    state = {"status": "SCANNEN", "haupttext": "Scanner wird ausgelöst...", "zeilen": []}
    def worker():
        bild = None
        try:
            ausgabe = tempfile.mktemp(suffix=".tiff")
            script = f'tell application "Image Capture"\nset theScanner to first device\nset output file of theScanner to POSIX file "{ausgabe}"\nscan theScanner\nend tell'
            r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
            if r.returncode == 0 and Path(ausgabe).exists():
                bild = ausgabe
        except: pass
        if bild:
            verarbeite_bild(bild)
        else:
            global scan_running
            state["status"] = "ROT"
            state["haupttext"] = "Scanner nicht gefunden"
            state["zeilen"] = ["Scanner anschließen und erneut versuchen."]
            scan_running = False
    threading.Thread(target=worker, daemon=True).start()

HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<title>RezeptCheck – Muster 13</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,sans-serif;min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;transition:background .4s;padding:30px}
body.BEREIT{background:#1e1e2e;color:#aaa}
body.SCANNEN{background:#0a3a6e;color:#fff}
body.GRUEN{background:#1a6b1a;color:#fff}
body.GELB{background:#7a5a00;color:#fff}
body.ROT{background:#8a1010;color:#fff}
.symbol{font-size:96px;margin-bottom:16px}
.haupttext{font-size:32px;font-weight:bold;margin-bottom:32px;text-align:center}
.fehler-liste{list-style:none;max-width:700px;width:100%}
.fehler-liste li{background:rgba(0,0,0,.25);border-radius:8px;padding:14px 20px;margin:8px 0;font-size:18px}
.btns{display:flex;gap:16px;margin-top:40px;flex-wrap:wrap;justify-content:center}
.btn{padding:16px 36px;font-size:20px;font-weight:bold;border:none;border-radius:12px;cursor:pointer;color:#fff}
.btn-scan{background:#0055cc}.btn-scan:hover{background:#0077ee}
.btn-reset{background:#444}.btn-reset:hover{background:#666}
.upload-area{margin-top:24px;border:3px dashed rgba(255,255,255,.4);border-radius:16px;padding:32px 48px;text-align:center;cursor:pointer;font-size:18px;transition:border-color .2s}
.upload-area:hover{border-color:rgba(255,255,255,.8)}
.upload-area input{display:none}
</style>
</head>
<body class="__STATUS__">
<div class="symbol">__SYMBOL__</div>
<div class="haupttext">__HAUPTTEXT__</div>
<ul class="fehler-liste">__ITEMS__</ul>
<div class="btns">
  <button class="btn btn-scan" onclick="scanner()">📷 Scanner auslösen</button>
  <button class="btn btn-reset" onclick="reset()">↺ Zurücksetzen</button>
</div>
<div class="upload-area" onclick="document.getElementById('fi').click()">
  <input type="file" id="fi" accept="image/*,.pdf" onchange="upload(this)">
  📂 Foto oder Scan hochladen<br><small style="opacity:.6">(JPG, PNG, PDF)</small>
</div>
<script>
function scanner(){fetch('/scanner')}
function reset(){fetch('/reset').then(()=>location.reload())}
function upload(input){
  if(!input.files.length) return;
  var fd=new FormData();
  fd.append('file',input.files[0]);
  fetch('/upload',{method:'POST',body:fd});
}
setInterval(()=>{
  fetch('/status').then(r=>r.json()).then(d=>{
    if(d.status!=='__STATUS__') location.reload();
  });
},1500);
</script>
</body>
</html>"""

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if self.path == "/status":
            self.send_response(200)
            self.send_header("Content-Type","application/json")
            self.end_headers()
            self.wfile.write(json.dumps(state).encode())
            return
        if self.path == "/scanner":
            threading.Thread(target=starte_scanner, daemon=True).start()
            self.send_response(200); self.end_headers(); return
        if self.path == "/reset":
            state.update({"status":"BEREIT","haupttext":"Rezept hochladen oder scannen","zeilen":[]})
            ampel_aus()
            self.send_response(200); self.end_headers(); return

        symbole = {"GRUEN":"✓","GELB":"⚠","ROT":"✗","BEREIT":"◎","SCANNEN":"⟳"}
        items = "".join(f"<li>{z}</li>" for z in state["zeilen"])
        html = (HTML
            .replace("__STATUS__", state["status"])
            .replace("__SYMBOL__", symbole.get(state["status"],"◎"))
            .replace("__HAUPTTEXT__", state["haupttext"])
            .replace("__ITEMS__", items))
        self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def do_POST(self):
        if self.path == "/upload":
            ct = self.headers.get("Content-Type","")
            length = int(self.headers.get("Content-Length",0))
            body = self.rfile.read(length)
            # Boundary extrahieren
            boundary = ct.split("boundary=")[-1].encode()
            parts = body.split(b"--" + boundary)
            for part in parts:
                if b"filename=" in part and b"\r\n\r\n" in part:
                    header, data = part.split(b"\r\n\r\n", 1)
                    data = data.rstrip(b"\r\n--")
                    suffix = ".jpg"
                    if b".png" in header: suffix = ".png"
                    elif b".pdf" in header: suffix = ".pdf"
                    tmp = tempfile.mktemp(suffix=suffix)
                    with open(tmp, "wb") as f: f.write(data)
                    threading.Thread(target=verarbeite_bild, args=(tmp,), daemon=True).start()
                    break
            self.send_response(200); self.end_headers()

def main():
    ampel_aus()
    threading.Thread(target=pruefe_updates, daemon=True).start()
    port = 8765
    server = HTTPServer(("127.0.0.1", port), Handler)
    logger.info(f"RezeptCheck läuft auf http://127.0.0.1:{port}")
    webbrowser.open(f"http://127.0.0.1:{port}")
    server.serve_forever()

if __name__ == "__main__":
    main()
