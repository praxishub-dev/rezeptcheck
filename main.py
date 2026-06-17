"""
main.py - RezeptCheck
UI im Browser mit Datei-Upload, kein Terminal nach dem Start nötig.
"""

from __future__ import annotations
import json, logging, subprocess, sys, threading, webbrowser, tempfile, os
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from core.ocr import scan_und_extrahiere
from core.rules_engine import pruefe_rezept, Pruefergebnis
from core.ampel import zeige_farbe, blinke, ampel_aus

logging.basicConfig(level=logging.INFO,
    handlers=[logging.FileHandler(ROOT / "rezeptcheck.log"), logging.StreamHandler()])
logger = logging.getLogger(__name__)

state = {"status": "BEREIT", "haupttext": "Rezept hochladen oder scannen", "zeilen": [], "felder": {}}
scan_running = False

def pruefe_updates():
    try:
        subprocess.run(["git", "pull", "--quiet"], cwd=ROOT, capture_output=True, timeout=10)
    except: pass

def verarbeite_bild(bild_pfad):
    global state, scan_running
    scan_running = True
    state = {"status": "SCANNEN", "haupttext": "Wird geprüft...", "zeilen": [], "felder": {}}
    try:
        daten = scan_und_extrahiere(bild_pfad)
        if not daten:
            state = {"status": "ROT", "haupttext": "Fehler", "zeilen": ["OCR fehlgeschlagen – Bild unleserlich?"], "felder": {}}
            zeige_farbe("ROT"); return
        ergebnis = pruefe_rezept(daten)
        felder = _felder_fuer_anzeige(daten)
        if ergebnis.status == "GRUEN":
            state = {"status": "GRUEN", "haupttext": "Rezept korrekt ✓", "zeilen": [], "felder": felder}
            blinke("GRUEN", 2); zeige_farbe("GRUEN")
        elif ergebnis.status == "GELB":
            state = {"status": "GELB", "haupttext": f"{len(ergebnis.warnungen)} Hinweis(e)", "zeilen": ergebnis.warnungen, "felder": felder}
            blinke("GELB", 2); zeige_farbe("GELB")
        else:
            zeilen = ergebnis.fehler + [f"⚠ {w}" for w in ergebnis.warnungen]
            state = {"status": "ROT", "haupttext": f"{len(ergebnis.fehler)} Fehler gefunden", "zeilen": zeilen, "felder": felder}
            blinke("ROT", 3); zeige_farbe("ROT")
    except Exception as e:
        state = {"status": "ROT", "haupttext": "Systemfehler", "zeilen": [str(e)], "felder": {}}
        logger.exception(e)
    finally:
        scan_running = False


def _felder_fuer_anzeige(daten: dict) -> dict:
    """Bereitet erkannte Felder für die Transparenz-Anzeige auf."""
    labels = [
        ("krankenkasse", "Krankenkasse"),
        ("patient_name", "Name"),
        ("patient_vorname", "Vorname"),
        ("patient_geburtsdatum", "Geburtsdatum"),
        ("patient_adresse", "Adresse"),
        ("versichertennummer", "Versicherten-Nr."),
        ("status", "Status"),
        ("bsnr", "BSNR"),
        ("lanr", "Arzt-Nr. (LANR)"),
        ("ausstellungsdatum", "Ausstellungsdatum"),
        ("fachbereich", "Fachbereich"),
        ("icd10", "ICD-10"),
        ("diagnosegruppe", "Diagnosegruppe"),
        ("heilmittel", "Heilmittel"),
        ("anzahl_einheiten", "Einheiten"),
        ("frequenz", "Frequenz"),
        ("hausbesuch", "Hausbesuch"),
    ]
    out = {}
    for key, label in labels:
        out[label] = str(daten.get(key, "")).strip()
    return out

