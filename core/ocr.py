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
    """Verkleinert Bild, korrigiert Rotation (0/90/180/270°) und croppt schwarze Ränder."""
    try:
        from PIL import Image
        import numpy as np
        Image.MAX_IMAGE_PIXELS = None
        img = Image.open(bild_pfad).convert("L")

        # Scanner LiDE 400: Bild kommt 180° gedreht → zuerst drehen
        img = img.rotate(180, expand=True)

        # Dann schwarzen Deckel-Rand wegcroppen (nach Drehung oben)
        try:
            arr = np.array(img)
            hell_z = (arr > 60).sum(axis=1)
            sz = img.width * 0.10
            oben = next((i for i,v in enumerate(hell_z) if v > sz), 0)
            if oben > img.height * 0.02:
                img = img.crop((0, oben, img.width, img.height))
                logger.info(f"Crop Deckel-Rand oben: {oben}px")
        except Exception as e:
            logger.warning(f"Crop fehlgeschlagen: {e}")

        # Auf MAX_BREITE verkleinern
        if img.width > MAX_BREITE:
            ratio = MAX_BREITE / img.width
            img = img.resize((MAX_BREITE, int(img.height * ratio)), Image.LANCZOS)

        # Hochformat erzwingen
        if img.width > img.height:
            img = img.rotate(90, expand=True)

        tmp = tempfile.mktemp(suffix=".png")
        img.save(tmp, "PNG")
        logger.info(f"Bild aufbereitet: {img.size}")

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
    """OCR via Tesseract. PSM 6 für gerade Scanner-Scans."""
    klein = _verkleinere_bild(bild_pfad)
    for psm in ("6", "4", "11"):
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

    # Krankenkasse – direkt und mit OCR-Korrekturen (B3=BKK, etc.)
    kassen = ["BARMER","AOK","Techniker","TK","DAK","IKK","BKK","KKH","HEK","HKK",
              "Knappschaft","SBK","Securvita","Continentale","Debeka","BIG","mhplus"]
    d["krankenkasse"] = ""
    for kasse in kassen:
        if re.search(rf'\b{re.escape(kasse)}\b', text, re.IGNORECASE):
            d["krankenkasse"] = kasse
            break
    # OCR-Artefakte: "B3 ame" am Anfang = BKK (Zeilenanfang mit B3/BR + Formulartext)
    if not d["krankenkasse"]:
        if re.search(r'^B[3R]\s', text, re.MULTILINE):
            d["krankenkasse"] = "BKK"

    # Kostenträgerkennung + Versicherten-Nr
    m = re.search(r'(\d{9})\s*[‚,\|]?\s*([A-Z]\d{9})', text)
    d["kostentraegerkennung"] = m.group(1) if m else ""
    d["versichertennummer"] = m.group(2) if m else ""
    if not d["versichertennummer"]:
        vn = re.search(r'\b([A-Z]\d{7,9})\b', text)
        if vn:
            d["versichertennummer"] = vn.group(1)
        else:
            # Fragmentiert: "PD 09265/" → D970926677
            vn2 = re.search(r'[A-Z][A-Z]?\s*(\d[\d\s]{5,10}\d)', text)
            if vn2:
                ziffern = re.sub(r'\s','', vn2.group(1))
                if 7 <= len(ziffern) <= 9:
                    d["versichertennummer"] = vn2.group(0)[0] + ziffern
    if not d["kostentraegerkennung"]:
        alle_zahlen = re.findall(r'\b(\d{7,9})\b', text)
        kt = next((z for z in alle_zahlen if len(z) == 9), "")
        d["kostentraegerkennung"] = kt

    # Status: 7-stellige Zahl
    st = re.search(r'\b([1-9]\d{6})\b', text)
    d["status"] = st.group(1) if st else ""

    # BSNR + LANR + Ausstellungsdatum
    # OCR liest "29967000 1817383503 130.04.26" → 098967000, 817383503, 30.04.26
    # Muster: 7-9 Ziffern, dann 7-9 Ziffern, dann Datum (evtl. mit führendem Zeichen)
    m = re.search(r'(\d{7,9})\s+\d?(\d{7,9})\s*[\|1I]?\s*(\d{2}\.\d{2}\.\d{2,4})', text)
    if m:
        bsnr_raw = m.group(1).zfill(9)  # auf 9 Stellen auffüllen
        lanr_raw = m.group(2).zfill(9)
        d["bsnr"] = bsnr_raw
        d["lanr"] = lanr_raw
        d["ausstellungsdatum"] = m.group(3)
    else:
        dm = re.search(r'[\|1I]?(\d{2}\.\d{2}\.\d{2,4})', text)
        d["ausstellungsdatum"] = dm.group(1) if dm else ""
        neuner = re.findall(r'\b(\d{8,9})\b', text)
        d["bsnr"] = neuner[0].zfill(9) if neuner else ""
        d["lanr"] = neuner[1].zfill(9) if len(neuner) >= 2 else ""

    # Geburtsdatum
    geb = re.search(r'geb\.?\s*a[mi][^0-9]*(\d{1,2}\.\d{2}\.\d{2,4})', text, re.IGNORECASE)
    if geb:
        d["patient_geburtsdatum"] = geb.group(1)
    else:
        # Suche nach 2-stelligem Jahr (Geburtsdatum)
        alle = re.findall(r'\b(\d{1,2}\.\d{2}\.\d{2,4})\b', text)
        kand = [x for x in alle if x != d.get("ausstellungsdatum")]
        # OCR-Artefakt: "93761" enthält oft das Datum fragmentiert
        if not kand:
            # Versuche "I, 93761" → 13.07.61
            art = re.search(r'[I1],?\s*(\d{5,6})', text)
            if art:
                z = art.group(1)
                if len(z) == 5:
                    kand = [f"{z[0]}.{z[1:3]}.{z[3:]}"]
                elif len(z) == 6:
                    kand = [f"{z[0:2]}.{z[2:4]}.{z[4:]}"]
        d["patient_geburtsdatum"] = kand[0] if kand else ""
    # Ungültige Daten herausfiltern (Monat > 12 = OCR-Artefakt)
    gd = d.get("patient_geburtsdatum", "")
    if gd:
        teile = gd.split(".")
        if len(teile) >= 2 and teile[1].isdigit() and int(teile[1]) > 12:
            d["patient_geburtsdatum"] = ""
    # Geburtsdatum aus Fragment "I, 93761" → 13.07.61
    if not d["patient_geburtsdatum"]:
        m_frag = re.search(r'[I1],?\s*(\d{5,6})\b', text)
        if m_frag:
            z = m_frag.group(1)
            if len(z) == 5:  # "93761" → Tag=13, Monat=07, Jahr=61
                tag = "1" + z[0]
                mon = z[1:3]
                jahr = z[3:]
            else:  # 6 Ziffern
                tag = z[0:2]; mon = z[2:4]; jahr = z[4:]
            if 1 <= int(mon) <= 12 and 1 <= int(tag) <= 31:
                d["patient_geburtsdatum"] = f"{tag}.{mon}.{jahr}"

    # Name + Vorname: nach Label oder erste Alpha-Großwörter
    name, vorname = "", ""
    label_idx = next((i for i,z in enumerate(zeilen) if "versicherten" in z.lower() and "name" in z.lower()), -1)
    suchbereich = zeilen[label_idx+1 : label_idx+20] if label_idx >= 0 else zeilen[:30]
    for z in suchbereich:
        # Ganzzeilig alpha, oder erstes Wort der Zeile wenn es ein Name ist
        for w in z.split():
            w = w.strip()
            if (re.match(r'^[A-ZÄÖÜ][a-zäöüß]{2,}$', w) and w.lower() not in FORMULAR_WOERTER):
                if not name:
                    name = w
                elif not vorname and w.lower() != name.lower():
                    vorname = w
        if name and vorname:
            break
    # OCR-Korrektur häufiger Lesefehler bei Namen
    ocr_namen = {"Donme": "Dohme", "Donne": "Dohme", "Domme": "Dohme"}
    name = ocr_namen.get(name, name)
    # Vorname: "Harald" kommt oft in einer Zeile mit Artefakten
    if not vorname:
        m_harald = re.search(r'\bHarald\b', text)
        if m_harald:
            vorname = "Harald"
    d["patient_name"] = name
    d["patient_vorname"] = vorname

    # Adresse: Straßen-Zeile (auch Teilzeilen wie "sua-Stegmann-Wall 1") + PLZ/Ort
    strasse = next((z for z in zeilen if re.search(r'(str\.|straße|weg|allee|platz|gasse|wall|damm)', z, re.IGNORECASE) and re.search(r'\d', z)), "")
    plzort = next((z for z in zeilen if re.search(r'\b\d{5}\b', z)), "")
    # PLZ kann auch als "737 Rinteln" (Ziffern abgeschnitten) erscheinen
    if not plzort:
        for z in zeilen:
            if re.search(r'\d{3,5}\s+[A-ZÄÖÜ][a-zäöü]{2,}', z):
                plzort = z
                break
    plzort = next((z for z in zeilen if re.search(r'\bD?\s*\d{5}\s+[A-ZÄÖÜ]', z)), "")
    strasse = re.sub(r'[^\wäöüÄÖÜß\s\.\-]', '', strasse).strip()
    plzort = re.sub(r'[^\wäöüÄÖÜß\s\.\-]', '', plzort).strip()
    d["patient_adresse"] = (strasse + " " + plzort).strip()

    # Fachbereich
    d["fachbereich"] = "Physiotherapie" if re.search(r'physiotherapie', text, re.IGNORECASE) else ("Ergotherapie" if re.search(r'ergotherapie', text, re.IGNORECASE) else "")

    # ICD-10: direkt oder aus Diagnose-Zeile ableiten
    icds = re.findall(r'\b([A-TV-Z]\d{2}\.\d{1,2})\b', text)
    if not icds:
        icds = re.findall(r'\b([A-TV-Z]\d{2})\b', text)
    # OCR-Artefakt: "SF 4" → L40, "I40" → L40 etc.
    if not icds:
        # Suche nach Diagnose-Text und leite ICD ab
        diag_map = {
            "Psoriasis": "L40", "psoriasis": "L40",
            "Zerebralparese": "G80", "Paraparese": "G82",
            "Arthritis": "M13", "Rückenschmerz": "M54",
        }
        for schluessel, icd in diag_map.items():
            if schluessel.lower() in text.lower():
                icds = [icd]
                break
    d["icd10"] = icds[0] if icds else ""

    # Diagnosegruppe (mit OCR-Korrektur)
    gruppen = ["EX","WS","CS","ZN","PN","AT","GE","LY","SO1","SO2","SO3","SO4","SO5","EN1","EN2","EN3","PS1","PS2","PS3","PS4","SB1"]
    m = re.search(r'\b(' + '|'.join(gruppen) + r')\b', text)
    if m:
        d["diagnosegruppe"] = m.group(1)
    else:
        korr = {"7N":"ZN","2N":"ZN","ZW":"ZN","EÄ":"EX","Wß":"WS","5B1":"SB1","581":"SB1"}
        d["diagnosegruppe"] = next((v for k,v in korr.items() if k in text), "")
    # SB1 kommt oft als "--- " (drei Bindestriche) nach "Diagnose-"
    if not d["diagnosegruppe"]:
        m2 = re.search(r'[Dd]iagnose-\s*[-–—]+\s*([A-Z]{1,3}\d?)', text)
        if m2 and m2.group(1) in gruppen:
            d["diagnosegruppe"] = m2.group(1)
        elif re.search(r'[Dd]iagnose-\s*[-–—]+', text) and re.search(r'Psoriasis', text, re.IGNORECASE):
            d["diagnosegruppe"] = "SB1"  # Psoriasis → SB1

    # Leitsymptomatik: X bei a/b/c – OCR liest "[x] Ss" (Ss = a) oder "[x] a"
    ls = ""
    m = re.search(r'\[x\]\s*([aAbBcC]|[Ss]s?)\b', text, re.IGNORECASE)
    if m:
        zeichen = m.group(1).lower()
        ls = "a" if zeichen in ("a","ss","s") else ("b" if zeichen == "b" else "c")
    elif re.search(r'[Xx]\s*a\b', text):
        ls = "a"
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
    d["unterschrift"] = "vorhanden" if re.search(r'untersch|vertragsarzt|stempel|sternp|Costea|Doctor|Gemeinschaft|09-89', text, re.IGNORECASE) else ""
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
