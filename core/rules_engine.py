"""
rules_engine.py
Prüft extrahierte Rezeptdaten gegen das Regelwerk aus rules.json.
Gibt Ergebnis zurück: GRUEN / GELB / ROT + Liste der Fehler/Warnungen.
"""

from __future__ import annotations
import json
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

RULES_PATH = Path(__file__).parent.parent / "rules.json"
BVB_LHB_PATH = Path(__file__).parent / "bvb_lhb.json"

# BVB/LHB-Diagnosedaten (amtliche KBV-Liste) einmalig laden.
_BVB_LHB_CACHE = None

def lade_bvb_lhb() -> dict:
    """Lädt die amtliche Diagnoseliste (langfristiger Heilmittelbedarf /
    besonderer Verordnungsbedarf). Quelle: KBV, heilmittel-diagnoseliste.pdf.
    Struktur: { 'E88.21': {'diagnose':..., 'gruppen':['LY'], 'hinweis':...}, ... }
    """
    global _BVB_LHB_CACHE
    if _BVB_LHB_CACHE is None:
        try:
            with open(BVB_LHB_PATH, "r", encoding="utf-8") as f:
                _BVB_LHB_CACHE = json.load(f)
        except Exception:
            _BVB_LHB_CACHE = {}
    return _BVB_LHB_CACHE


def pruefe_bvb_lhb(icd_code: str, diagnosegruppe: str) -> dict:
    """Prüft, ob ICD-Code + Diagnosegruppe einen anerkannten BVB/LHB darstellen.

    Rückgabe-Dict:
      status:
        'anerkannt'     -> Code gelistet UND Diagnosegruppe passt -> Höchstmenge gilt nicht
        'code_gelistet' -> Code gelistet, aber für ANDERE Diagnosegruppe(n)
        'aehnlich'      -> Code nicht gelistet, aber gleicher Stamm (z.B. E88.28 vs E88.21)
        'nicht'         -> nicht gelistet, kein ähnlicher Code
      info: erläuternder Text (Diagnose-Klartext, ähnlicher Code, Befristung)
    """
    daten = lade_bvb_lhb()
    if not daten:
        return {"status": "nicht", "info": ""}
    code = re.sub(r'[^A-Z0-9. ]', '', (icd_code or "").upper())
    # Diagnosesicherheits-Kennzeichen (G/V/A/Z) am Ende abtrennen
    code = re.sub(r'\s*[GVAZ]\s*$', '', code).strip()
    code = code.replace(" ", "")
    grp = (diagnosegruppe or "").upper().strip()

    eintrag = daten.get(code)
    if eintrag:
        gruppen = [g.upper() for g in eintrag.get("gruppen", [])]
        if not grp or grp in gruppen:
            bef = ""
            if "befristet" in eintrag.get("hinweis", "").lower():
                m = re.search(r'(\d{2}\.\d{2}\.\d{4})', eintrag["hinweis"])
                bef = f" (befristet bis {m.group(1)})" if m else " (befristet)"
            return {"status": "anerkannt",
                    "info": f"{eintrag['diagnose']}{bef}"}
        return {"status": "code_gelistet",
                "info": f"Code {code} ist BVB/LHB, aber für Gruppe(n) {', '.join(gruppen)}, "
                        f"nicht {grp}"}

    # Code nicht exakt gelistet -> ähnlichen Code mit gleichem Stamm suchen
    # (z.B. E88.28 eingegeben, aber E88.20/21/22 sind gelistet -> Verdacht Tipp-/Lesefehler)
    if "." in code:
        stamm = code.split(".")[0] + "."
        kandidaten = sorted(c for c in daten if c.startswith(stamm) and c != code)
        if kandidaten:
            passende = [c for c in kandidaten
                        if not grp or grp in [g.upper() for g in daten[c].get("gruppen", [])]]
            ziel = passende or kandidaten
            beispiel = ziel[0]
            return {"status": "aehnlich",
                    "info": f"Code {code} ist NICHT in der BVB/LHB-Liste, aber "
                            f"{beispiel} ({daten[beispiel]['diagnose']}) wäre es – "
                            f"ICD-Code am Rezept prüfen"}
    return {"status": "nicht", "info": ""}


