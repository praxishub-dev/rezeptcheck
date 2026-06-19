"""
ki_zuordnung.py – RezeptCheck
==============================
Optionale KI-gestützte Feldzuordnung über die Anthropic Messages API.

Modus: Das Rezept-BILD wird an das Modell gesendet (nicht nur OCR-Text),
damit auch ANGEKREUZTE KÄSTCHEN zuverlässig erkannt werden – das ist mit
reinem OCR-Text nicht möglich. Claude liest gedrehte/kopfüber Vorlagen
selbst, daher keine Rotationskorrektur nötig; das Bild wird nur verkleinert.

WICHTIG – DATENSCHUTZ:
    In diesem Modus verlässt das gesamte Rezept-Bild den Mac. Auf Muster-13-
    Verordnungen stehen Gesundheitsdaten (Art. 9 DSGVO) + ärztliche Schweige-
    pflicht (§203 StGB). Produktiver Einsatz mit echten Patientendaten nur
    mit Auftragsverarbeitungsvertrag (AVV) und Zero-Data-Retention (ZDR).

    -> Standardmäßig DEAKTIVIERT. Testbetrieb nur mit Fantasie-Rezepten.

AKTIVIERUNG (erst nach AVV + ZDR für echte Daten):
    export ANTHROPIC_API_KEY="sk-ant-..."
    export REZEPTCHECK_KI=1
    (optional) export REZEPTCHECK_KI_MODELL="claude-sonnet-4-6"

ARCHITEKTUR:
    Das Modell extrahiert NUR die Felder + Kästchen-Zustände. Die Bewertung
    grün/gelb/rot bleibt vollständig im deterministischen rules_engine.
    Eine KI-Fehleinschätzung kann damit kein "rot" in ein "grün" drehen.
"""
import os
import io
import json
import base64
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
TIMEOUT = 60          # Sekunden (Bild-Analyse dauert länger als Text)
MAX_KANTE = 1568      # px; Claudes optimale lange Bildkante

# Felder inkl. Kästchen-Zustände, die rules_engine erwartet.
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
    "anzahl_fachbereich_kreuze",   # für Regel "genau ein Bereich"
    "icd10",
    "diagnosegruppe",
    "leitsymptomatik",             # "a"/"b"/"c"/kombi ODER "patientenindividuell" ODER ""
    "leitsymptomatik_freitext",    # Freitext, falls eingetragen
    "heilmittel",
    "anzahl_einheiten",
    "frequenz",
    "hausbesuch",                  # "ja"/"nein"/""  ("" = kein Kreuz = Mangel)
    "zuzahlung",
    "arzt_name",
    "arzt_stempel_block",
]

# Feld für KI-Plausibilitätshinweise (NICHT ampelrelevant, nur Anzeige/Warnung).
# Wird getrennt von FELDER gehalten, damit es nie als Extraktionswert in die
# Engine-Pflichtprüfung gerät.
HINWEIS_FELD = "ki_hinweise"

