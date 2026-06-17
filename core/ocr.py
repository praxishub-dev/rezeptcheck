"""
ocr.py - RezeptCheck
Robuste OCR-Pipeline: Bild verkleinern → Tesseract → Feldextraktion.
Verhindert Einfrieren durch Größenbegrenzung. PDF via pdftoppm/Quartz.
"""

from __future__ import annotations
import logging
import re
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_BREITE = 2200  # Sweet Spot: beste Trefferquote bei guter Geschwindigkeit


def _verkleinere_bild(bild_pfad: str) -> str:
    """Verkleinert Bild auf MAX_BREITE, Graustufen, korrigiert Rotation automatisch."""
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        img = Image.open(bild_pfad)

        # Hochformat erzwingen (Scanner liefert manchmal Querformat)
        if img.width > img.height:
            img = img.rotate(90, expand=True)

        if img.width > MAX_BREITE:
            ratio = MAX_BREITE / img.width
            img = img.resize((MAX_BREITE, int(img.height * ratio)), Image.LANCZOS)
        img = img.convert("L")  # Graustufen
        tmp = tempfile.mktemp(suffix=".png")
        img.save(tmp, "PNG")

        # Schräglage korrigieren via Tesseract OSD
        try:
            r = subprocess.run(
                ["tesseract", tmp, "stdout", "--psm", "0", "-l", "deu"],
                capture_output=True, text=True, timeout=10
            )
            winkel = 0.0
            for zeile in r.stdout.splitlines():
                if "Rotate:" in zeile:
                    winkel = float(zeile.split(":")[1].strip())
                    break
            if abs(winkel) > 1:
                img2 = Image.open(tmp)
                img2 = img2.rotate(winkel, expand=True)
                img2.save(tmp)
                logger.info(f"Rotation korrigiert: {winkel}°")
        except Exception as e:
            logger.warning(f"OSD-Rotation fehlgeschlagen: {e}")

        return tmp
    except Exception as e:
        logger.error(f"Verkleinern fehlgeschlagen: {e}")
        return bild_pfad


def pdf_zu_bild(pdf_pfad: str) -> str:
    """PDF → PNG. Erst pdftoppm (poppler), dann macOS sips als Fallback."""
    # pdftoppm mit scale-to (begrenzt Größe direkt – kein Einfrieren)
    try:
        out_prefix = tempfile.mktemp()
        r = subprocess.run(
            ["pdftoppm", "-png", "-scale-to", str(MAX_BREITE), "-f", "1", "-l", "1", pdf_pfad, out_prefix],
            capture_output=True, timeout=20
        )
        png = Path(out_prefix + "-1.png")
        if not png.exists():
            png = Path(out_prefix + "-01.png")
        if png.exists():
            logger.info("PDF via pdftoppm konvertiert.")
            return str(png)
    except Exception as e:
        logger.warning(f"pdftoppm fehlgeschlagen: {e}")

    # Fallback: macOS sips
    try:
        out = tempfile.mktemp(suffix=".png")
        subprocess.run(["sips", "-s", "format", "png", pdf_pfad, "--out", out],
                       capture_output=True, timeout=15)
        if Path(out).exists():
            logger.info("PDF via sips konvertiert.")
            return out
    except Exception as e:
        logger.warning(f"sips fehlgeschlagen: {e}")

    return pdf_pfad


def extrahiere_text(bild_pfad: str) -> str:
    """OCR via Tesseract. Bild wird vorher verkleinert. PSM 11 = beste Trefferquote bei Formularen."""
    klein = _verkleinere_bild(bild_pfad)
    # PSM 11 (sparse text) liefert bei Muster-13-Formularen die beste Trefferquote
    for psm in ("11", "4", "6"):
        try:
            r = subprocess.run(
                ["tesseract", klein, "stdout", "-l", "deu", "--oem", "1", "--psm", psm],
                capture_output=True, text=True, timeout=20
            )
            if len(r.stdout.strip()) > 100:
                logger.info(f"Tesseract OK (psm {psm}, {len(r.stdout)} Zeichen)")
                return r.stdout
        except FileNotFoundError:
            logger.error("Tesseract fehlt – brew install tesseract tesseract-lang")
            return ""
        except Exception as e:
            logger.warning(f"Tesseract psm {psm}: {e}")
    return ""


FORMULAR_WOERTER = set("""physiotherapie podologische therapie unfall folgen geb am versicherten
name vorname ergotherapie ernährungstherapie stimm sprech sprach und schlucktherapie
heilmittelverordnung krankenkasse kostenträger status kostenträgerkennung diagnose
leitsymptomatik gruppe heilmittel behandlungseinheiten hausbesuch therapiebericht
frequenz wöch ergänzendes maßgabe kataloges betriebsstätten arzt datum barmer""".lower().split())