def starte_scanner():
    global state, scan_running
    if scan_running: return
    scan_running = True
    state = {"status": "SCANNEN", "haupttext": "Scanner wird ausgelöst...", "zeilen": [], "felder": {}}
    def worker():
        global scan_running
        try:
            ausgabe = tempfile.mktemp(suffix=".png")
            r = subprocess.run(
                ["scanimage", "--resolution=300", "--format=png", f"--output-file={ausgabe}"],
                capture_output=True, text=True, timeout=60
            )
            if r.returncode == 0 and Path(ausgabe).exists():
                verarbeite_bild(ausgabe)
            else:
                state.update({"status": "ROT", "haupttext": "Scanner-Fehler",
                              "zeilen": [r.stderr.strip() or "Scan fehlgeschlagen"], "felder": {}})
                scan_running = False
        except Exception as e:
            state.update({"status": "ROT", "haupttext": "Scanner nicht gefunden",
                          "zeilen": ["Scanner anschließen und erneut versuchen."], "felder": {}})
            logger.exception(e)
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
.upload-area{margin-top:24px;border:3px dashed rgba(255,255,255,.4);border-radius:16px;padding:32px 48px;text-align:center;cursor:pointer;font-size:18px;transition:border-color .2s,background .2s}
.upload-area:hover,.upload-area.dragover{border-color:rgba(255,255,255,.9);background:rgba(255,255,255,.1)}
.upload-area input{display:none}
.felder{max-width:700px;width:100%;margin-top:28px}
.felder summary{cursor:pointer;font-size:16px;opacity:.85;padding:8px 0;user-select:none}
.feld-grid{display:grid;grid-template-columns:1fr 1fr;gap:6px 20px;margin-top:12px}
.feld{display:flex;justify-content:space-between;background:rgba(0,0,0,.2);border-radius:6px;padding:8px 14px;font-size:15px}
.feld .label{opacity:.7}
.feld .wert{font-weight:600;text-align:right}
.feld.leer .wert{color:#ffb3b3;font-weight:400}
@media(max-width:600px){.feld-grid{grid-template-columns:1fr}}
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
<div class="upload-area" id="drop" onclick="document.getElementById('fi').click()">
  <input type="file" id="fi" accept="image/*,.pdf" onchange="upload(this.files[0])">
  📂 Foto/Scan hierher ziehen oder klicken<br><small style="opacity:.6">(JPG, PNG, PDF)</small>
</div>
__FELDER__
<script>
function scanner(){fetch('/scanner')}
function reset(){fetch('/reset').then(()=>location.reload())}
function upload(file){
  if(!file) return;
  var fd=new FormData();
  fd.append('file',file);
  fetch('/upload',{method:'POST',body:fd});
}
var drop=document.getElementById('drop');
['dragenter','dragover'].forEach(ev=>drop.addEventListener(ev,e=>{
  e.preventDefault();e.stopPropagation();drop.classList.add('dragover');
}));
['dragleave','drop'].forEach(ev=>drop.addEventListener(ev,e=>{
  e.preventDefault();e.stopPropagation();drop.classList.remove('dragover');
}));
drop.addEventListener('drop',e=>{
  if(e.dataTransfer.files.length) upload(e.dataTransfer.files[0]);
});
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
            state.update({"status":"BEREIT","haupttext":"Rezept hochladen oder scannen","zeilen":[],"felder":{}})
            ampel_aus()
            self.send_response(200); self.end_headers(); return

        symbole = {"GRUEN":"✓","GELB":"⚠","ROT":"✗","BEREIT":"◎","SCANNEN":"⟳"}
        items = "".join(f"<li>{z}</li>" for z in state["zeilen"])
        # Erkannte Felder (Transparenz)
        felder_html = ""
        felder = state.get("felder", {})
        if felder:
            zeilen_html = ""
            for label, wert in felder.items():
                leer = "" if wert else " leer"
                anzeige = wert if wert else "— nicht erkannt"
                zeilen_html += f'<div class="feld{leer}"><span class="label">{label}</span><span class="wert">{anzeige}</span></div>'
            felder_html = (
                '<details class="felder" open><summary>📋 Erkannte Felder (bitte „— nicht erkannt" am Rezept gegenprüfen)</summary>'
                f'<div class="feld-grid">{zeilen_html}</div></details>'
            )
        html = (HTML
            .replace("__STATUS__", state["status"])
            .replace("__SYMBOL__", symbole.get(state["status"],"◎"))
            .replace("__HAUPTTEXT__", state["haupttext"])
            .replace("__ITEMS__", items)
            .replace("__FELDER__", felder_html))
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