SYSTEM_PROMPT = """Du bist ein präziser Extraktor für deutsche Heilmittelverordnungen (Muster 13, GKV).
Du erhältst ein FOTO/SCAN einer EINZELNEN Verordnung (Seite 1). Werte das BILD aus.

Achte besonders auf ANGEKREUZTE KÄSTCHEN (X / Kreuz / Haken). Ein leeres Kästchen
ist NICHT angekreuzt. Unterscheide sorgfältig zwischen gesetztem und leerem Kreuz.

Antworte AUSSCHLIESSLICH mit einem JSON-Objekt. Kein Fließtext, keine Erklärung,
keine Markdown-Codeblöcke, keine Backticks. Verwende GENAU diese Schlüssel:

krankenkasse, patient_name, patient_vorname, patient_geburtsdatum, patient_adresse,
versichertennummer, kostentraegerkennung, status, bsnr, lanr, ausstellungsdatum,
unterschrift, fachbereich, anzahl_fachbereich_kreuze, icd10, diagnosegruppe,
leitsymptomatik, leitsymptomatik_freitext, heilmittel, anzahl_einheiten, frequenz,
hausbesuch, zuzahlung, arzt_name, arzt_stempel_block, ki_hinweise

REGELN:
- Wenn ein Feld nicht eindeutig lesbar/erkennbar ist: leerer String "". ERFINDE NICHTS.
- patient_name = Nachname, patient_vorname = Vorname (getrennt).
- patient_geburtsdatum / ausstellungsdatum: Format wie im Bild (TT.MM.JJ).
- bsnr / lanr: je 9-stellige Ziffernfolge (Betriebsstätten-Nr. / Arzt-Nr.).
- kostentraegerkennung: 9-stellig; versichertennummer: 1 Buchstabe + 9 Ziffern;
  status: 7-stellige Zahl.
- patient_adresse = Anschrift des Versicherten (oben links), NICHT die Arzt-/Klinik-
  adresse aus dem Stempel unten rechts.
- fachbereich: der EINE angekreuzte Heilmittelbereich (z.B. "Physiotherapie",
  "Ergotherapie", "Podologische Therapie", "Stimm-, Sprech-, Sprach- und Schlucktherapie",
  "Ernährungstherapie"). anzahl_fachbereich_kreuze: wie viele dieser Bereiche angekreuzt
  sind (als Zahl-String, normal "1").
- icd10: nur der Code (z.B. M54.10). diagnosegruppe: Kürzel (z.B. WS, PS3, SB1).
- leitsymptomatik: Welches der Kästchen ist angekreuzt? Mögliche Werte:
  "a", "b", "c", Kombinationen wie "a,b", ODER "patientenindividuell"
  (NUR wenn das Kästchen rechts neben "patientenindividuelle Leitsymptomatik"
  ein SICHTBARES Kreuz/X enthält), ODER "" wenn KEIN Kästchen angekreuzt ist.
  WICHTIG: Vorhandener Freitext bedeutet NICHT automatisch, dass das Kästchen
  "patientenindividuell" angekreuzt ist. Prüfe das Kästchen selbst. Wenn Freitext
  dasteht, aber das Kästchen daneben LEER ist, dann leitsymptomatik = "".
  Im Zweifel (Kästchen nicht eindeutig angekreuzt): leitsymptomatik = "".
- leitsymptomatik_freitext: der Freitext in der Zeile "Leitsymptomatik (patienten-
  individuelle ... als Freitext)", falls vorhanden, sonst "".
- heilmittel: Bezeichnung (auch Freitext wie "Psychisch funktionelle Behandlung")
  oder "BLANKOVERORDNUNG" wenn dieses Feld angekreuzt/eingetragen ist.
- anzahl_einheiten: nur die Zahl (z.B. "20").
- frequenz: z.B. "1-2", "1x wöch".
- hausbesuch: "ja" wenn das Ja-Kästchen angekreuzt ist, "nein" wenn das Nein-Kästchen
  angekreuzt ist, "" wenn keines angekreuzt ist.
- unterschrift: "vorhanden" wenn ein Arztstempel/Unterschriftsblock erkennbar ist, sonst "".
- arzt_stempel_block: "vorhanden" wenn unten rechts ein Arzt-/Praxisstempel ist, sonst "".
- arzt_name: Arztname aus dem Stempel.
- zuzahlung: "zuzahlungsfrei" wenn das Feld "Zuzahlungsfrei" angekreuzt ist,
  "zuzahlungspflichtig" wenn "Zuzahlungspflichtig" angekreuzt ist, sonst "".
- ki_hinweise: Eine LISTE von kurzen Hinweis-Strings (oder leere Liste []) zu
  AUFFÄLLIGKEITEN, die ein Mensch gegenpruefen sollte. Dies ist KEIN Urteil ueber
  gueltig/ungueltig, nur ein Hinweis. Stuetze dich NUR auf das, was im Bild steht
  - ERFINDE NICHTS. Moegliche Hinweise:
  * ICD-Code passt nicht zur daneben stehenden Klartext-Diagnose, z.B.
    "ICD M54.10 passt evtl. nicht zur Diagnose 'XYZ' - pruefen".
  * Code sieht nach OCR-Lesefehler aus (beginnt mit Ziffer statt Buchstabe),
    z.B. "ICD '140.5' beginnt mit Ziffer - evtl. L statt 1 verlesen? pruefen".
  * Leitsymptomatik-Freitext vorhanden, aber kein Kaestchen angekreuzt.
  * Behandlungsfrequenz fehlt oder ist unklar.
  Jeder Hinweis unter 15 Woertern. Wenn nichts auffaellt: []."""

