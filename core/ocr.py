"""
ocr.py - RezeptCheck
OCR via Apple Vision (primär) oder Tesseract (Fallback).
PDF wird automatisch zu Bild konvertiert.
"""

from __future__ import annotations
import logging
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


def pdf_zu_bild(pdf_pfad: str) -> str:
    """Konvertiert erste PDF-Seite in TIFF."""
    try:
        from pdf2image import convert_from_path
        bilder = convert_from_path(pdf_pfad, dpi=300, first_page=1, last_page=1)
        if bilder:
            tmp = tempfile.mktemp(suffix=".tiff")
            bilder[0].save(tmp, "TIFF")
            logger.info(f"PDF konvertiert: {tmp}")
            return tmp
    except Exception as e:
        logger.error(f"PDF-Konvertierung: {e}")
    return pdf_pfad


def ocr_apple_vision(bild_pfad: str) -> str:
    """OCR via Apple Vision durch Swift-Subprocess – zuverlässig auf allen macOS-Versionen."""
    swift_code = """
import Vision
import Foundation

let url = URL(fileURLWithPath: CommandLine.arguments[1])
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.recognitionLanguages = ["de-DE", "de", "en-US"]
request.usesLanguageCorrection = true

let handler = VNImageRequestHandler(url: url, options: [:])
try? handler.perform([request])

if let results = request.results {
    for obs in results {
        if let candidate = obs.topCandidates(1).first {
            print(candidate.string)
        }
    }
}
"""
    try:
        swift_file = tempfile.mktemp(suffix=".swift")
        with open(swift_file, "w") as f:
            f.write(swift_code)
        result = subprocess.run(
            ["swift", swift_file, bild_pfad],
            capture_output=True, text=True, timeout=30
        )
        Path(swift_file).unlink(missing_ok=True)
        if result.stdout.strip():
            logger.info("Apple Vision OCR erfolgreich.")
            return result.stdout
        logger.warning(f"Apple Vision leer: {result.stderr[:200]}")
    except Exception as e:
        logger.warning(f"Apple Vision fehlgeschlagen: {e}")
    return ""


def ocr_tesseract(bild_pfad: str) -> str:
    """OCR via Tesseract als Fallback."""
    try:
        result = subprocess.run(
            ["tesseract", bild_pfad, "stdout", "-l", "deu", "--oem", "3", "--psm", "6"],
            capture_output=True, text=True, timeout=30
        )
        if result.stdout.strip():
            logger.info("Tesseract OCR erfolgreich.")
            return result.stdout
    except Exception as e:
        logger.warning(f"Tesseract fehlgeschlagen: {e}")
    return ""


def extrahiere_text(bild_pfad: str) -> str:
    """Versucht OCR – erst Apple Vision, dann Tesseract."""
    text = ocr_apple_vision(bild_pfad)
    if not text.strip():
        text = ocr_tesseract(bild_pfad)
    return text


