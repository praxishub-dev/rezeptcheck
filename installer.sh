#!/bin/bash
# installer.sh
# RezeptCheck – Einmal-Installer für macOS
# Ausführen mit: bash installer.sh

set -e

echo ""
echo "╔══════════════════════════════════════╗"
echo "║     RezeptCheck – Installer          ║"
echo "║     Muster 13 Prüfsystem             ║"
echo "╚══════════════════════════════════════╝"
echo ""

# ── 1. Xcode Command Line Tools (für git) ────────────────────────────────────
if ! command -v git &> /dev/null; then
    echo "→ Installiere Xcode Command Line Tools..."
    xcode-select --install
    echo "  Bitte Installation abwarten, dann Skript erneut ausführen."
    exit 1
fi

# ── 2. Homebrew ──────────────────────────────────────────────────────────────
if ! command -v brew &> /dev/null; then
    echo "→ Installiere Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
fi

# ── 3. Python 3 ──────────────────────────────────────────────────────────────
if ! command -v python3 &> /dev/null; then
    echo "→ Installiere Python 3..."
    brew install python3
fi

echo "→ Python: $(python3 --version)"

# ── 3b. Tesseract OCR + deutsche Sprachdaten + Poppler (PDF) ─────────────────
if ! command -v tesseract &> /dev/null; then
    echo "→ Installiere Tesseract OCR..."
    brew install tesseract tesseract-lang
else
    echo "→ Tesseract bereits installiert."
fi

if ! command -v pdftoppm &> /dev/null; then
    echo "→ Installiere Poppler (für PDF-Konvertierung)..."
    brew install poppler
fi

# Prüfen ob deutsche Sprachdaten vorhanden
if ! tesseract --list-langs 2>/dev/null | grep -q "deu"; then
    echo "→ Installiere deutsche Sprachdaten für Tesseract..."
    brew install tesseract-lang
fi

# ── 4. Python-Abhängigkeiten ─────────────────────────────────────────────────
echo "→ Installiere Python-Pakete..."
pip3 install --quiet --break-system-packages \
    blink1 \
    pillow

echo "  Pakete installiert."

# ── 5. App-Verzeichnis ───────────────────────────────────────────────────────
APP_DIR="$HOME/RezeptCheck"

if [ -d "$APP_DIR" ]; then
    echo "→ Update: Repository aktualisieren..."
    cd "$APP_DIR" && git pull --quiet
else
    echo "→ Repository klonen..."
    # Hier deine GitHub-URL eintragen:
    git clone https://github.com/praxishub-dev/rezeptcheck.git "$APP_DIR"
fi

# ── 6. Autostart (LaunchAgent) ───────────────────────────────────────────────
PLIST="$HOME/Library/LaunchAgents/de.specht-waschkowski.rezeptcheck.plist"

cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>de.specht-waschkowski.rezeptcheck</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/python3</string>
        <string>$APP_DIR/main.py</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>$APP_DIR/rezeptcheck.log</string>
    <key>StandardErrorPath</key>
    <string>$APP_DIR/rezeptcheck_error.log</string>
</dict>
</plist>
EOF

launchctl load "$PLIST" 2>/dev/null || true
echo "→ Autostart eingerichtet."

# ── 7. Desktop-Verknüpfung ───────────────────────────────────────────────────
cat > "$HOME/Desktop/RezeptCheck.command" << EOF
#!/bin/bash
cd "$APP_DIR"
git pull --quiet
python3 main.py
EOF
chmod +x "$HOME/Desktop/RezeptCheck.command"
echo "→ Desktop-Verknüpfung erstellt."

echo ""
echo "✓ Installation abgeschlossen!"
echo ""
echo "  Starten:    Doppelklick auf 'RezeptCheck' auf dem Desktop"
echo "  Autostart:  Läuft automatisch beim Login"
echo "  Updates:    Werden beim Start automatisch eingespielt"
echo ""
