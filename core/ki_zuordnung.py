"""
ki_zuordnung.py – RezeptCheck
==============================
Optionale KI-gestützte Feldzuordnung über die Anthropic Messages API.

WICHTIG – DATENSCHUTZ:
    Dieses Modul sendet den per Apple Vision lokal erkannten OCR-TEXT
    (nicht das Bild) an die Anthropic API. Auf Muster-13-Verordnungen
    stehen Gesundheitsdaten (Art. 9 DSGVO) und sie unterliegen der
    ärztlichen Schweigepflicht (§203 StGB). Der produktive Einsatz mit
    echten Patientendaten ist nur zulässig mit Auftragsverarbeitungs-
    vertrag (AVV) und Zero-Data-Retention (ZDR) von Anthropic.

    -> Standardmäßig ist dieses Modul DEAKTIVIERT.
    -> Testbetrieb nur mit anonymisierten Fantasie-Rezepten.

AKTIVIERUNG (erst nach AVV + ZDR für echte Daten):
    export ANTHROPIC_API_KEY="sk-ant-..."
    export REZEPTCHECK_KI=1
    (optional) export REZEPTCHECK_KI_MODELL="claude-sonnet-4-6"

ARCHITEKTUR:
    Das Modell extrahiert NUR die Felder (Zuordnung). Die Bewertung
    grün/gelb/rot bleibt vollständig im deterministischen rules_engine.
    Damit kann eine KI-Halluzination kein "rot" in ein "grün" drehen.
"""
import os
import json
import logging
import urllib.request
import urllib.error

logger = logging.getLogger(__name__)

# ── Konfiguration über Umgebungsvariablen ────────────────────────────────────
API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
KI_AKTIV = os.environ.get("REZEPTCHECK_KI", "0").strip() == "1"
MODELL = os.environ.get("REZEPTCHECK_KI_MODELL", "claude-sonnet-4-6").strip()

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
TIMEOUT = 30  # Sekunden

# Exakt die Feld-Keys, die rules_engine / die UI erwarten.
FELDER = [
    "krankenkasse",
    "patient_name",
    "patient_vorname",
    "patient_geburtsdatum",
    "patient_adresse",
    "versichertennummer",
    "kostentraegerkennung",
    "status",
    "bsnr",
    "lanr",
    "ausstellungsdatum",
    "unterschrift",
    "fachbereich",
    "icd10",
    "diagnosegruppe",
    "leitsymptomatik",
    "heilmittel",
    "anzahl_einheiten",
    "frequenz",
    "hausbesuch",
    "zuzahlung",
    "arzt_name",
]

