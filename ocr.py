"""
ocr.py
Scannt ein Dokument (via ICA/TWAIN auf macOS) und extrahiert
Textfelder aus dem Muster 13 via Apple Vision Framework.

Auf macOS wird ImageCaptureCore über PyObjC angesprochen.
Als Fallback: Bilddatei direkt übergeben (für Tests).
"""

import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)


# ── 1. Scanner-Zugriff (macOS ImageCaptureCore) ───────────────────────────────

def scan_zu_bild(aufloesung_dpi: int = 300) -> str | None:
    """
    Löst einen Scan aus und gibt den Pfad zur gescannten Bilddatei zurück.
    Gibt None zurück wenn kein Scanner verfügbar.
    
    Nutzt AppleScript als zuverlässigste Methode für Flachbettscanner auf macOS.
    """
    try:
        import subprocess
        ausgabe_pfad = tempfile.mktemp(suffix=".tiff")
        
        # AppleScript: Image Capture ansprechen
        script = f'''
        tell application "Image Capture"
            set theScanner to first device
            set output file of theScanner to POSIX file "{ausgabe_pfad}"
            set resolution of theScanner to {aufloesung_dpi}
            scan theScanner
        end tell
        '''
        ergebnis = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=30
        )
        
        if ergebnis.returncode == 0 and Path(ausgabe_pfad).exists():
            logger.info(f"Scan erfolgreich: {ausgabe_pfad}")
            return ausgabe_pfad
        else:
            logger.error(f"Scan fehlgeschlagen: {ergebnis.stderr}")
            return None

    except Exception as e:
        logger.error(f"Scanner-Fehler: {e}")
        return None


# ── 2. OCR via Apple Vision ────────────────────────────────────────────────────

def extrahiere_text_aus_bild(bild_pfad: str) -> str:
    """
    Führt OCR auf einem Bild durch via Apple Vision Framework (PyObjC).
    Gibt den erkannten Volltext zurück.
    """
    try:
        import Vision
        import Quartz
        from Foundation import NSURL

        url = NSURL.fileURLWithPath_(bild_pfad)
        handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, {})
        
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        request.setRecognitionLanguages_(["de-DE", "de"])
        request.setUsesLanguageCorrection_(True)
        
        handler.performRequests_error_([request], None)
        
        erkannter_text = []
        for observation in request.results():
            kandidat = observation.topCandidates_(1)
            if kandidat:
                erkannter_text.append(kandidat[0].string())
        
        volltext = "\n".join(erkannter_text)
        logger.info(f"OCR abgeschlossen, {len(erkannter_text)} Textblöcke erkannt.")
        return volltext

    except ImportError:
        logger.error("PyObjC/Vision nicht verfügbar. Bitte: pip install pyobjc-framework-Vision")
        return ""
    except Exception as e:
        logger.error(f"OCR-Fehler: {e}")
        return ""


# ── 3. Feldextraktion aus OCR-Text ────────────────────────────────────────────

