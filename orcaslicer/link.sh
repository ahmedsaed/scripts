#!/bin/bash
# Point OrcaSlicer (flatpak) user presets at the profiles submodule.
set -e
SRC="$(cd "$(dirname "$0")" && pwd)/profiles"
DST="$HOME/.var/app/com.orcaslicer.OrcaSlicer/config/OrcaSlicer/user/default"

git -C "$(dirname "$0")/.." submodule update --init orcaslicer/profiles
mkdir -p "$(dirname "$DST")"
if [ -e "$DST" ] && [ ! -L "$DST" ]; then
    mv "$DST" "$DST.bak.$(date +%s)"
fi
ln -sfn "$SRC" "$DST"
echo "Linked $DST -> $SRC"