def extrahiere_felder(volltext: str) -> dict:
    """Extrahiert Rezeptfelder aus OCR-Text."""
    import re
    zeilen = [z.strip() for z in volltext.split("\n") if z.strip()]
    daten = {}

    def suche(label_varianten, zeilen):
        for i, zeile in enumerate(zeilen):
            for v in label_varianten:
                if v.lower() in zeile.lower():
                    if ":" in zeile:
                        wert = zeile.split(":", 1)[1].strip()
                        if wert: return wert
                    if i + 1 < len(zeilen):
                        return zeilen[i + 1].strip()
        return ""

    # Patientendaten
    daten["krankenkasse"]         = suche(["krankenkasse", "kostenträger", "kasse"], zeilen)
    daten["patient_name"]         = suche(["name, vorname", "familienname"], zeilen)
    daten["patient_vorname"]      = suche(["vorname"], zeilen)
    daten["patient_adresse"]      = suche(["straße", "str.", "wilhelm", "anschrift"], zeilen)
    daten["patient_geburtsdatum"] = suche(["geb. am", "geburtsdatum", "geb.am"], zeilen)
    daten["versichertennummer"]   = suche(["versicherten-nr", "versichertennr", "versicherten-nr."], zeilen)
    daten["kostentraegerkennung"] = suche(["kostenträgerkennung", "kostenträger-ik"], zeilen)
    daten["status"]               = suche(["status"], zeilen)

    # Geburtsdatum per Regex wenn nicht gefunden
    if not daten["patient_geburtsdatum"]:
        geb = re.search(r'\b(\d{2}\.\d{2}\.\d{2,4})\b', volltext)
        if geb: daten["patient_geburtsdatum"] = geb.group(1)

    # Arztdaten
    bsnr = re.search(r'(?:Betriebsstätten-Nr|BSNR)[.:\s]*(\d{9})', volltext, re.IGNORECASE)
    daten["bsnr"] = bsnr.group(1) if bsnr else suche(["betriebsstätten"], zeilen)

    lanr = re.search(r'(?:Arzt-Nr|LANR)[.:\s]*(\d{9})', volltext, re.IGNORECASE)
    daten["lanr"] = lanr.group(1) if lanr else suche(["arzt-nr"], zeilen)

    # Ausstellungsdatum – letztes Datum im Dokument
    daten["ausstellungsdatum"] = suche(["datum"], zeilen)
    if not daten["ausstellungsdatum"]:
        daten["ausstellungsdatum"] = suche(["datum"], zeilen)
    alle_daten = re.findall(r'\b\d{2}\.\d{2}\.\d{2,4}\b', volltext)
    if alle_daten and not daten["ausstellungsdatum"]:
        daten["ausstellungsdatum"] = alle_daten[-1]

    # Unterschrift
    daten["unterschrift"] = "vorhanden" if re.search(
        r'unterschrift|vertragsarzt', volltext, re.IGNORECASE) else ""

    # Stempeldaten
    daten["arzt_name"]    = suche(["dres.", "dr. med.", "dr.med", "gemeinschaftspraxis"], zeilen)
    daten["arzt_beruf"]   = suche(["facharzt", "allgemeinmedizin", "innere", "orthopädie", "neurologie"], zeilen)
    daten["arzt_strasse"] = suche(["lange str", "str.", "weg ", "allee ", "platz "], zeilen)
    daten["arzt_plz_ort"] = suche(["bückeburg", "rinteln", "minden", "lingen"], zeilen)
    daten["arzt_telefon"] = suche(["tel.", "telefon", "tel:"], zeilen)

    # Fachbereich
    if re.search(r'physiotherapie', volltext, re.IGNORECASE):
        daten["fachbereich"] = "Physiotherapie"
    elif re.search(r'ergotherapie', volltext, re.IGNORECASE):
        daten["fachbereich"] = "Ergotherapie"
    else:
        daten["fachbereich"] = ""

    # ICD-10
    icd = re.search(r'\b([A-Z]\d{2}(?:\.\d{1,4})?)\b', volltext)
    daten["icd10"] = icd.group(1) if icd else ""

    # Diagnosegruppe
    gruppen = ["EX","WS","CS","ZN","PN","AT","GE","LY",
               "SO1","SO2","SO3","SO4","SO5",
               "EN1","EN2","EN3","PS1","PS2","PS3","PS4","SB1"]
    dg = re.search(r'\b(' + '|'.join(gruppen) + r')\b', volltext)
    daten["diagnosegruppe"] = dg.group(1) if dg else ""

    # Leitsymptomatik
    ls = re.search(r'leitsymptomatik[^a-z]*([a-c])\b', volltext, re.IGNORECASE)
    if ls:
        daten["leitsymptomatik"] = ls.group(1).lower()
    else:
        # Checkbox-Erkennung: X vor a/b/c
        ls2 = re.search(r'[Xx✓]\s*([a-c])\b', volltext)
        daten["leitsymptomatik"] = ls2.group(1).lower() if ls2 else suche(["leitsymptomatik"], zeilen)

    # Heilmittel
    if re.search(r'blankoverordnung', volltext, re.IGNORECASE):
        daten["heilmittel"] = "BLANKOVERORDNUNG"
        daten["anzahl_einheiten"] = "1"
        daten["frequenz"] = "blanko"
    else:
        daten["heilmittel"] = suche(["kg-zns","kg ","mld","mt ","krankengymnastik","heilmittel"], zeilen)
        anzahl = re.search(r'\b(\d+)\b(?=\s*(?:Behandlungseinheiten|$))', volltext, re.MULTILINE)
        # Einfacher: erste isolierte Zahl zwischen 1-60
        alle_zahlen = re.findall(r'\b(\d{1,2})\b', volltext)
        plausible = [z for z in alle_zahlen if 1 <= int(z) <= 60]
        daten["anzahl_einheiten"] = plausible[-1] if plausible else ""
        daten["frequenz"] = suche(["frequenz","x wöch","wöch","x/woche"], zeilen)
        if not daten["frequenz"]:
            freq = re.search(r'(\d+-?\d*x\s*wöch\.?)', volltext, re.IGNORECASE)
            daten["frequenz"] = freq.group(1) if freq else ""

    # Hausbesuch
    if re.search(r'hausbesuch.*?(?:X|x|✓)\s*(?:ja|nein)', volltext, re.IGNORECASE):
        hb = re.search(r'(?:X|x|✓)\s*(ja|nein)', volltext, re.IGNORECASE)
        daten["hausbesuch"] = hb.group(1).lower() if hb else ""
    elif re.search(r'nein', volltext, re.IGNORECASE):
        daten["hausbesuch"] = "nein"
    else:
        daten["hausbesuch"] = ""

    # Zuzahlung
    if re.search(r'zuzahlungsfrei|befreit|gebührenfrei', volltext, re.IGNORECASE):
        daten["zuzahlung"] = "zuzahlungsfrei"
    else:
        daten["zuzahlung"] = "zuzahlungspflichtig"  # Standard wenn nicht explizit befreit

    logger.info(f"Felder extrahiert: {sum(1 for v in daten.values() if v)} von {len(daten)}")
    return daten


def scan_zu_bild(aufloesung_dpi: int = 300) -> "str | None":
    """Scannt via AppleScript."""
    try:
        ausgabe = tempfile.mktemp(suffix=".tiff")
        script = f'''tell application "Image Capture"
set theScanner to first device
set output file of theScanner to POSIX file "{ausgabe}"
set resolution of theScanner to {aufloesung_dpi}
scan theScanner
end tell'''
        r = subprocess.run(["osascript", "-e", script],
            capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and Path(ausgabe).exists():
            return ausgabe
    except Exception as e:
        logger.error(f"Scanner: {e}")
    return None


def scan_und_extrahiere(bild_pfad: "str | None" = None) -> dict:
    """Hauptfunktion: Scan/Upload → OCR → Felder."""
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
