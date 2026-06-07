"""
ampel.py
Steuert den Blink(1) USB-LED-Stick.
Farben: GRUEN, GELB, ROT
Fallback: Kein Absturz wenn kein Blink(1) angeschlossen.
"""

import logging

logger = logging.getLogger(__name__)

# Farben als RGB
FARBEN = {
    "GRUEN":  (0, 255, 0),
    "GELB":   (255, 180, 0),
    "ROT":    (255, 0, 0),
    "AUS":    (0, 0, 0),
}

_blink1 = None


def _get_blink1():
    """Lazy-Init des Blink(1)-Geräts."""
    global _blink1
    if _blink1 is not None:
        return _blink1
    try:
        from blink1.blink1 import Blink1
        _blink1 = Blink1()
        logger.info("Blink(1) gefunden und verbunden.")
        return _blink1
    except Exception as e:
        logger.warning(f"Blink(1) nicht verfügbar: {e}")
        return None


def zeige_farbe(status: str):
    """
    Schaltet die Ampel auf die dem Status entsprechende Farbe.
    Status: "GRUEN", "GELB", "ROT", "AUS"
    """
    farbe = FARBEN.get(status.upper(), FARBEN["AUS"])
    r, g, b = farbe

    geraet = _get_blink1()
    if geraet is None:
        # Kein Gerät – nur loggen, kein Absturz
        logger.info(f"[AMPEL SIMULATION] Status: {status} → RGB({r},{g},{b})")
        return

    try:
        geraet.fade_to_rgb(300, r, g, b)  # 300ms Überblendzeit
        logger.info(f"Ampel gesetzt: {status}")
    except Exception as e:
        logger.error(f"Fehler beim Setzen der Ampelfarbe: {e}")


def blinke(status: str, wiederholungen: int = 3):
    """
    Lässt die Ampel kurz blinken – z.B. bei neuem Scan.
    """
    geraet = _get_blink1()
    if geraet is None:
        logger.info(f"[AMPEL SIMULATION] Blinke: {status} x{wiederholungen}")
        return

    farbe = FARBEN.get(status.upper(), FARBEN["AUS"])
    r, g, b = farbe

    try:
        import time
        for _ in range(wiederholungen):
            geraet.fade_to_rgb(100, r, g, b)
            time.sleep(0.15)
            geraet.fade_to_rgb(100, 0, 0, 0)
            time.sleep(0.15)
        geraet.fade_to_rgb(200, r, g, b)
    except Exception as e:
        logger.error(f"Fehler beim Blinken: {e}")


def ampel_aus():
    """Schaltet die Ampel aus."""
    zeige_farbe("AUS")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("Teste Ampel (Blink1 muss angeschlossen sein)...")
    import time
    for s in ["ROT", "GELB", "GRUEN", "AUS"]:
        print(f"  → {s}")
        zeige_farbe(s)
        time.sleep(1)
