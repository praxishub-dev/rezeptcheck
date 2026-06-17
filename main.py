"""
main.py
RezeptCheck – Hauptprogramm
Ablauf: Scan → OCR → Regelprüfung → Ampel + Bildschirmanzeige
"""

from __future__ import annotations
import logging
import sys
import threading
import tkinter as tk
from tkinter import font as tkfont
from pathlib import Path

# Projektpfade
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from core.ocr import scan_und_extrahiere
from core.rules_engine import pruefe_rezept, Pruefergebnis
from core.ampel import zeige_farbe, blinke, ampel_aus

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(ROOT / "rezeptcheck.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ── Update-Check beim Start ───────────────────────────────────────────────────

def pruefe_updates():
    """Zieht updates von GitHub (rules.json + Code) im Hintergrund."""
    try:
        import subprocess
        ergebnis = subprocess.run(
            ["git", "pull", "--quiet"],
            cwd=ROOT, capture_output=True, text=True, timeout=10
        )
        if "Already up to date" not in ergebnis.stdout:
            logger.info(f"Update eingespielt: {ergebnis.stdout.strip()}")
        else:
            logger.info("Kein Update verfügbar.")
    except Exception as e:
        logger.warning(f"Update-Check fehlgeschlagen (kein Internet?): {e}")


# ── GUI ───────────────────────────────────────────────────────────────────────

class RezeptCheckApp:

    FARBEN = {
        "GRUEN":    {"bg": "#1a7a1a", "fg": "white",  "symbol": "✓"},
        "GELB":     {"bg": "#b38600", "fg": "white",  "symbol": "⚠"},
        "ROT":      {"bg": "#a01010", "fg": "white",  "symbol": "✗"},
        "BEREIT":   {"bg": "#1e1e2e", "fg": "#aaaaaa","symbol": ""},
        "SCANNEN":  {"bg": "#0a3a6e", "fg": "white",  "symbol": "⟳"},
    }

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("RezeptCheck – Muster 13")
        self.root.configure(bg="#1e1e2e")
        self.root.geometry("800x600")
        self.root.resizable(True, True)

        self._baue_ui()
        self._zeige_bereit()

        # Update-Check im Hintergrund
        threading.Thread(target=pruefe_updates, daemon=True).start()

    def _baue_ui(self):
        # Statusbalken oben
        self.status_frame = tk.Frame(self.root, bg="#1e1e2e", height=180)
        self.status_frame.pack(fill="x", padx=20, pady=(20, 10))

        self.status_symbol = tk.Label(
            self.status_frame,
            text="", font=tkfont.Font(size=72),
            bg="#1e1e2e", fg="white"
        )
        self.status_symbol.pack()

        self.status_label = tk.Label(
            self.status_frame,
            text="Bereit zum Scannen",
            font=tkfont.Font(size=22, weight="bold"),
            bg="#1e1e2e", fg="#aaaaaa"
        )
        self.status_label.pack(pady=(0, 5))

        # Fehler-/Warnungsliste
        self.liste_frame = tk.Frame(self.root, bg="#1e1e2e")
        self.liste_frame.pack(fill="both", expand=True, padx=20, pady=5)

        self.liste_text = tk.Text(
            self.liste_frame,
            bg="#12121e", fg="white",
            font=tkfont.Font(size=14),
            relief="flat", bd=0,
            state="disabled",
            wrap="word",
            padx=15, pady=15
        )
        self.liste_text.pack(fill="both", expand=True)

        # Scan-Button
        self.scan_button = tk.Button(
            self.root,
            text="  Rezept scannen  ",
            font=tkfont.Font(size=18, weight="bold"),
            bg="#0055aa", fg="white",
            activebackground="#0077cc",
            relief="flat", bd=0,
            padx=30, pady=15,
            cursor="hand2",
            command=self._starte_scan
        )
        self.scan_button.pack(pady=20)

        # Tastenkürzel: Leertaste = Scan
        self.root.bind("<space>", lambda e: self._starte_scan())
        self.root.bind("<Return>", lambda e: self._starte_scan())

    def _zeige_bereit(self):
        self._update_anzeige("BEREIT", "Rezept auflegen, dann scannen", [])
        ampel_aus()

    def _update_anzeige(self, modus: str, haupttext: str, zeilen: list[str]):
        stil = self.FARBEN.get(modus, self.FARBEN["BEREIT"])
        self.root.configure(bg=stil["bg"])
        self.status_frame.configure(bg=stil["bg"])
        self.status_symbol.configure(bg=stil["bg"], fg=stil["fg"], text=stil["symbol"])
        self.status_label.configure(bg=stil["bg"], fg=stil["fg"], text=haupttext)
        self.liste_frame.configure(bg=stil["bg"])

        self.liste_text.configure(state="normal", bg="#12121e" if modus == "BEREIT" else stil["bg"])
        self.liste_text.delete("1.0", "end")
        for zeile in zeilen:
            self.liste_text.insert("end", f"• {zeile}\n")
        self.liste_text.configure(state="disabled")

    def _starte_scan(self, bild_pfad: "str | None" = None):
        """Scan im Hintergrund-Thread starten (UI bleibt responsiv)."""
        self.scan_button.configure(state="disabled")
        self._update_anzeige("SCANNEN", "Scannen...", [])
        threading.Thread(
            target=self._scan_worker,
            args=(bild_pfad,),
            daemon=True
        ).start()

    def _scan_worker(self, bild_pfad: "str | None"):
        """Läuft im Hintergrund-Thread."""
        try:
            logger.info("Scan gestartet.")

            # 1. Scannen + OCR
            daten = scan_und_extrahiere(bild_pfad)

            if not daten:
                self.root.after(0, lambda: self._zeige_fehler_allgemein(
                    "Scanner nicht erreichbar oder OCR fehlgeschlagen."
                ))
                return

            # 2. Regelprüfung
            ergebnis: Pruefergebnis = pruefe_rezept(daten)
            logger.info(f"Prüfergebnis: {ergebnis.status}, {len(ergebnis.fehler)} Fehler, {len(ergebnis.warnungen)} Warnungen")

            # 3. UI + Ampel aktualisieren (zurück im Main-Thread)
            self.root.after(0, lambda: self._zeige_ergebnis(ergebnis))

        except Exception as e:
            logger.exception(f"Unerwarteter Fehler im Scan-Worker: {e}")
            self.root.after(0, lambda: self._zeige_fehler_allgemein(str(e)))

    def _zeige_ergebnis(self, ergebnis: Pruefergebnis):
        """Aktualisiert UI und Ampel mit dem Prüfergebnis."""
        if ergebnis.status == "GRUEN":
            self._update_anzeige("GRUEN", "Rezept korrekt", [])
            blinke("GRUEN", 2)
            zeige_farbe("GRUEN")

        elif ergebnis.status == "GELB":
            zeilen = ergebnis.warnungen
            self._update_anzeige("GELB", f"{len(zeilen)} Hinweis(e)", zeilen)
            blinke("GELB", 2)
            zeige_farbe("GELB")

        elif ergebnis.status == "ROT":
            zeilen = ergebnis.fehler + (
                [f"⚠ {w}" for w in ergebnis.warnungen] if ergebnis.warnungen else []
            )
            self._update_anzeige("ROT", f"{len(ergebnis.fehler)} Fehler gefunden", zeilen)
            blinke("ROT", 3)
            zeige_farbe("ROT")

        self.scan_button.configure(state="normal")

        # Nach 30 Sekunden automatisch zurück auf "Bereit"
        self.root.after(30000, self._zeige_bereit)

    def _zeige_fehler_allgemein(self, meldung: str):
        self._update_anzeige("ROT", "Systemfehler", [meldung])
        zeige_farbe("ROT")
        self.scan_button.configure(state="normal")


# ── Einstiegspunkt ────────────────────────────────────────────────────────────

def main():
    root = tk.Tk()
    app = RezeptCheckApp(root)

    # Testmodus: Bilddatei als Argument übergeben
    if len(sys.argv) > 1:
        bild = sys.argv[1]
        logger.info(f"Testmodus: Bild '{bild}' wird verarbeitet.")
        root.after(500, lambda: app._starte_scan(bild))

    root.mainloop()


if __name__ == "__main__":
    main()
