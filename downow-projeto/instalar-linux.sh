#!/usr/bin/env bash
# Instala o downow no menu do Linux Mint (só para o seu usuário).
# Coloque este script, o executável "downow-linux-x86_64", o icon.png e o downow.desktop na mesma pasta.
set -e
cd "$(dirname "$0")"
BIN=downow-linux-x86_64
[ -f "$BIN" ] || BIN=downow
mkdir -p ~/.local/bin ~/.local/share/applications ~/.local/share/icons/hicolor/512x512/apps
install -m 755 "$BIN" ~/.local/bin/downow
install -m 644 assets/icon.png ~/.local/share/icons/hicolor/512x512/apps/downow.png 2>/dev/null \
  || install -m 644 icon.png ~/.local/share/icons/hicolor/512x512/apps/downow.png
sed "s|^Exec=.*|Exec=$HOME/.local/bin/downow|" downow.desktop > ~/.local/share/applications/downow.desktop
update-desktop-database ~/.local/share/applications 2>/dev/null || true
gtk-update-icon-cache -f ~/.local/share/icons/hicolor 2>/dev/null || true
echo "downow instalado. Procure por 'downow' no menu de aplicativos."