def extrahiere_felder(volltext: str) -> dict:
    """
    Versucht aus dem OCR-Volltext die relevanten Felder zu extrahieren.
    
    Strategie: Regelbasierte Mustererkennung auf bekannte Formularbegriffe.
    Felder die nicht gefunden werden → leerer String (wird von rules_engine als Fehler erkannt).
    """
    import re
    
    zeilen = volltext.split("\n")
    daten = {}

    def suche_nach_label(label_varianten: list, zeilen: list) -> str:
        """Sucht nach einem Label und gibt den Wert in der gleichen oder nächsten Zeile zurück."""
        for i, zeile in enumerate(zeilen):
            for variante in label_varianten:
                if variante.lower() in zeile.lower():
                    # Wert nach dem Doppelpunkt in derselben Zeile
                    if ":" in zeile:
                        wert = zeile.split(":", 1)[1].strip()
                        if wert:
                            return wert
                    # Oder in der nächsten Zeile
                    if i + 1 < len(zeilen):
                        naechste = zeilen[i + 1].strip()
                        if naechste:
                            return naechste
        return ""

    # Krankenkasse
    daten["krankenkasse"] = suche_nach_label(
        ["krankenkasse", "kostenträger", "kasse"], zeilen)

    # Patientendaten
    daten["patient_name"] = suche_nach_label(
        ["name, vorname", "familienname", "name des versicherten"], zeilen)
    daten["patient_vorname"] = suche_nach_label(
        ["vorname"], zeilen)
    daten["patient_adresse"] = suche_nach_label(
        ["straße", "anschrift", "adresse"], zeilen)
    daten["patient_geburtsdatum"] = suche_nach_label(
        ["geburtsdatum", "geb.", "geboren am"], zeilen)
    daten["versichertennummer"] = suche_nach_label(
        ["versichertennr", "versicherten-nr", "versichertennummer"], zeilen)
    daten["kostentraegerkennung"] = suche_nach_label(
        ["kostenträgerkennung", "kassen-nr", "kostenträger-ik"], zeilen)
    daten["status"] = suche_nach_label(
        ["status"], zeilen)

    # Arztdaten – BSNR und LANR per Regex
    bsnr_match = re.search(r'(?:BSNR|Betriebsstätten)[:\s]*(\d{9})', volltext, re.IGNORECASE)
    daten["bsnr"] = bsnr_match.group(1) if bsnr_match else ""

    lanr_match = re.search(r'(?:LANR|Arztnummer)[:\s]*(\d{9})', volltext, re.IGNORECASE)
    daten["lanr"] = lanr_match.group(1) if lanr_match else ""

    # Datum (letztes gefundenes Datum im Dokument = meist Ausstellungsdatum)
    datum_matches = re.findall(r'\d{2}\.\d{2}\.\d{4}', volltext)
    daten["ausstellungsdatum"] = datum_matches[-1] if datum_matches else ""

    # Unterschrift (heuristisch: Wort "Unterschrift" oder Linie erkannt)
    daten["unterschrift"] = "vorhanden" if re.search(
        r'unterschrift|Stempel u\. Unterschrift', volltext, re.IGNORECASE) else ""

    # Arzt-Stempeldaten
    daten["arzt_name"] = suche_nach_label(
        ["dr.", "dr. med.", "dipl.", "facharzt", "ärztin"], zeilen)
    daten["arzt_beruf"] = suche_nach_label(
        ["facharzt", "hausarzt", "allgemeinmedizin", "orthopädie", "neurologie"], zeilen)
    daten["arzt_strasse"] = suche_nach_label(
        ["straße", "str.", "weg", "allee", "platz"], zeilen)
    daten["arzt_plz_ort"] = suche_nach_label(
        ["plz", "ort"], zeilen)
    daten["arzt_telefon"] = suche_nach_label(
        ["tel", "telefon", "fon", "phone"], zeilen)

    # Fachbereich
    if re.search(r'physiotherapie|krankengymnastik|KG\b', volltext, re.IGNORECASE):
        daten["fachbereich"] = "Physiotherapie"
    elif re.search(r'ergotherapie', volltext, re.IGNORECASE):
        daten["fachbereich"] = "Ergotherapie"
    else:
        daten["fachbereich"] = ""

    # ICD-10
    icd_match = re.search(r'\b([A-Z]\d{2}(?:\.\d{1,4})?)\b', volltext)
    daten["icd10"] = icd_match.group(1) if icd_match else ""

    # Diagnosegruppe
    diagnosegruppen = ["EX", "WS", "CS", "ZN", "PN", "AT", "GE", "LY",
                       "SO1","SO2","SO3","SO4","SO5",
                       "EN1","EN2","EN3","PS1","PS2","PS3","PS4","SB1"]
    dg_match = re.search(
        r'\b(' + '|'.join(diagnosegruppen) + r')\b', volltext)
    daten["diagnosegruppe"] = dg_match.group(1) if dg_match else ""

    # Leitsymptomatik
    ls_match = re.search(r'leitsymptomatik[:\s]*([a-cA-C]|\w{3,50})', volltext, re.IGNORECASE)
    daten["leitsymptomatik"] = ls_match.group(1) if ls_match else ""

    # Blankoverordnung
    if re.search(r'blankoverordnung', volltext, re.IGNORECASE):
        daten["heilmittel"] = "BLANKOVERORDNUNG"
        daten["anzahl_einheiten"] = "1"   # Platzhalter, wird nicht geprüft bei Blanko
        daten["frequenz"] = "blanko"
    else:
        daten["heilmittel"] = suche_nach_label(
            ["heilmittel", "kg ", "mt ", "mld", "krankengymnastik"], zeilen)
        anzahl_match = re.search(r'(\d+)\s*(?:x|einheit|behandlung)', volltext, re.IGNORECASE)
        daten["anzahl_einheiten"] = anzahl_match.group(1) if anzahl_match else ""
        daten["frequenz"] = suche_nach_label(
            ["frequenz", "x/woche", "wöchentlich"], zeilen)

    # Hausbesuch
    if re.search(r'hausbesuch[:\s]*ja', volltext, re.IGNORECASE):
        daten["hausbesuch"] = "ja"
    elif re.search(r'hausbesuch[:\s]*nein', volltext, re.IGNORECASE):
        daten["hausbesuch"] = "nein"
    else:
        daten["hausbesuch"] = ""

    # Zuzahlung
    if re.search(r'zuzahlungsfrei|gebührenfrei|befreit', volltext, re.IGNORECASE):
        daten["zuzahlung"] = "zuzahlungsfrei"
    elif re.search(r'zuzahlungspflichtig|gebührenpflichtig', volltext, re.IGNORECASE):
        daten["zuzahlung"] = "zuzahlungspflichtig"
    else:
        daten["zuzahlung"] = ""

    logger.info(f"Feldextraktion abgeschlossen: {len([v for v in daten.values() if v])} Felder gefunden.")
    return daten