@dataclass
class Pruefergebnis:
    status: str  # "GRUEN", "GELB", "ROT"
    fehler: list[str] = field(default_factory=list)    # -> ROT
    warnungen: list[str] = field(default_factory=list)  # -> GELB


def lade_regelwerk() -> dict:
    with open(RULES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def ist_blanko(daten: dict) -> bool:
    heilmittel = daten.get("heilmittel", "").strip().upper()
    return "BLANKOVERORDNUNG" in heilmittel


def pruefe_icd10(code: str) -> bool:
    """Prüft ob der ICD-10-Code ein gültiges Format hat (z.B. M54.5, G35, Z96.65).

    Ein optionales Diagnosesicherheits-Kennzeichen (G = gesichert, V = Verdacht,
    A = ausgeschlossen, Z = Zustand nach) darf dem Code folgen, z.B. "M54.10 G".
    Es wird vor der Formatprüfung abgetrennt; der Code selbst wird streng geprüft.
    """
    if not code:
        return False
    bereinigt = code.strip().upper()
    # Optionales Diagnosesicherheits-Kennzeichen (G/V/A/Z) am Ende erlauben,
    # mit oder ohne Leerzeichen davor. Der Code selbst wird streng geprüft.
    pattern = r'^[A-Z][0-9]{2}(?:\.[0-9A-Z]{1,4})?(?:[ ]?[GVAZ])?$'
    return bool(re.match(pattern, bereinigt))


def split_icd_codes(feld: str) -> list:
    """Zerlegt ein ICD-Feld in einzelne Codes. Auf Heilmittelverordnungen können
    mehrere Diagnosen stehen (z.B. "Z96.64 RZ, Z98.88 RG"). Trennzeichen sind
    Komma, Semikolon, Slash oder mehrere Leerzeichen vor einem neuen Code.
    Gibt eine Liste der Roh-Codes zurück (mit evtl. angehängtem Kennzeichen).
    """
    if not feld:
        return []
    # An Komma/Semikolon/Slash und an Stellen, wo ein neuer Code beginnt, trennen
    teile = re.split(r'[;,/]+|\s{2,}', feld.strip())
    codes = []
    for t in teile:
        t = t.strip()
        # Innerhalb eines Teils kann noch "CODE KENNZ CODE" stehen -> per Regex Codes ziehen
        for m in re.finditer(r'[A-Z][0-9]{2}(?:\.[0-9]{1,3})?', t.upper()):
            codes.append(m.group(0))
    # Dedupe unter Erhalt der Reihenfolge
    seen = set(); out = []
    for c in codes:
        if c not in seen:
            seen.add(c); out.append(c)
    return out


def pruefe_icd_feld(feld: str, diagnosegruppe: str) -> dict:
    """Prüft ein komplettes ICD-Feld (ein oder mehrere Codes).
    Rückgabe: {gueltig: bool, codes: [...], bvb_treffer: dict|None, hinweis: str}
    """
    codes = split_icd_codes(feld)
    if not codes:
        return {"gueltig": False, "codes": [], "bvb_treffer": None,
                "hinweis": "kein ICD-Code erkennbar"}
    gueltige = [c for c in codes if pruefe_icd10(c)]
    # BVB-Treffer über alle Codes suchen (bester Status gewinnt)
    bvb_bester = None
    for c in codes:
        res = pruefe_bvb_lhb(c, diagnosegruppe)
        if res["status"] == "anerkannt":
            bvb_bester = {"code": c, **res}; break
        if res["status"] in ("aehnlich", "code_gelistet") and bvb_bester is None:
            bvb_bester = {"code": c, **res}
    return {
        "gueltig": len(gueltige) > 0,
        "codes": codes,
        "bvb_treffer": bvb_bester,
        "hinweis": "" if gueltige else f"keiner der Codes {codes} hat gültiges Format"
    }


def pruefe_neun_stellig_numerisch(wert: str) -> bool:
    return bool(re.match(r'^\d{9}$', wert.strip())) if wert else False


def pruefe_datum(wert: str) -> bool:
    """Prüft Format TT.MM.JJJJ"""
    return bool(re.match(r'^\d{1,2}\.\d{2}\.\d{2,4}$', wert.strip())) if wert else False


def pruefe_rezept(daten: dict) -> Pruefergebnis:
    """
    Hauptfunktion. Nimmt ein Dict mit OCR-extrahierten Feldern,
    gibt Pruefergebnis zurück.

    Erwartete Keys in `daten` entsprechen den Feldnamen in rules.json.
    Fehlende Keys werden als leer behandelt.
    """
    regeln = lade_regelwerk()
    fehler = []
    warnungen = []
    blanko = ist_blanko(daten)

    # ── BLOCK 1: Patientendaten ──────────────────────────────────────────────
    for feld_def in regeln["pflichtfelder"]["patientendaten"]:
        feld = feld_def["feld"]
        label = feld_def["label"]
        typ = feld_def["typ"]
        wert = daten.get(feld, "").strip()

        if typ == "text":
            if not wert:
                fehler.append(f"Fehlt: {label}")

        elif typ == "datum":
            if not wert:
                fehler.append(f"Fehlt: {label}")
            elif not pruefe_datum(wert):
                fehler.append(f"Ungültiges Format: {label} (erwartet TT.MM.JJJJ)")

        elif typ == "numerisch_9":
            if not wert:
                fehler.append(f"Fehlt: {label}")
            elif not pruefe_neun_stellig_numerisch(wert):
                fehler.append(f"Ungültig: {label} (muss 9-stellig numerisch sein)")

    # ── BLOCK 2: Arztdaten ───────────────────────────────────────────────────
    # BSNR (Pflicht, 9-stellig)
    bsnr = daten.get("bsnr", "").strip()
    if not bsnr:
        fehler.append("Fehlt: Betriebsstättennummer (BSNR)")
    elif not pruefe_neun_stellig_numerisch(bsnr):
        fehler.append("Ungültig: BSNR (muss 9-stellig numerisch sein)")

    # LANR (Pflicht, 9-stellig)
    lanr = daten.get("lanr", "").strip()
    if not lanr:
        fehler.append("Fehlt: Arztnummer (LANR)")
    elif not pruefe_neun_stellig_numerisch(lanr):
        fehler.append("Ungültig: LANR (muss 9-stellig numerisch sein)")

    # Ausstellungsdatum (Pflicht)
    ausstellungsdatum = daten.get("ausstellungsdatum", "").strip()
    if not ausstellungsdatum:
        fehler.append("Fehlt: Ausstellungsdatum")
    elif not pruefe_datum(ausstellungsdatum):
        fehler.append("Ungültiges Format: Ausstellungsdatum")

    # Arztstempel: pragmatische Prüfung – Stempel-Block muss erkennbar sein.
    # (Einzelne Stempel-Unterfelder werden bei Foto-OCR oft nicht gelesen,
    #  daher prüfen wir ob ein Arztstempel grundsätzlich vorhanden ist.)
    stempel_block = daten.get("arzt_stempel_block", "").strip()
    arzt_name = daten.get("arzt_name", "").strip()
    if not stempel_block and not arzt_name and not bsnr:
        fehler.append("Fehlt: Arztstempel (Name, Praxis, BSNR nicht erkennbar)")

    # Unterschrift: wenn BSNR und LANR erkannt wurden, ist der Stempel physisch da
    unterschrift = daten.get("unterschrift", "").strip()
    stempel_impliziert = bool(bsnr and lanr)  # BSNR+LANR = Stempel lesbar
    if not unterschrift and not stempel_impliziert:
        fehler.append("Fehlt: Unterschrift des Arztes")

    # ── BLOCK 3: Verordnungsinhalt ───────────────────────────────────────────
    fachbereich = daten.get("fachbereich", "").strip().lower()
    if not fachbereich:
        fehler.append("Fehlt: Fachbereich (Physiotherapie / Ergotherapie)")

    # ICD-10 (kann mehrere Codes enthalten, z.B. "Z96.64, Z98.88")
    icd10 = daten.get("icd10", "").strip()
    if not icd10:
        fehler.append("Fehlt: ICD-10-Code")
    else:
        icd_pruef = pruefe_icd_feld(icd10, daten.get("diagnosegruppe", ""))
        if not icd_pruef["gueltig"]:
            fehler.append(f"Ungültiger ICD-10-Code: '{icd10}'")

    # Diagnosegruppe
    diagnosegruppe = daten.get("diagnosegruppe", "").strip().upper()
    if not diagnosegruppe:
        fehler.append("Fehlt: Diagnosegruppe")
    else:
        alle_gruppen = {
            **regeln["diagnosegruppen"]["physiotherapie"],
            **regeln["diagnosegruppen"]["ergotherapie"]
        }
        if diagnosegruppe not in alle_gruppen:
            fehler.append(f"Unbekannte Diagnosegruppe: '{diagnosegruppe}'")

    # Leitsymptomatik (HeilM-RL: Pflichtangabe)
    # Strenge Kästchen-Prüfung NUR wenn die Kästchen-Felder geliefert werden
    # (KI-Bild-Modus). Im Regex-Modus fehlen diese Felder -> alte, einfache Prüfung.
    kaestchen_modus = "leitsymptomatik_freitext" in daten
    leit_kreuz = daten.get("leitsymptomatik", "").strip().lower()
    leit_freitext = daten.get("leitsymptomatik_freitext", "").strip()
    if kaestchen_modus:
        hat_buchstabe = bool(re.search(r'[abc]', leit_kreuz)) and "individuell" not in leit_kreuz
        hat_pi_kreuz = "individuell" in leit_kreuz
        if leit_kreuz or leit_freitext:
            if hat_buchstabe:
                pass
            elif hat_pi_kreuz and leit_freitext:
                pass
            elif leit_freitext and not hat_pi_kreuz:
                fehler.append("Leitsymptomatik als Freitext angegeben, aber Kästchen "
                              "'patientenindividuelle Leitsymptomatik' nicht angekreuzt")
            elif hat_pi_kreuz and not leit_freitext:
                fehler.append("Kästchen 'patientenindividuelle Leitsymptomatik' angekreuzt, "
                              "aber kein Freitext angegeben")
            else:
                fehler.append("Leitsymptomatik unklar – kein gültiges Kästchen (a/b/c) "
                              "und keine patientenindividuelle Angabe erkennbar")
        else:
            fehler.append("Fehlt: Leitsymptomatik (a/b/c ankreuzen ODER Freitext + "
                          "Kästchen 'patientenindividuell')")
    else:
        # Regex-Modus: nur prüfen, ob überhaupt etwas erkannt wurde
        if not leit_kreuz:
            fehler.append("Fehlt: Leitsymptomatik (a/b/c oder Freitext + patientenindividuell)")

    # Genau EIN Heilmittelbereich angekreuzt (nur im KI-Modus prüfbar)
    anzahl_fb = daten.get("anzahl_fachbereich_kreuze", "").strip()
    if anzahl_fb.isdigit():
        n = int(anzahl_fb)
        if n == 0:
            fehler.append("Kein Heilmittelbereich angekreuzt")
        elif n > 1:
            fehler.append(f"Mehr als ein Heilmittelbereich angekreuzt ({n}) – "
                          f"nur ein Kreuz zulässig")

    # Heilmittel + Blanko-Logik
    if blanko:
        # Blankoverordnung: Heilmittel/Anzahl/Frequenz dürfen leer sein
        # Aber: Diagnosegruppe muss passen
        if diagnosegruppe:
            blanko_regeln = regeln["blanko_regeln"]
            physio_ok = diagnosegruppe in blanko_regeln["physio_erlaubte_gruppen"]
            ergo_ok = diagnosegruppe in blanko_regeln["ergo_erlaubte_gruppen"]
            if not physio_ok and not ergo_ok:
                fehler.append(
                    f"Blankoverordnung nicht zulässig für Diagnosegruppe '{diagnosegruppe}'. "
                    f"Nur EX (Physio) oder EN3/PS3/PS4/SB1 (Ergo) erlaubt."
                )
    else:
        # Normale Verordnung
        heilmittel = daten.get("heilmittel", "").strip()
        if not heilmittel:
            fehler.append("Fehlt: Heilmittel (mindestens 1 vorrangiges Heilmittel)")

        # Anzahl Behandlungseinheiten
        anzahl_str = daten.get("anzahl_einheiten", "").strip()
        if not anzahl_str:
            fehler.append("Fehlt: Anzahl der Behandlungseinheiten")
        else:
            try:
                anzahl = int(anzahl_str)
                if anzahl <= 0:
                    fehler.append("Anzahl Behandlungseinheiten muss größer als 0 sein")
                elif diagnosegruppe:
                    alle_gruppen = {
                        **regeln["diagnosegruppen"]["physiotherapie"],
                        **regeln["diagnosegruppen"]["ergotherapie"]
                    }
                    if diagnosegruppe in alle_gruppen:
                        hoechstmenge = alle_gruppen[diagnosegruppe]["hoechstmenge"]
                        lhb_standard = alle_gruppen[diagnosegruppe].get("lhb_standard", False)
                        if anzahl > hoechstmenge and not lhb_standard:
                            # Echter Abgleich gegen die amtliche BVB/LHB-Diagnoseliste (KBV).
                            # Mehrfach-ICD-Feld: bester Treffer über alle Codes.
                            icd_pruef = pruefe_icd_feld(daten.get("icd10", ""), diagnosegruppe)
                            bvb = icd_pruef["bvb_treffer"] or {"status": "nicht", "info": ""}
                            if bvb["status"] == "anerkannt":
                                # Höchstmenge gilt nicht (§12 HeilM-RL) -> nur Info, kein Warnen.
                                warnungen.append(
                                    f"Anzahl ({anzahl}) über orientierender Menge ({hoechstmenge}), "
                                    f"aber zulässig: besonderer Verordnungsbedarf – {bvb['info']}."
                                )
                            elif bvb["status"] == "aehnlich":
                                # Code nicht gelistet, aber ähnlicher Code wäre BVB -> Lesefehler?
                                warnungen.append(
                                    f"Anzahl ({anzahl}) überschreitet Höchstmenge ({hoechstmenge}) "
                                    f"für {diagnosegruppe}. {bvb['info']}."
                                )
                            elif bvb["status"] == "code_gelistet":
                                warnungen.append(
                                    f"Anzahl ({anzahl}) überschreitet Höchstmenge ({hoechstmenge}). "
                                    f"{bvb['info']} – Diagnosegruppe gegenprüfen."
                                )
                            else:
                                warnungen.append(
                                    f"Anzahl ({anzahl}) überschreitet Höchstmenge ({hoechstmenge}) "
                                    f"für Diagnosegruppe {diagnosegruppe}. "
                                    f"Nur zulässig bei LHB oder BVB – Rücksprache mit Arzt prüfen."
                                )
            except ValueError:
                fehler.append(f"Ungültige Anzahl Behandlungseinheiten: '{anzahl_str}'")

        # Frequenz
        frequenz = daten.get("frequenz", "").strip()
        if not frequenz:
            fehler.append("Fehlt: Behandlungsfrequenz")
        else:
            # Plausibilität: gültige Muster sind z.B. "1-2", "1x wöch", "2x woechentl",
            # "3x tägl", "1 x monatl", "1-3x wöchentlich". Reine Spanne "1-2" ist ok
            # (Frequenzspanne pro Woche). Unplausibel -> nur Warnung (GELB), kein ROT,
            # da Schreibweisen stark variieren und Fehlalarme vermieden werden sollen.
            f_norm = frequenz.lower().replace(" ", "")
            # Ausgeschriebene Formen vereinheitlichen ("pro woche" -> "woech" etc.)
            f_norm = (f_norm
                      .replace("prowoche", "woech").replace("prowo", "woech")
                      .replace("protag", "taeg").replace("promonat", "monat")
                      .replace("woche", "woech").replace("tag", "taeg")
                      .replace("monatlich", "monat").replace("monatl", "monat")
                      .replace("wöch", "woech").replace("woch", "woech")
                      .replace("täg", "taeg").replace("tägl", "taeg"))
            # Mehrfach-Ersetzungen können "woechentl" -> "woechentl" verschoben haben;
            # auf einheitlichen Stamm "woech"/"taeg"/"monat" reduzieren.
            for stamm in ("woech", "taeg", "monat"):
                f_norm = re.sub(stamm + r'[a-z]*', stamm, f_norm)
            gueltig = bool(re.match(
                r'^(\d{1,2}([-–]\d{1,2})?)(x)?(woech|taeg|monat)?\.?$',
                f_norm
            )) or f_norm in ("blanko",)
            # Zusätzliche Plausibilität der Zahl, falls eine Frequenz/Woche erkennbar
            m_freq = re.match(r'^(\d{1,2})', f_norm)
            if gueltig and m_freq:
                pro_einheit = int(m_freq.group(1))
                if pro_einheit > 7 and ("taeg" not in f_norm):
                    warnungen.append(
                        f"Behandlungsfrequenz '{frequenz}' wirkt ungewöhnlich hoch – prüfen."
                    )
            if not gueltig:
                warnungen.append(
                    f"Behandlungsfrequenz '{frequenz}' nicht eindeutig interpretierbar – prüfen."
                )

    # Hausbesuch
    hausbesuch = daten.get("hausbesuch", "").strip().lower()
    if hausbesuch not in ("ja", "nein", "yes", "no", "true", "false"):
        fehler.append("Fehlt: Hausbesuch (ja oder nein muss angekreuzt sein)")

    # Zuzahlung
    zuzahlung = daten.get("zuzahlung", "").strip().lower()
    if not zuzahlung:
        fehler.append("Fehlt: Zuzahlungsstatus (zuzahlungspflichtig oder zuzahlungsfrei)")

    # ── KI-Plausibilitätshinweise (NICHT ampelentscheidend) ──────────────────
    # Diese kommen aus dem KI-Bildmodus (Schlüssel "ki_hinweise", eine Liste).
    # Sie sind reine Hinweise zum Gegenprüfen und werden als Warnungen geführt,
    # damit sie NIEMALS aus einem korrekten Rezept fälschlich ein ROT machen.
    ki_hinweise = daten.get("ki_hinweise", [])
    if isinstance(ki_hinweise, list):
        for h in ki_hinweise:
            h = str(h).strip()
            if h:
                warnungen.append(f"KI-Hinweis: {h}")
    elif isinstance(ki_hinweise, str) and ki_hinweise.strip():
        warnungen.append(f"KI-Hinweis: {ki_hinweise.strip()}")

    # ── Ergebnis (FAIL-SAFE) ─────────────────────────────────────────────────
    # Ziel: jede falsch/unvollständig ausgefüllte Verordnung MUSS auffallen.
    # Deshalb: GRÜN nur wenn alle Pflichtfelder da sind UND kein inhaltlicher
    # Fehler vorliegt. Alles andere ist ROT (= manuell ansehen). Lieber ein
    # ROT zu viel als ein falsches GRÜN. GELB nur für reine Plausibilitäts-
    # Warnungen (Feld erkannt, aber Wert grenzwertig), nie für fehlende Felder.
    fehlt_fehler = [f for f in fehler if f.startswith("Fehlt:")]
    echte_fehler = [f for f in fehler if not f.startswith("Fehlt:")]

    if echte_fehler:
        status = "ROT"  # inhaltlich falsch (Format, Höchstmenge, ungültig)
    elif fehlt_fehler:
        status = "ROT"  # Pflichtfeld nicht lesbar/vorhanden → nicht prüfbar → ROT
    elif warnungen:
        status = "GELB"  # alle Pflichtfelder da, nur Plausibilität anmerken
    else:
        status = "GRUEN"

    return Pruefergebnis(status=status, fehler=fehler, warnungen=warnungen)


# ── Schnelltest ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Testdatensatz: korrektes Rezept
    test_korrekt = {
        "krankenkasse": "AOK Niedersachsen",
        "patient_name": "Mustermann",
        "patient_vorname": "Max",
        "patient_adresse": "Musterstraße 1, 31737 Rinteln",
        "patient_geburtsdatum": "01.01.1970",
        "versichertennummer": "A123456789",
        "kostentraegerkennung": "102345678",
        "status": "1",
        "bsnr": "123456789",
        "lanr": "987654321",
        "arzt_name": "Dr. med. Hans Müller",
        "arzt_beruf": "Facharzt für Allgemeinmedizin",
        "arzt_strasse": "Arztstraße 5",
        "arzt_plz_ort": "31737 Rinteln",
        "arzt_telefon": "05751 12345",
        "unterschrift": "vorhanden",
        "ausstellungsdatum": "01.06.2025",
        "fachbereich": "Physiotherapie",
        "icd10": "M54.5",
        "diagnosegruppe": "WS",
        "leitsymptomatik": "a",
        "heilmittel": "KG",
        "anzahl_einheiten": "6",
        "frequenz": "2x/Woche",
        "hausbesuch": "nein",
        "zuzahlung": "zuzahlungspflichtig"
    }

    # Testdatensatz: Fehler
    test_fehler = {
        "krankenkasse": "AOK Niedersachsen",
        "patient_name": "Mustermann",
        "patient_vorname": "",            # FEHLT
        "patient_adresse": "Musterstraße 1",
        "patient_geburtsdatum": "01-01-1970",  # FALSCHES FORMAT
        "versichertennummer": "A123456789",
        "kostentraegerkennung": "12345",   # FALSCHE LÄNGE
        "status": "1",
        "bsnr": "123456789",
        "lanr": "987654321",
        "arzt_name": "Dr. Müller",
        "arzt_beruf": "",                  # FEHLT
        "arzt_strasse": "Arztstraße 5",
        "arzt_plz_ort": "31737 Rinteln",
        "arzt_telefon": "05751 12345",
        "unterschrift": "vorhanden",
        "ausstellungsdatum": "01.06.2025",
        "fachbereich": "Physiotherapie",
        "icd10": "M54.5",
        "diagnosegruppe": "WS",
        "leitsymptomatik": "a",
        "heilmittel": "KG",
        "anzahl_einheiten": "10",          # ÜBERSCHREITET HÖCHSTMENGE (6)
        "frequenz": "2x/Woche",
        "hausbesuch": "nein",
        "zuzahlung": "zuzahlungspflichtig"
    }

    print("=== Test 1: Korrektes Rezept ===")
    ergebnis = pruefe_rezept(test_korrekt)
    print(f"Status: {ergebnis.status}")
    print(f"Fehler: {ergebnis.fehler}")
    print(f"Warnungen: {ergebnis.warnungen}")

    print("\n=== Test 2: Rezept mit Fehlern ===")
    ergebnis2 = pruefe_rezept(test_fehler)
    print(f"Status: {ergebnis2.status}")
    print(f"Fehler: {ergebnis2.fehler}")
    print(f"Warnungen: {ergebnis2.warnungen}")
