#!/usr/bin/env python3
"""Hook MessageDisplay de FeengSpeak (modo streaming).

Ligero a propósito (solo stdlib): dispara muchas veces durante el render, así
que NO importa numpy/kokoro ni bloquea la UI. Acumula los `delta` del mensaje,
detecta oraciones nuevas completas, y las encola en el daemon para que las lea
en orden mientras Claude sigue escribiendo.

Gated por config `stream_mode`: si está apagado, sale de inmediato.
"""
import fcntl
import json
import os
import re
import socket
import subprocess
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VENV_PYTHON = os.path.join(BASE_DIR, "venv", "bin", "python")
FEENGSPEAK = os.path.join(BASE_DIR, "feengspeak.py")
CONFIG_PATH = os.path.expanduser("~/.config/feengspeak/config.json")
RUNTIME_DIR = os.path.expanduser("~/.cache/feengspeak")
SOCK_PATH = os.path.join(RUNTIME_DIR, "daemon.sock")
STATE_PATH = os.path.join(RUNTIME_DIR, "stream_state.json")
LOCK_PATH = os.path.join(RUNTIME_DIR, "stream.lock")

_LETTER = re.compile(r"[A-Za-zÁÉÍÓÚÜáéíóúüÑñ]")
_SENT_SPLIT = re.compile(r"(?<=[.!?:])\s+")
_ENDS_TERM = re.compile(r"[.!?:]\s*$")


def _stream_on():
    try:
        with open(CONFIG_PATH) as f:
            return bool(json.load(f).get("stream_mode"))
    except Exception:
        return False


def _daemon_alive():
    if not os.path.exists(SOCK_PATH):
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.4)
        s.connect(SOCK_PATH)
        s.sendall(b'{"op":"ping"}\n')
        r = s.recv(64)
        s.close()
        return b'"ok"' in r
    except OSError:
        return False


def _spawn_daemon():
    try:
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        log = open(os.path.join(RUNTIME_DIR, "daemon.log"), "a")
        subprocess.Popen([VENV_PYTHON, FEENGSPEAK, "--daemon"],
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         start_new_session=True, close_fds=True)
    except Exception:
        pass


def _send(payload, timeout=0.8):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(SOCK_PATH)
        s.sendall(json.dumps(payload).encode() + b"\n")
        s.recv(128)
        s.close()
        return True
    except OSError:
        return False


def _clean_stream(raw):
    """Limpia para voz, conservando estado de bloque de código: si hay un fence
    sin cerrar, retiene desde ahí (no lee código a medio escribir)."""
    s = re.sub(r"```[\s\S]*?```", " ", raw)   # bloques completos fuera
    i = s.find("```")
    if i != -1:
        s = s[:i]                              # fence abierto: retén el resto
    s = re.sub(r"`[^`]*`", " ", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"^\s*\d+\.\s+", "", s, flags=re.M)
    s = re.sub(r"^\s*[-•]\s+", "", s, flags=re.M)
    s = re.sub(r"\|[^\n]+\|", " ", s)
    s = re.sub(r"[*_#>`|]", "", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _speakable(sent):
    return len(sent) >= 4 and _LETTER.search(sent) is not None


def main():
    if not _stream_on():
        return
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        return
    if data.get("hook_event_name") != "MessageDisplay":
        return

    tid = data.get("turn_id")
    mid = data.get("message_id")
    delta = data.get("delta") or ""
    final = bool(data.get("final"))

    os.makedirs(RUNTIME_DIR, exist_ok=True)
    lock = open(LOCK_PATH, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
    except OSError:
        pass

    try:
        with open(STATE_PATH) as f:
            state = json.load(f)
    except Exception:
        state = {}

    # Nuevo turno → corta lo que el daemon esté leyendo y reinicia.
    if state.get("turn_id") != tid:
        if _daemon_alive():
            _send({"op": "reset_stream"})
        state = {"turn_id": tid, "message_id": None, "raw": "", "spoken": []}
    # Nuevo mensaje dentro del turno → reinicia buffer (sin cortar audio).
    if state.get("message_id") != mid:
        state["message_id"] = mid
        state["raw"] = ""
        state["spoken"] = []

    state["raw"] += delta
    cleaned = _clean_stream(state["raw"])
    parts = [p.strip() for p in _SENT_SPLIT.split(cleaned) if p.strip()]
    # La última oración es parcial salvo que el mensaje haya terminado o ya
    # cierre con un terminador.
    if parts and not final and not _ENDS_TERM.search(cleaned):
        parts = parts[:-1]

    spoken = state["spoken"]
    new = [p for p in parts if p not in spoken and _speakable(p)]
    if new:
        if not _daemon_alive():
            # Pre-calienta sin bloquear el render; estas oraciones se reintentan
            # en el siguiente delta cuando el daemon ya esté listo.
            _spawn_daemon()
        else:
            for sent in new:
                if _send({"op": "speak_stream", "text": sent}):
                    spoken.append(sent)

    state["spoken"] = spoken
    try:
        with open(STATE_PATH, "w") as f:
            json.dump(state, f)
    except Exception:
        pass
    try:
        fcntl.flock(lock, fcntl.LOCK_UN)
    except OSError:
        pass


if __name__ == "__main__":
    main()