SYSTEM_PROMPT = """Du bist ein Extraktor für deutsche Heilmittelverordnungen (Muster 13, GKV).
Du erhältst den per OCR ausgelesenen Text einer EINZELNEN Verordnung.
Deine Aufgabe: Ordne die erkennbaren Angaben den vorgegebenen Feldern zu.

REGELN:
- Antworte AUSSCHLIESSLICH mit einem JSON-Objekt. Kein Fließtext, keine Erklärung,
  keine Markdown-Codeblöcke, keine Backticks.
- Verwende GENAU diese Schlüssel (alle müssen vorkommen):
  krankenkasse, patient_name, patient_vorname, patient_geburtsdatum, patient_adresse,
  versichertennummer, kostentraegerkennung, status, bsnr, lanr, ausstellungsdatum,
  unterschrift, fachbereich, icd10, diagnosegruppe, leitsymptomatik, heilmittel,
  anzahl_einheiten, frequenz, hausbesuch, zuzahlung, arzt_name
- Wenn ein Feld im Text NICHT eindeutig lesbar ist: leerer String "".
- ERFINDE NICHTS. Rate keine Werte. Im Zweifel leerer String.
- Werte als reiner Text, ohne Beschriftung. Beispiele:
  - patient_name: Nachname; patient_vorname: Vorname (getrennt)
  - patient_geburtsdatum / ausstellungsdatum: Format TT.MM.JJ wie im Text
  - bsnr / lanr: jeweils 9-stellige Ziffernfolge (Betriebsstätten-Nr. / Arzt-Nr.)
  - kostentraegerkennung: 9-stellig; versichertennummer: 1 Buchstabe + 9 Ziffern
  - status: die 7-stellige Versichertenstatus-Zahl
  - icd10: nur der ICD-10-Code (z.B. M54.10); diagnosegruppe: Kürzel (z.B. WS, PS3, SB1)
  - heilmittel: Bezeichnung, auch Freitext (z.B. "Psychisch funktionelle Behandlung")
    oder "BLANKOVERORDNUNG", wenn angekreuzt
  - anzahl_einheiten: nur die Zahl (z.B. "20")
  - hausbesuch: "ja" oder "nein" (was angekreuzt ist)
  - unterschrift: "vorhanden", wenn ein Arztstempel/Unterschriftsblock erkennbar ist, sonst ""
  - fachbereich: z.B. "Physiotherapie", "Ergotherapie", wenn angekreuzt
  - arzt_name: Name aus dem Arztstempel
  - zuzahlung: "frei" wenn zuzahlungsbefreit angekreuzt, sonst ""
- Trenne Patientenadresse vom Arzt-/Klinikstempel. patient_adresse = Anschrift des
  Versicherten (oben), NICHT die Klinik-/Praxisadresse des Arztes (unten)."""


def ki_verfuegbar() -> bool:
    """True nur, wenn Schalter aktiv UND API-Key gesetzt."""
    return KI_AKTIV and bool(API_KEY)


def _leeres_feldset() -> dict:
    return {k: "" for k in FELDER}


def _parse_json_antwort(text: str) -> dict:
    """Robustes Parsen: evtl. Markdown-Fences entfernen, JSON laden."""
    s = text.strip()
    # ```json ... ``` oder ``` ... ``` abstreifen
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[: -3]
        s = s.strip()
        # evtl. führendes "json"
        if s.lower().startswith("json"):
            s = s[4:].strip()
    # Falls Text drumherum: erstes { bis letztes }
    if not s.startswith("{"):
        a, b = s.find("{"), s.rfind("}")
        if a >= 0 and b > a:
            s = s[a : b + 1]
    return json.loads(s)


def extrahiere_felder_ki(volltext: str) -> dict:
    """
    Sendet den OCR-Text an die Anthropic API und gibt das Feld-Dict zurück.
    Bei jedem Fehler: leeres Dict {} -> der Aufrufer fällt auf Regex zurück.
    """
    if not ki_verfuegbar():
        return {}

    body = json.dumps({
        "model": MODELL,
        "max_tokens": 1024,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": volltext}],
    }).encode("utf-8")

    req = urllib.request.Request(
        API_URL,
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": API_KEY,
            "anthropic-version": API_VERSION,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            roh = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:300]
        except Exception:
            pass
        logger.warning("KI-API HTTP-Fehler %s: %s", e.code, detail)
        return {}
    except Exception as e:
        logger.warning("KI-API nicht erreichbar: %s", e)
        return {}

    # Antwort: {"content":[{"type":"text","text":"..."}], ...}
    try:
        data = json.loads(roh)
        bloecke = data.get("content", [])
        text = "".join(b.get("text", "") for b in bloecke if b.get("type") == "text")
        if not text.strip():
            logger.warning("KI-API: leere Textantwort")
            return {}
        felder_roh = _parse_json_antwort(text)
    except Exception as e:
        logger.warning("KI-API: Antwort nicht parsebar: %s", e)
        return {}

    # Auf erwartete Keys normalisieren (fehlende ergänzen, Fremdkeys verwerfen)
    felder = _leeres_feldset()
    for k in FELDER:
        v = felder_roh.get(k, "")
        felder[k] = v.strip() if isinstance(v, str) else ("" if v is None else str(v))
    logger.info("KI-Zuordnung OK (Modell %s)", MODELL)
    return felder
