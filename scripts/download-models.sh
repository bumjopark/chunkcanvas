#!/usr/bin/env bash
# Download parser models into services/api/.models so parsing never needs the network.
#   scripts/download-models.sh [python]     (default: services/api/.venv/bin/python)
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${1:-services/api/.venv/bin/python}"
DEST="${CC_MODELS_DIR:-services/api/.models}"
mkdir -p "$DEST"

# Docling layout + table structure models, and RapidOCR detection/classification.
"$(dirname "$PY")/docling-tools" models download layout tableformer rapidocr -o "$DEST"

# Korean PP-OCRv5 recognizer (also reads Latin text) for the optional CPU OCR parser.
KO="$DEST/RapidOcr-korean"
BASE="https://www.modelscope.cn/models/RapidAI/RapidOCR/resolve/v3.10.0"
mkdir -p "$KO"
curl -fsSL -o "$KO/korean_PP-OCRv5_rec_mobile.onnx" "$BASE/onnx/PP-OCRv5/rec/korean_PP-OCRv5_rec_mobile.onnx"
curl -fsSL -o "$KO/ppocrv5_korean_dict.txt" "$BASE/paddle/PP-OCRv5/rec/korean_PP-OCRv5_rec_mobile/ppocrv5_korean_dict.txt"
echo "cd6e2ea50f6943ca7271eb8c56a877a5a90720b7047fe9c41a2e541a25773c9b  $KO/korean_PP-OCRv5_rec_mobile.onnx" | sha256sum -c -
# Formula recognizer (pix2text-mfr 1.5, MIT): display formulas read from their image as LaTeX.
[ -f "$DEST/pix2text-mfr-1.5/model.safetensors" ] || "$PY" scripts/convert_pix2text.py "$DEST/pix2text-mfr-1.5"
echo "Models ready in $DEST"