AUFTRAG_TEXT = ("Extrahiere alle Felder dieser Muster-13-Verordnung aus dem Bild "
                "und gib sie als JSON zurück. Achte genau auf angekreuzte Kästchen.")


def ki_verfuegbar() -> bool:
    return KI_AKTIV and bool(API_KEY)


def _leeres_feldset() -> dict:
    return {k: "" for k in FELDER}


def _bild_als_base64(bild_pfad: str):
    """Lädt Bild, verkleinert auf MAX_KANTE, gibt (base64, media_type) zurück."""
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    img = Image.open(bild_pfad)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w, h = img.size
    if max(w, h) > MAX_KANTE:
        r = MAX_KANTE / max(w, h)
        img = img.resize((int(w * r), int(h * r)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii"), "image/png"


def _parse_json_antwort(text: str) -> dict:
    """Robustes Parsen: evtl. Markdown-Fences entfernen, JSON laden."""
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else s
        if s.endswith("```"):
            s = s[:-3]
        s = s.strip()
        if s.lower().startswith("json"):
            s = s[4:].strip()
    if not s.startswith("{"):
        a, b = s.find("{"), s.rfind("}")
        if a >= 0 and b > a:
            s = s[a:b + 1]
    return json.loads(s)


def _api_call(content_bloecke: list) -> dict:
    """Schickt content an die API, gibt geparstes Feld-Dict oder {} zurück."""
    body = json.dumps({
        "model": MODELL,
        "max_tokens": 1500,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": content_bloecke}],
    }).encode("utf-8")

    req = urllib.request.Request(
        API_URL, data=body, method="POST",
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

    felder = _leeres_feldset()
    for k in FELDER:
        v = felder_roh.get(k, "")
        felder[k] = v.strip() if isinstance(v, str) else ("" if v is None else str(v))
    # KI-Hinweise (Liste) separat übernehmen – NICHT ampelrelevant.
    hinweise_roh = felder_roh.get(HINWEIS_FELD, [])
    hinweise = []
    if isinstance(hinweise_roh, list):
        hinweise = [str(h).strip() for h in hinweise_roh if str(h).strip()]
    elif isinstance(hinweise_roh, str) and hinweise_roh.strip():
        hinweise = [hinweise_roh.strip()]
    felder[HINWEIS_FELD] = hinweise
    logger.info("KI-Zuordnung OK (Modell %s, Bild-Modus)%s", MODELL,
                f", {len(hinweise)} Hinweis(e)" if hinweise else "")
    return felder


def extrahiere_felder_ki(volltext: str = "", bild_pfad: str = None) -> dict:
    """
    Sendet das Rezept-BILD (+ optional OCR-Text als Hilfe) an die API.
    Bei jedem Fehler: {} -> der Aufrufer fällt auf Regex zurück.
    """
    if not ki_verfuegbar():
        return {}
    if not bild_pfad:
        logger.warning("KI-Bildmodus ohne Bildpfad – Regex-Fallback.")
        return {}

    try:
        b64, media_type = _bild_als_base64(bild_pfad)
    except Exception as e:
        logger.warning("KI: Bild nicht ladbar (%s) – Regex-Fallback.", e)
        return {}

    auftrag = AUFTRAG_TEXT
    if volltext.strip():
        auftrag += ("\n\nZur Unterstützung der lokale OCR-Text (das BILD ist maßgeblich, "
                    "der Text kann Fehler enthalten):\n" + volltext[:2000])

    content = [
        {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}},
        {"type": "text", "text": auftrag},
    ]
    return _api_call(content)