def extrahiere_felder(volltext: str) -> dict:
    zeilen = [z.strip() for z in volltext.split("\n") if z.strip()]
    text = volltext
    d = {}

    # Krankenkasse
    kassen = ["BARMER","AOK","Techniker","TK","DAK","IKK","BKK","KKH","HEK","HKK","Knappschaft","SBK","Securvita","Continentale","Debeka","BIG","mhplus"]
    d["krankenkasse"] = next((k for k in kassen if re.search(rf'\b{re.escape(k)}\b', text, re.IGNORECASE)), "")

    # Kostenträgerkennung + Versicherten-Nr (Komma/Pipe/Space-Trenner)
    m = re.search(r'(\d{9})\s*[‚,\|]?\s*([A-Z]\d{9})', text)
    d["kostentraegerkennung"] = m.group(1) if m else (re.search(r'\b(\d{9})\b', text).group(1) if re.search(r'\b(\d{9})\b', text) else "")
    d["versichertennummer"] = m.group(2) if m else (re.search(r'\b([A-Z]\d{9})\b', text).group(1) if re.search(r'\b([A-Z]\d{9})\b', text) else "")

    # Status: 7-stellige Zahl (oft 3000000, 1000000 etc.)
    st = re.search(r'\b([1-9]\d{6})\b', text)
    d["status"] = st.group(1) if st else ""

    # BSNR + LANR + Ausstellungsdatum
    m = re.search(r'(\d{9})\s+\d?(\d{9})\s*[\(\|]?\s*(\d{2}\.\d{2}\.\d{2,4})', text)
    if m:
        d["bsnr"], d["lanr"], d["ausstellungsdatum"] = m.group(1), m.group(2), m.group(3)
    else:
        d["bsnr"] = ""
        d["lanr"] = ""
        dm = re.search(r'\b(\d{2}\.\d{2}\.\d{2,4})\b', text)
        d["ausstellungsdatum"] = dm.group(1) if dm else ""
        # BSNR/LANR einzeln: zwei 9-stellige Zahlen
        neuner = re.findall(r'\b(\d{9})\b', text)
        if len(neuner) >= 2:
            d["bsnr"] = d["bsnr"] or neuner[0]
            d["lanr"] = neuner[1]

    # Geburtsdatum: nach "geb. am", sonst erstes Datum != Ausstellungsdatum
    geb = re.search(r'geb\.?\s*am[^0-9]*(\d{2}\.\d{2}\.\d{2,4})', text, re.IGNORECASE)
    if geb:
        d["patient_geburtsdatum"] = geb.group(1)
    else:
        alle = re.findall(r'\b(\d{2}\.\d{2}\.\d{2,4})\b', text)
        kand = [x for x in alle if x != d.get("ausstellungsdatum")]
        d["patient_geburtsdatum"] = kand[0] if kand else ""

    # Name + Vorname: nach Label "Name, Vorname des Versicherten"
    name, vorname = "", ""
    label_idx = next((i for i,z in enumerate(zeilen) if "versicherten" in z.lower() and "name" in z.lower()), -1)
    if label_idx >= 0:
        for z in zeilen[label_idx+1 : label_idx+20]:
            w = z.strip()
            # Nur ein Wort, Großbuchstabe-Anfang, alphabetisch, kein Formularwort
            if (re.match(r'^[A-ZÄÖÜ][a-zäöüß]{2,}$', w) and w.lower() not in FORMULAR_WOERTER):
                if not name:
                    name = w
                elif not vorname and w.lower() != name.lower():
                    vorname = w
            if name and vorname:
                break
    d["patient_name"] = name
    d["patient_vorname"] = vorname

    # Adresse: Straßen-Zeile + PLZ/Ort-Zeile zusammensetzen
    strasse = next((z for z in zeilen if re.search(r'(str\.|straße|weg|allee|platz|gasse)', z, re.IGNORECASE)), "")
    plzort = next((z for z in zeilen if re.search(r'\bD?\s*\d{5}\s+[A-ZÄÖÜ]', z)), "")
    strasse = re.sub(r'[^\wäöüÄÖÜß\s\.\-]', '', strasse).strip()
    plzort = re.sub(r'[^\wäöüÄÖÜß\s\.\-]', '', plzort).strip()
    d["patient_adresse"] = (strasse + " " + plzort).strip()

    # Fachbereich
    d["fachbereich"] = "Physiotherapie" if re.search(r'physiotherapie', text, re.IGNORECASE) else ("Ergotherapie" if re.search(r'ergotherapie', text, re.IGNORECASE) else "")

    # ICD-10: erstes valides mit bekanntem Anfangsbuchstaben (G,M,F,S,Z,R,I,J,K,...)
    icds = re.findall(r'\b([A-TV-Z]\d{2}\.\d{1,2})\b', text)
    if not icds:
        icds = re.findall(r'\b([A-TV-Z]\d{2})\b', text)
    d["icd10"] = icds[0] if icds else ""

    # Diagnosegruppe (mit OCR-Korrektur)
    gruppen = ["EX","WS","CS","ZN","PN","AT","GE","LY","SO1","SO2","SO3","SO4","SO5","EN1","EN2","EN3","PS1","PS2","PS3","PS4","SB1"]
    m = re.search(r'\b(' + '|'.join(gruppen) + r')\b', text)
    if m:
        d["diagnosegruppe"] = m.group(1)
    else:
        korr = {"7N":"ZN","2N":"ZN","ZW":"ZN","EÄ":"EX","Wß":"WS"}
        d["diagnosegruppe"] = next((r for f,r in korr.items() if f in text), "")

    # Leitsymptomatik: X bei a/b/c, oder Freitext
    ls = ""
    m = re.search(r'[Xx]\s*([abc])\b', text)
    if m:
        ls = m.group(1).lower()
    elif re.search(r'X[pbc]', text):  # "Xp" = X bei b
        mm = re.search(r'X([pbc])', text)
        ls = "b" if mm.group(1)=="p" else mm.group(1)
    elif re.search(r'sch[äa]digung|st[öo]rung|funktion', text, re.IGNORECASE):
        ls = "patientenindividuell"
    d["leitsymptomatik"] = ls

    # Heilmittel + Anzahl + Blanko
    if re.search(r'blanko', text, re.IGNORECASE):
        d["heilmittel"] = "BLANKOVERORDNUNG"; d["anzahl_einheiten"] = "1"; d["frequenz"] = "blanko"
    else:
        # Heilmittel-Zeile finden
        hm = ""
        for z in zeilen:
            hmm = re.search(r'(KG-ZNS|KG-Ger[äa]te|KG\b|MT\b|MLD|KMT|Bobath|Vojta|PNF|Manuelle\s+Therapie|Krankengymnastik|Massage|W[äa]rmetherapie|Elektrotherapie)', z, re.IGNORECASE)
            if hmm:
                hm = z.strip()
                break
        # (Bobath) anhängen falls separate Zeile
        if hm and "bobath" not in hm.lower():
            for z in zeilen:
                if "bobath" in z.lower():
                    hm = hm + " (Bobath)"
                    break
        hm = re.sub(r'\s*\d+\s*$', '', hm).strip()
        d["heilmittel"] = hm

        # Anzahl: Zahl in Zeile "Behandlungseinheiten"-Nähe oder isolierte Zahl die plausibel ist
        anzahl = ""
        # Zahl direkt nach KG-ZNS Block (Zeile mit nur einer Zahl 1-99 nach Heilmittel)
        for i, z in enumerate(zeilen):
            if re.search(r'KG-ZNS|bobath', z, re.IGNORECASE):
                # Suche in den nächsten 3 Zeilen nach isolierter Zahl
                for zz in zeilen[i:i+4]:
                    am = re.search(r'\b([1-9]\d?)\b', zz)
                    if am and 1 <= int(am.group(1)) <= 99:
                        anzahl = am.group(1)
                        break
                if anzahl: break
        if not anzahl:
            # Fallback: höchste plausible Behandlungszahl
            zahlen = [int(z) for z in re.findall(r'\b(\d{1,2})\b', text) if 1 <= int(z) <= 60]
            anzahl = str(max(zahlen)) if zahlen else ""
        d["anzahl_einheiten"] = anzahl

        freq = re.search(r'(\d+\s*[-–]\s*\d*\s*x\s*w[öo]ch\.?|\d+\s*x\s*w[öo]ch\.?)', text, re.IGNORECASE)
        d["frequenz"] = freq.group(1).strip() if freq else ""

    # Hausbesuch: X bei ja/nein
    hb = ""
    hbm = re.search(r'hausbesuch[^\n]*?([Xx])\s*(ja|nein)|hausbesuch[^\n]*?(ja|nein)\s*([Xx])', text, re.IGNORECASE)
    if hbm:
        if hbm.group(2):
            hb = hbm.group(2).lower()
        elif hbm.group(3):
            hb = hbm.group(3).lower()
    if not hb:
        # "Hausbesuch = ja nein" ohne klares X – Standard nein (häufigster Fall)
        if re.search(r'hausbesuch', text, re.IGNORECASE):
            hb = "nein"
    d["hausbesuch"] = hb

    # Zuzahlung
    d["zuzahlung"] = "zuzahlungsfrei" if re.search(r'zuzahlungsfrei|geb[üu]hrenfrei|befreit', text, re.IGNORECASE) else "zuzahlungspflichtig"

    # Stempel
    d["unterschrift"] = "vorhanden" if re.search(r'untersch|vertragsarzt|stempel|sternp', text, re.IGNORECASE) else ""
    am = re.search(r'(Dres?\.\s*[A-ZÄÖÜ][a-zäöü]+(?:\s+un\w*\s+\w*)?)', text)
    d["arzt_name"] = re.sub(r"\s+", " ", am.group(1)).strip() if am else ""
    d["arzt_stempel_block"] = "vorhanden" if re.search(r'gemeinschaftspraxis|praxis|Dres?\.|fach[äa]rzt|[äa]rzte', text, re.IGNORECASE) else ""

    return d


def scan_zu_bild(aufloesung_dpi: int = 300) -> "str | None":
    """Scannt via AppleScript / Image Capture."""
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
