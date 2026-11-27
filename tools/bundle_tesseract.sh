#!/usr/bin/env bash
# Copy tesseract.exe, only the DLLs it loads, and the English data into $1.
# Used by the GitHub build so RBCAgent.zip works without installing Tesseract.
# Usage: bash tools/bundle_tesseract.sh release/RBCAgent/tesseract
set -euo pipefail
src="${TESSERACT_DIR:-/c/Program Files/Tesseract-OCR}"
out="$1"
mkdir -p "$out/tessdata"
cp "$src/tesseract.exe" "$out/"
# ldd lists every DLL tesseract.exe loads; keep the ones from the Tesseract folder.
ldd "$src/tesseract.exe" | sed -n 's/.*=> \(.*\) (0x.*/\1/p' | sort -u | while read -r dll; do
  case "$dll" in
    *Tesseract-OCR*) cp "$dll" "$out/" ;;
  esac
done
cp "$src/tessdata/eng.traineddata" "$out/tessdata/"
cp "$src/LICENSE" "$out/" 2>/dev/null || true
"$out/tesseract.exe" --version | head -1
ls "$out" | wc -l