# ── 4. Hauptfunktion ──────────────────────────────────────────────────────────

def scan_und_extrahiere(bild_pfad: str | None = None) -> dict:
    """
    Kompletter Durchlauf: Scannen → OCR → Felder extrahieren.
    
    bild_pfad: Optional. Wenn angegeben, wird kein Scanner ausgelöst (für Tests).
    Gibt Dict mit extrahierten Feldern zurück.
    """
    if bild_pfad is None:
        bild_pfad = scan_zu_bild()
        if bild_pfad is None:
            logger.error("Kein Scanner verfügbar und kein Bild übergeben.")
            return {}

    volltext = extrahiere_text_aus_bild(bild_pfad)
    if not volltext:
        logger.error("OCR hat keinen Text erkannt.")
        return {}

    return extrahiere_felder(volltext)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    # Test mit Dummy-Text (simuliert OCR-Output)
    test_text = """
    Krankenkasse: AOK Niedersachsen
    Name, Vorname: Mustermann, Max
    Straße: Musterstraße 1
    Geburtsdatum: 01.01.1970
    Versichertennr: A123456789
    Kostenträgerkennung: 102345678
    Status: 1
    BSNR: 123456789
    LANR: 987654321
    Dr. med. Hans Müller
    Facharzt für Allgemeinmedizin
    Arztstraße 5
    PLZ Ort: 31737 Rinteln
    Tel: 05751 12345
    Stempel u. Unterschrift
    Ausstellungsdatum: 01.06.2025
    Physiotherapie
    ICD-10: M54.5
    Diagnosegruppe: WS
    Leitsymptomatik: a
    Heilmittel: KG
    6 x Behandlung
    Frequenz: 2x/Woche
    Hausbesuch: nein
    Zuzahlungspflichtig
    """

    felder = extrahiere_felder(test_text)
    print("Extrahierte Felder:")
    for k, v in felder.items():
        print(f"  {k}: '{v}'")
