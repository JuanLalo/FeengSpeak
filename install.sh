#!/usr/bin/env bash
# Instalador reproducible de FeengSpeak: crea el venv, instala dependencias y
# descarga los modelos Kokoro. Idempotente — se puede correr varias veces.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "==> venv"
if [ ! -x venv/bin/python ]; then
  # El Python del sistema puede no traer ensurepip; arrancamos pip con get-pip.
  python3 -m venv --without-pip venv
  curl -fsSL https://bootstrap.pypa.io/get-pip.py -o /tmp/get-pip.py
  venv/bin/python /tmp/get-pip.py
fi

echo "==> dependencias"
venv/bin/python -m pip install -r requirements.txt

echo "==> modelos Kokoro"
mkdir -p models
REL="https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
[ -f models/kokoro-v1.0.onnx ] || curl -fL --progress-bar -o models/kokoro-v1.0.onnx "$REL/kokoro-v1.0.onnx"
[ -f models/voices-v1.0.bin ]  || curl -fL --progress-bar -o models/voices-v1.0.bin  "$REL/voices-v1.0.bin"

echo
echo "Instalado. Siguiente paso:"
echo "  venv/bin/python feengspeak.py setup    # instala los hooks en Claude Code"
echo "  (opcional) sudo apt install -y libportaudio2   # resaltado karaoke"
