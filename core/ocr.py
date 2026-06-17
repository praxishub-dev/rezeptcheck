"""
ocr.py - RezeptCheck
OCR via Tesseract (schnell, lokal, kein pyobjc nötig).
"""

from __future__ import annotations
import logging
import re
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def pdf_zu_bild(pdf_pfad: str) -> str:
    try:
        from pdf2image import convert_from_path
        bilder = convert_from_path(pdf_pfad, dpi=300, first_page=1, last_page=1)
        if bilder:
            tmp = tempfile.mktemp(suffix=".png")
            bilder[0].save(tmp, "PNG")
            return tmp
    except Exception as e:
        logger.error(f"PDF-Konvertierung: {e}")
    return pdf_pfad


def extrahiere_text(bild_pfad: str) -> str:
    try:
        r = subprocess.run(
            ["tesseract", bild_pfad, "stdout", "-l", "deu", "--oem", "1", "--psm", "6"],
            capture_output=True, text=True, timeout=20
        )
        if r.stdout.strip():
            logger.info("Tesseract OK")
            return r.stdout
        logger.warning(f"Tesseract leer: {r.stderr[:100]}")
    except FileNotFoundError:
        logger.error("Tesseract nicht installiert – brew install tesseract")
    except Exception as e:
        logger.error(f"Tesseract Fehler: {e}")
    return ""


def extrahiere_felder(volltext: str) -> dict:
    zeilen = [z.strip() for z in volltext.split("\n") if z.strip()]
    daten = {}

    def suche(varianten):
        for i, zeile in enumerate(zeilen):
            for v in varianten:
                if v.lower() in zeile.lower():
                    if ":" in zeile:
                        wert = zeile.split(":", 1)[1].strip()
                        if wert: return wert
                    if i + 1 < len(zeilen):
                        return zeilen[i + 1].strip()
        return ""

    daten["krankenkasse"]         = suche(["krankenkasse", "kostenträger"])
    daten["patient_name"]         = suche(["name, vorname", "familienname", "name des"])
    daten["patient_vorname"]      = suche(["vorname"])
    daten["patient_adresse"]      = suche(["str.", "straße", "weg ", "allee"])
    daten["versichertennummer"]   = suche(["versicherten-nr", "versichertennr"])
    daten["kostentraegerkennung"] = suche(["kostenträgerkennung"])
    daten["status"]               = suche(["status"])

    # Geburtsdatum – erstes Datum im Text
    alle_daten = re.findall(r'\b(\d{2}\.\d{2}\.\d{2,4})\b', volltext)
    daten["patient_geburtsdatum"] = alle_daten[0] if alle_daten else ""
    daten["ausstellungsdatum"]    = alle_daten[-1] if len(alle_daten) > 1 else ""

    # BSNR + LANR
    bsnr = re.search(r'(?:Betriebsst[äa]tten|BSNR)[^0-9]*(\d{9})', volltext, re.IGNORECASE)
    daten["bsnr"] = bsnr.group(1) if bsnr else ""
    lanr = re.search(r'(?:Arzt-Nr|LANR)[^0-9]*(\d{9})', volltext, re.IGNORECASE)
    daten["lanr"] = lanr.group(1) if lanr else ""

    # Stempel
    daten["unterschrift"] = "vorhanden" if re.search(r'unterschrift|vertragsarzt', volltext, re.IGNORECASE) else ""
    daten["arzt_name"]    = suche(["dres.", "dr. med", "gemeinschaftspraxis", "praxis"])
    daten["arzt_beruf"]   = suche(["facharzt", "allgemeinmedizin", "orthopädie", "neurologie"])
    daten["arzt_strasse"] = suche(["lange str", "hauptstr", "bahnhof"])
    daten["arzt_plz_ort"] = suche(["bückeburg", "rinteln", "minden", "lingen", "31675", "31737"])
    daten["arzt_telefon"] = suche(["tel.", "telefon", "fon"])

    # Fachbereich
    daten["fachbereich"] = ""
    if re.search(r'physiotherapie', volltext, re.IGNORECASE):
        daten["fachbereich"] = "Physiotherapie"
    elif re.search(r'ergotherapie', volltext, re.IGNORECASE):
        daten["fachbereich"] = "Ergotherapie"

    # ICD-10
    icd = re.search(r'\b([A-Z]\d{2}(?:\.\d{1,4})?)\b', volltext)
    daten["icd10"] = icd.group(1) if icd else ""

    # Diagnosegruppe
    gruppen = ["EX","WS","CS","ZN","PN","AT","GE","LY","SO1","SO2","SO3","SO4","SO5","EN1","EN2","EN3","PS1","PS2","PS3","PS4","SB1"]
    dg = re.search(r'\b(' + '|'.join(gruppen) + r')\b', volltext)
    daten["diagnosegruppe"] = dg.group(1) if dg else ""

    # Leitsymptomatik
    ls = re.search(r'[Xx✓\[X\]]\s+([abc])\b', volltext, re.IGNORECASE)
    daten["leitsymptomatik"] = ls.group(1).lower() if ls else suche(["leitsymptomatik"])

    # Heilmittel + Blanko
    if re.search(r'blanko', volltext, re.IGNORECASE):
        daten["heilmittel"] = "BLANKOVERORDNUNG"
        daten["anzahl_einheiten"] = "1"
        daten["frequenz"] = "blanko"
    else:
        daten["heilmittel"] = suche(["kg-zns","kg ","mld","mt ","bobath","vojta","heilmittel"])
        zahlen = [z for z in re.findall(r'\b(\d{1,2})\b', volltext) if 1 <= int(z) <= 60]
        daten["anzahl_einheiten"] = zahlen[-1] if zahlen else ""
        freq = re.search(r'(\d+[-–]\d*\s*x\s*w[öo]ch\.?|\d+\s*x\s*w[öo]ch\.?)', volltext, re.IGNORECASE)
        daten["frequenz"] = freq.group(1) if freq else suche(["frequenz", "wöch"])

    # Hausbesuch
    hb_nein = re.search(r'nein', volltext, re.IGNORECASE)
    hb_ja   = re.search(r'hausbesuch.*?ja', volltext, re.IGNORECASE)
    daten["hausbesuch"] = "ja" if hb_ja else ("nein" if hb_nein else "")

    # Zuzahlung – Standard zuzahlungspflichtig außer explizit befreit
    daten["zuzahlung"] = "zuzahlungsfrei" if re.search(
        r'befreit|zuzahlungsfrei|gebührenfrei', volltext, re.IGNORECASE
    ) else "zuzahlungspflichtig"

    logger.info(f"Felder: {sum(1 for v in daten.values() if v)}/{len(daten)} gefunden")
    return daten


def scan_zu_bild(aufloesung_dpi: int = 300) -> "str | None":
    try:
        ausgabe = tempfile.mktemp(suffix=".png")
        script = f'''tell application "Image Capture"
set theScanner to first device
set output file of theScanner to POSIX file "{ausgabe}"
set resolution of theScanner to {aufloesung_dpi}
scan theScanner
end tell'''
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and Path(ausgabe).exists():
            return ausgabe
    except Exception as e:
        logger.error(f"Scanner: {e}")
    return None


def scan_und_extrahiere(bild_pfad: "str | None" = None) -> dict:
    if bild_pfad is None:
        bild_pfad = scan_zu_bild()
        if bild_pfad is None:
            return {}
    if bild_pfad.lower().endswith(".pdf"):
        bild_pfad = pdf_zu_bild(bild_pfad)
    volltext = extrahiere_text(bild_pfad)
    if not volltext.strip():
        return {}
    return extrahiere_felder(volltext)
