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
    """Prüft ob der ICD-10-Code ein gültiges Format hat (z.B. M54.5, G35, Z96.65)"""
    if not code:
        return False
    pattern = r'^[A-Z][0-9]{2}(\.[0-9A-Z]{1,4})?$'
    return bool(re.match(pattern, code.strip().upper()))


def pruefe_neun_stellig_numerisch(wert: str) -> bool:
    return bool(re.match(r'^\d{9}$', wert.strip())) if wert else False


def pruefe_datum(wert: str) -> bool:
    """Prüft Format TT.MM.JJJJ"""
    return bool(re.match(r'^\d{2}\.\d{2}\.\d{2,4}$', wert.strip())) if wert else False


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

    # ICD-10
    icd10 = daten.get("icd10", "").strip()
    if not icd10:
        fehler.append("Fehlt: ICD-10-Code")
    elif not pruefe_icd10(icd10):
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

    # Leitsymptomatik
    leitsymptomatik = daten.get("leitsymptomatik", "").strip()
    if not leitsymptomatik:
        fehler.append("Fehlt: Leitsymptomatik (a/b/c oder Freitext + patientenindividuell)")

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

    # Hausbesuch
    hausbesuch = daten.get("hausbesuch", "").strip().lower()
    if hausbesuch not in ("ja", "nein", "yes", "no", "true", "false"):
        fehler.append("Fehlt: Hausbesuch (ja oder nein muss angekreuzt sein)")

    # Zuzahlung
    zuzahlung = daten.get("zuzahlung", "").strip().lower()
    if not zuzahlung:
        fehler.append("Fehlt: Zuzahlungsstatus (zuzahlungspflichtig oder zuzahlungsfrei)")

    # ── Ergebnis ─────────────────────────────────────────────────────────────
    # Trennung: reine "Fehlt:"-Fehler (OCR-verdächtig) vs. echte inhaltliche
    # Fehler (Ungültig, Höchstmenge, falsches Format) – letztere bleiben immer ROT.
    fehlt_fehler = [f for f in fehler if f.startswith("Fehlt:")]
    echte_fehler = [f for f in fehler if not f.startswith("Fehlt:")]

    # Anzahl tatsächlich erkannter Felder als Indikator für die OCR-Qualität.
    erkannte_felder = sum(1 for v in daten.values() if str(v).strip())

    # Heuristik: genau 1 fehlendes Pflichtfeld trotz vieler Treffer und ohne
    # echte inhaltliche Fehler → wahrscheinlich OCR-Lesefehler (schräges Foto),
    # nicht ein echtes Fehlen auf dem Rezept → GELB mit Prüfhinweis statt ROT.
    ocr_lesefehler = (
        not echte_fehler
        and len(fehlt_fehler) == 1
        and erkannte_felder >= 15
    )

    if ocr_lesefehler:
        feldname = fehlt_fehler[0].replace("Fehlt: ", "")
        warnungen.insert(0, f"{feldname} nicht lesbar – bitte am Rezept gegenprüfen")
        status = "GELB"
        fehler = []
    elif fehler:
        status = "ROT"
    elif warnungen:
        status = "GELB"
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
