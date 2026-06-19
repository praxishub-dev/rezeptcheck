"""
ocr.py - RezeptCheck
OCR-Pipeline: Apple Vision (lokal, hochwertig) → Tesseract-Fallback → Feldextraktion.
Vision läuft on-device, keine Patientendaten verlassen den Mac (DSGVO).
"""

from __future__ import annotations
import logging
import re
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_BREITE = 2200  # Sweet Spot: beste Trefferquote bei guter Geschwindigkeit
_SWIFT_SCRIPT = str(Path(__file__).parent / "vision_ocr.swift")


def _vision_text(bild_pfad: str) -> str:
    """Apple Vision OCR (lokal). Gibt erkannten Text zurück oder '' bei Fehler."""
    try:
        r = subprocess.run(
            ["swift", _SWIFT_SCRIPT, bild_pfad],
            capture_output=True, text=True, timeout=40
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
        if r.stderr.strip():
            logger.warning(f"Vision: {r.stderr.strip()[:120]}")
    except FileNotFoundError:
        logger.warning("swift nicht gefunden – Xcode Command Line Tools nötig")
    except Exception as e:
        logger.warning(f"Vision fehlgeschlagen: {e}")
    return ""


def _tesseract_text(bild_pfad: str) -> str:
    """Tesseract-Fallback wenn Vision nicht verfügbar."""
    for psm in ("6", "4", "11"):
        try:
            r = subprocess.run(
                ["tesseract", bild_pfad, "stdout", "-l", "deu", "--oem", "1", "--psm", psm],
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


def _verkleinere_bild(bild_pfad: str) -> str:
    """Verkleinert Bild und korrigiert Rotation (0/90/180/270°) via Vision-Scoring."""
    try:
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None
        img = Image.open(bild_pfad).convert("L")

        # Auf MAX_BREITE verkleinern (vor Rotationscheck, spart Zeit)
        if img.width > MAX_BREITE:
            ratio = MAX_BREITE / img.width
            img = img.resize((MAX_BREITE, int(img.height * ratio)), Image.LANCZOS)

        # Rotationserkennung: Vision-Text pro Winkel, beste Ausrichtung gewinnt
        # Formular-Wörter, die auf jedem Muster-13-Rezept vorkommen
        FORMULAR = [
            "Heilmittel", "Diagnose", "Versicherten", "Kostenträger",
            "Krankenkasse", "Hausbesuch", "Therapie", "Behandlung",
            "Physiotherapie", "Ergotherapie", "Leitsymptomatik", "Maßgabe"
        ]
        bestes_bild = img
        bester_score = -1
        for winkel in [0, 90, 180, 270]:
            kandidat = img.rotate(winkel, expand=True)
            tmp_c = tempfile.mktemp(suffix=".png")
            kandidat.save(tmp_c)
            txt = _vision_text(tmp_c)
            if not txt:
                # Vision nicht verfügbar → Tesseract für den Check
                txt = _tesseract_text(tmp_c)
            score = sum(1 for w in FORMULAR if w.lower() in txt.lower())
            logger.info(f"Rotation {winkel}°: score={score}")
            if score > bester_score:
                bester_score = score
                bestes_bild = kandidat
        img = bestes_bild
        logger.info(f"Beste Rotation gewählt (score={bester_score})")

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
    """OCR: Apple Vision (lokal, hochwertig) zuerst, Tesseract als Fallback."""
    klein = _verkleinere_bild(bild_pfad)
    txt = _vision_text(klein)
    if txt and len(txt.strip()) > 80:
        logger.info(f"Vision OK ({len(txt)} Zeichen)")
        return txt
    logger.info("Vision unzureichend → Tesseract-Fallback")
    return _tesseract_text(klein)


FORMULAR_WOERTER = set("""physiotherapie podologische therapie unfall folgen geb am versicherten
name vorname ergotherapie ernährungstherapie stimm sprech sprach und schlucktherapie
heilmittelverordnung krankenkasse kostenträger status kostenträgerkennung diagnose
leitsymptomatik gruppe heilmittel behandlungseinheiten hausbesuch therapiebericht
frequenz wöch ergänzendes maßgabe kataloges betriebsstätten arzt datum barmer""".lower().split())


def _zeile_index(zeilen, *schluessel):
    """Index der ersten Zeile, die einen der Schlüssel enthält (case-insensitive)."""
    for i, z in enumerate(zeilen):
        zl = z.lower()
        if any(s.lower() in zl for s in schluessel):
            return i
    return -1


def extrahiere_felder(volltext: str) -> dict:
    """Label-orientierte Extraktion aus Apple-Vision-Text (zeilenweise sortiert)."""
    zeilen = [z.strip() for z in volltext.split("\n") if z.strip()]
    text = volltext
    d = {}

    # ── Krankenkasse ─────────────────────────────────────────────────────────
    kassen = ["AOK", "BARMER", "TK", "Techniker", "DAK", "IKK", "BKK", "KKH",
              "HEK", "HKK", "Knappschaft", "SBK", "Securvita", "Continentale",
              "Debeka", "BIG", "mhplus", "Pronova", "Viactiv", "Novitas"]
    kk = ""
    for z in zeilen[:8]:  # Kasse steht oben
        for kasse in kassen:
            if re.search(rf'\b{re.escape(kasse)}\b', z, re.IGNORECASE):
                kk = z.strip()  # ganze Zeile, z.B. "AOK Niedersachsen"
                break
        if kk:
            break
    d["krankenkasse"] = kk

    # ── Name + Vorname ───────────────────────────────────────────────────────
    # Vision liefert: <Kassenname>, <Fachbereich>, NACHNAME, <Fachbereich>, ..., VORNAME
    # Strategie: Wörter, die reine Eigennamen sind (Großbuchstabe, keine Formularwörter,
    # keine Kassen, keine Therapie-Begriffe). Erster = Nachname, nächster passender = Vorname.
    THERAPIE = ("therapie", "physio", "podolog", "ergo", "ernährung", "schluck",
                "sprech", "sprach", "stimm")
    name, vorname = "", ""
    kk_idx = _zeile_index(zeilen, *kassen) if kk else 0
    for z in zeilen[kk_idx:kk_idx + 14]:
        w = z.strip()
        if not re.match(r'^[A-ZÄÖÜ][a-zäöüß]{2,}$', w):
            continue
        wl = w.lower()
        if wl in FORMULAR_WOERTER or any(t in wl for t in THERAPIE):
            continue
        if any(k.lower() in wl for k in kassen):
            continue
        if not name:
            name = w
        elif not vorname and w != name:
            vorname = w
            break
    d["patient_name"] = name
    d["patient_vorname"] = vorname

    # ── Geburtsdatum ─────────────────────────────────────────────────────────
    # 2-stelliges Jahr, steht oben beim Namen (vor Kostenträgerkennung).
    # Layout variiert: Datum kann einige Zeilen nach "geb. am" stehen.
    geb = ""
    grenze_geb = _zeile_index(zeilen, "Kostenträgerkennung", "tenträgerkennung",
                              "Versicherten-Nr", "Status")
    such_geb = zeilen[:grenze_geb] if grenze_geb > 0 else zeilen[:16]
    for z in such_geb:
        m = re.search(r'\b(\d{1,2}\.\d{2}\.\d{2})\b', z)
        if m:
            geb = m.group(1)
            break
    d["patient_geburtsdatum"] = geb

    # ── Adresse (Straße + Hausnr + PLZ/Ort) aus oberem Patientenbereich ──────
    # Nur Zeilen oberhalb von "Kostenträgerkennung"/"Status" betrachten,
    # damit der Arztstempel unten (Meppen) NICHT reinkommt.
    obergrenze = _zeile_index(zeilen, "Kostenträgerkennung", "tenträgerkennung",
                              "Status", "Versicherten-Nr", "Betriebsstätten")
    patientenblock = zeilen[:obergrenze] if obergrenze > 0 else zeilen[:18]
    strasse, hausnr, plz, ort = "", "", "", ""
    for i, z in enumerate(patientenblock):
        if re.search(r'(str(aß|ass)e|weg|wall|allee|damm|platz|ring|gasse|hof)\b',
                     z, re.IGNORECASE) and not any(t in z.lower() for t in THERAPIE):
            strasse = z.strip()
            # Hausnummer evtl. als eigene Zahl-Zeile direkt davor/danach
            for nb in (patientenblock[i-1] if i > 0 else "", patientenblock[i+1] if i+1 < len(patientenblock) else ""):
                if nb.strip().isdigit() and len(nb.strip()) <= 4:
                    hausnr = nb.strip()
        m = re.search(r'\bD?\s*(\d{5})\s+([A-ZÄÖÜ][a-zäöüß]+)', z)
        if m:
            plz = m.group(1)
            ort = m.group(2)
        else:
            m2 = re.search(r'\bD?\s*(\d{5})\b', z)
            if m2:
                plz = m2.group(1)
        # Ort: Zeile die nur aus einem Großwort besteht, kein Formularwort,
        # NICHT die Straße selbst (Schilfweg etc.)
        zs = z.strip()
        ist_strasse_wort = bool(re.search(r'(str(aß|ass)e|weg|wall|allee|damm|platz|ring|gasse|hof)\b', zs, re.IGNORECASE))
        if not ort and re.match(r'^[A-ZÄÖÜ][a-zäöüß]{2,}$', zs) and zs.lower() not in FORMULAR_WOERTER \
           and not any(t in zs.lower() for t in THERAPIE) and not ist_strasse_wort \
           and zs not in (name, vorname) and not any(k.lower() in zs.lower() for k in kassen):
            if zs not in ("Status",) and zs != strasse:
                ort = zs
    teile = [p for p in [strasse, hausnr] if p]
    adr_strasse = " ".join(teile)
    adr = " ".join(p for p in [adr_strasse, (f"{plz} {ort}".strip())] if p).strip()
    d["patient_adresse"] = adr

    # ── Kostenträgerkennung + Versichertennummer ────────────────────────────
    # Zeile wie "102114819JN153514093" oder getrennt
    kt, versnr = "", ""
    m = re.search(r'\b(\d{9})[^\d]{0,3}([A-Z]\d{9})\b', text)
    if m:
        kt, versnr = m.group(1), m.group(2)
    else:
        m_kt = re.search(r'\b(\d{9})\b', text)
        kt = m_kt.group(1) if m_kt else ""
        m_v = re.search(r'\b([A-Z]\d{9})\b', text)
        versnr = m_v.group(1) if m_v else ""
    d["kostentraegerkennung"] = kt
    d["versichertennummer"] = versnr

    # ── Status (7-stellig, beginnt mit 1-9; toleriert 1 führende Störziffer) ──
    st = re.search(r'\b([1-9]\d{6})\b', text)
    if st:
        d["status"] = st.group(1)
    else:
        # 8-stellig durch OCR-Artefakt (z.B. "15000000" statt "5000000")
        st8 = re.search(r'\b\d(\d{6}0)\b', text)  # 8 Stellen, endet auf 0 (Statuscodes enden meist auf 0)
        d["status"] = st8.group(1) if st8 else ""

    # ── BSNR + LANR + Ausstellungsdatum ──────────────────────────────────────
    bsnr, lanr, ausstell = "", "", ""
    # Variante A: verklebter Block MIT Datum, z.B. "1301840001563522303 115.04.26"
    for z in zeilen:
        dm = re.search(r'(\d{1,3})\.(\d{2})\.(\d{2,4})', z)
        ziffern_vor_datum = re.sub(r'\D', '', z.split(dm.group(0))[0]) if dm else ""
        if dm and len(ziffern_vor_datum) >= 17:
            bsnr = ziffern_vor_datum[:9]
            lanr = ziffern_vor_datum[9:][-9:]
            tag = dm.group(1)[-2:]
            ausstell = f"{tag}.{dm.group(2)}.{dm.group(3)}"
            break
    # Variante B: label-verankert – BSNR/LANR in den Zeilen nach "Betriebsstätten-Nr"
    if not bsnr:
        bidx = _zeile_index(zeilen, "Betriebsstätten")
        such_b = zeilen[bidx:bidx + 7] if bidx >= 0 else zeilen
        for z in such_b:
            zwei = re.findall(r'\b(\d{9})\b', z)
            zwei = [n for n in zwei if n != kt and n != d.get("status")]
            if len(zwei) >= 2:
                bsnr, lanr = zwei[0], zwei[1]
                break
            elif len(zwei) == 1 and not bsnr:
                bsnr = zwei[0]
            elif len(zwei) == 1 and bsnr and not lanr:
                lanr = zwei[0]
    # Variante C: langer verklebter Ziffernblock als eigene Zeile (BSNR+LANR),
    # toleriert 1 eingeschobene Störziffer, z.B. "0989670001817383503" (19 statt 18)
    if not bsnr:
        for z in zeilen:
            nur_ziffern = re.sub(r'\D', '', z.strip())
            # exakt diese Zeile soll im Wesentlichen NUR der Ziffernblock sein
            if z.strip() == nur_ziffern and 18 <= len(nur_ziffern) <= 20:
                if len(nur_ziffern) == 18:
                    kand_bsnr, kand_lanr = nur_ziffern[:9], nur_ziffern[9:]
                else:
                    # 19/20 Stellen: erste 9 = BSNR, letzte 9 = LANR (Mitte = Störziffer)
                    kand_bsnr, kand_lanr = nur_ziffern[:9], nur_ziffern[-9:]
                if kand_bsnr != kt:
                    bsnr, lanr = kand_bsnr, kand_lanr
                    break
    if not ausstell:
        # Datum nach "Datum"-Label; "120.11.25"/"130.04.26" → führende Ziffer bei 3-stelligem Tag weg
        di = _zeile_index(zeilen, "Datum")
        such_dat = zeilen[di:di + 4] if di >= 0 else zeilen
        for z in such_dat:
            dm = re.search(r'\b(\d{1,3})\.(\d{2})\.(\d{2,4})\b', z)
            if dm:
                kand = f"{dm.group(1)[-2:]}.{dm.group(2)}.{dm.group(3)}"
                if kand != d.get("patient_geburtsdatum"):
                    ausstell = kand
                    break
    d["bsnr"] = bsnr
    d["lanr"] = lanr
    d["ausstellungsdatum"] = ausstell

    # ── Fachbereich ──────────────────────────────────────────────────────────
    fb = ""
    for begriff in ["Physiotherapie", "Ergotherapie", "Podologische",
                    "Stimm-, Sprech-", "Ernährungstherapie"]:
        if begriff.lower() in text.lower():
            # nur als gewählt werten, wenn X/Kreuz – Vision zeigt das nicht zuverlässig,
            # daher: erstes vorkommendes nehmen (meist das angekreuzte oben)
            fb = "Physiotherapie" if "physio" in begriff.lower() else begriff
            if "physio" in text.lower():
                fb = "Physiotherapie"
            break
    # Physio hat Priorität wenn vorhanden
    if "physiotherapie" in text.lower():
        fb = "Physiotherapie"
    elif "ergotherapie" in text.lower():
        fb = "Ergotherapie"
    d["fachbereich"] = fb

    # ── ICD-10 ───────────────────────────────────────────────────────────────
    icd = ""
    m = re.search(r'\b([A-TV-Z]\d{2}\.\d{1,2})\s*[A-Z]?\b', text)
    if m:
        icd = m.group(1)
    else:
        m2 = re.search(r'\b([A-TV-Z]\d{2})\b', text)
        icd = m2.group(1) if m2 else ""
    d["icd10"] = icd

    # ── Diagnosegruppe ───────────────────────────────────────────────────────
    # Steht nach "Diagnose-" / "gruppe", z.B. "Diagnose- WS"
    gruppen = ["WS", "EX", "CS", "ZN", "PN", "AT", "GE", "LY", "SO1", "SO2", "SO3",
               "SO4", "SO5", "EN1", "EN2", "EN3", "PS1", "PS2", "PS3", "PS4", "SB1",
               "SB2", "SB3", "SB4", "SB5", "SB6", "SB7", "WS1", "WS2", "CS1", "CS2"]
    dg = ""
    dg_idx = _zeile_index(zeilen, "Diagnose-", "gruppe")
    if dg_idx >= 0:
        for z in zeilen[dg_idx:dg_idx + 2]:
            m = re.search(r'\b(' + '|'.join(gruppen) + r')\b', z)
            if m:
                dg = m.group(1)
                break
    if not dg:
        m = re.search(r'[Dd]iagnose-\s*([A-Z]{2}\d?)', text)
        if m and m.group(1) in gruppen:
            dg = m.group(1)
    d["diagnosegruppe"] = dg

    # ── Leitsymptomatik ──────────────────────────────────────────────────────
    ls = ""
    m = re.search(r'\bX\b\s*\n?\s*([abc])\b', text)
    if m:
        ls = m.group(1).lower()
    elif re.search(r'patientenindividuelle?\s+Leitsymptomatik', text, re.IGNORECASE) \
            and re.search(r'Schädigung|Störung|Funktion', text, re.IGNORECASE):
        ls = "patientenindividuell"
    elif re.search(r'\bX\b', text) and re.search(r'Leitsymptomatik', text):
        ls = "a"  # X meist bei a
    d["leitsymptomatik"] = ls

    # ── Heilmittel + Einheiten + Frequenz ────────────────────────────────────
    heilmittel = ""
    if "BLANKOVERORDNUNG" in text.upper() or "BLANKOVER" in text.upper():
        heilmittel = "BLANKOVERORDNUNG"
    else:
        for hm in ["KG-Gerät", "KG-ZNS", "KG", "MT", "MLD", "KMT", "Wärme",
                   "Kälte", "Elektro", "Ultraschall", "Inhalation", "Bewegungsbad",
                   "Übungsbehandlung", "Massage", "Psychisch funktionelle",
                   "Sensomotorisch", "Hirnleistungstraining", "Manuelle Therapie"]:
            if re.search(rf'{re.escape(hm)}', text, re.IGNORECASE):
                heilmittel = hm
                break
    # Fallback: Freitext-Zeile nach "Heilmittel nach Maßgabe des Kataloges"
    if not heilmittel:
        hm_idx = _zeile_index(zeilen, "Maßgabe des Kataloges", "Mabgabe des Kataloges")
        if hm_idx >= 0:
            for z in zeilen[hm_idx + 1:hm_idx + 5]:
                zs = z.strip()
                # echte Heilmittel-Bezeichnung: mehrere Buchstaben, kein Label,
                # KEINE Frequenz-/Therapiezeile
                if (len(zs) > 4 and re.search(r'[A-Za-zäöü]', zs)
                        and not re.search(r'Heilmittel|Behandlungseinheit|Ergänzend|Maßgabe|Therapie-|frequenz|wöch|tägl|monat', zs, re.IGNORECASE)):
                    heilmittel = zs
                    break
    d["heilmittel"] = heilmittel

    # Behandlungseinheiten: Zahl, evtl. mit "x" (z.B. "20x")
    eh = ""
    be_idx = _zeile_index(zeilen, "Behandlungseinheit", "Behandlungsenheit")
    such_eh = zeilen[be_idx:be_idx + 4] if be_idx >= 0 else []
    for z in such_eh:
        m = re.search(r'\b(\d{1,3})\s*x?\b', z)
        if m and 1 <= int(m.group(1)) <= 99:
            eh = m.group(1)
            break
    # Fallback: "20x" irgendwo im Heilmittel-Bereich
    if not eh:
        m = re.search(r'\b(\d{1,3})\s*x\b', text)
        if m and 1 <= int(m.group(1)) <= 99:
            eh = m.group(1)
    d["anzahl_einheiten"] = eh

    # Frequenz
    freq = ""
    fm = re.search(r'(\d+)\s*x\s*(wöch|tägl|monat)', text, re.IGNORECASE)
    if fm:
        freq = fm.group(0).strip().rstrip('.')
    else:
        # label-verankert: in der Zeile mit "frequenz" steht oft "frequenz 1-2"
        for i, z in enumerate(zeilen):
            if re.search(r'frequenz', z, re.IGNORECASE):
                # Zahl/Spanne in dieser Zeile?
                fm2 = re.search(r'(\d+\s*[-–]\s*\d+|\d+\s*x?)', z)
                if fm2:
                    freq = fm2.group(1).replace(" ", "")
                    break
                # sonst in der Folgezeile
                if i + 1 < len(zeilen):
                    fm3 = re.search(r'^(\d+\s*[-–]\s*\d+|\d+\s*x?)$', zeilen[i + 1].strip())
                    if fm3:
                        freq = fm3.group(1).replace(" ", "")
                        break
    if not freq and "blanko" in heilmittel.lower():
        freq = "blanko"
    d["frequenz"] = freq

    # ── Hausbesuch ───────────────────────────────────────────────────────────
    hb = ""
    hb_idx = _zeile_index(zeilen, "Hausbesuch")
    if hb_idx >= 0:
        # Zeilen rund um das Label als Liste betrachten (Vision-Reihenfolge variiert)
        umfeld_zeilen = zeilen[max(0, hb_idx - 5):hb_idx + 5]
        # Position von "nein"/"ja"/"X" suchen: welches Kreuz steht direkt an welcher Option?
        nein_neben_x = False
        ja_neben_x = False
        for i, z in enumerate(umfeld_zeilen):
            zl = z.strip().lower()
            nachbar = " ".join(umfeld_zeilen[i:i+2]).lower()
            if zl == "nein" and "x" in (umfeld_zeilen[i+1].strip().lower() if i+1 < len(umfeld_zeilen) else ""):
                nein_neben_x = True
            if zl == "ja" and "x" in (umfeld_zeilen[i+1].strip().lower() if i+1 < len(umfeld_zeilen) else ""):
                ja_neben_x = True
            # auch "X nein" / "X ja" in einer Zeile
            if re.search(r'\bX\b[^\n]{0,4}nein', z, re.IGNORECASE):
                nein_neben_x = True
            if re.search(r'nein[^\n]{0,4}\bX\b', z, re.IGNORECASE):
                nein_neben_x = True
        if nein_neben_x:
            hb = "nein"
        elif ja_neben_x:
            hb = "ja"
        elif "nein" in " ".join(umfeld_zeilen).lower():
            hb = "nein"  # Muster 13: i.d.R. nein angekreuzt
    d["hausbesuch"] = hb

    # ── Unterschrift / Arztstempel ───────────────────────────────────────────
    d["unterschrift"] = "vorhanden" if re.search(
        r'untersch|vertragsarzt|stempel|Facharzt|Arzt für|Medizin|Telefon\s*\d', text, re.IGNORECASE
    ) else ""
    # Arztname aus Stempelblock (Zeile vor "Facharzt"/"Arzt für")
    arzt = ""
    for i, z in enumerate(zeilen):
        if re.search(r'Facharzt|Arzt für|Allgemeinmedizin', z, re.IGNORECASE) and i > 0:
            kand = zeilen[i - 1].strip()
            if re.match(r'^[A-ZÄÖÜ][a-zäöüß]+\s+[A-ZÄÖÜ]', kand):
                arzt = kand
            break
    d["arzt_name"] = arzt
    d["arzt_stempel_block"] = "vorhanden" if d["unterschrift"] else ""

    # ── Zuzahlung ────────────────────────────────────────────────────────────
    d["zuzahlung"] = "zuzahlungsfrei" if re.search(r'zuzahlungsfrei|gebührenfrei', text, re.IGNORECASE) \
        else "zuzahlungspflichtig"

    return d

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
    # Optionale KI-Zuordnung (standardmäßig aus). Bei jedem Fehler -> Regex.
    try:
        from core.ki_zuordnung import ki_verfuegbar, extrahiere_felder_ki
        if ki_verfuegbar():
            felder = extrahiere_felder_ki(volltext)
            if felder:
                return felder
            logger.warning("KI-Zuordnung leer – Regex-Fallback.")
    except Exception as e:
        logger.warning("KI-Zuordnung Fehler (%s) – Regex-Fallback.", e)
    return extrahiere_felder(volltext)
