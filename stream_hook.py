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
# Partir solo en fin de oración real (.!?), NO en ':' — así no se cortan
# encabezados ni frases con dos puntos, evitando huecos innecesarios.
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_ENDS_TERM = re.compile(r"[.!?]\s*$")

# Detección de idioma (sesgada a español): debe coincidir con feengspeak._detect_lang.
_ES_HINT = {"el", "la", "los", "las", "un", "una", "de", "que", "y", "en", "por",
            "con", "para", "se", "no", "es", "está", "más", "cómo", "qué", "pero",
            "este", "esta", "su", "al", "del", "lo", "le", "ya", "como", "hay", "fue"}
_EN_HINT = {"the", "is", "are", "and", "to", "of", "in", "that", "it", "for",
            "with", "you", "this", "be", "on", "not", "we", "your", "can", "will",
            "here", "i", "have", "do", "was", "but", "they", "from", "at"}
_WORD_RE = re.compile(r"[a-záéíóúñü]+", re.IGNORECASE)


def _detect_lang(text):
    """es vs en-us, sesgado a español: inglés solo si la señal es clara."""
    es = 2 if re.search(r"[áéíóúñ¿¡]", text, re.IGNORECASE) else 0
    en = 0
    for w in _WORD_RE.findall(text.lower()):
        if w in _ES_HINT:
            es += 1
        elif w in _EN_HINT:
            en += 1
    return "en-us" if (en >= 2 and en > es + 1) else "es"


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


MAX_CODE_READ = 200   # bloques cercados más largos que esto se anuncian, no se leen
MIN_CHUNK = 60        # mínimo de caracteres por bloque enviado (evita fragmentos diminutos)


def _fenced(m):
    """Bloque cercado: corto se lee; largo (>200 chars o >5 líneas) se anuncia."""
    inner = m.group(1).strip()
    largo = len(inner) > MAX_CODE_READ or inner.count("\n") > 5
    return " bloque de código. " if largo else f" {inner}. "


def _clean_stream(raw):
    """Limpia para voz. Lee TODO lo que comunica el agente (inline, rutas,
    comandos); solo omite bloques de código largos. Si hay un fence sin cerrar,
    retiene desde ahí (no lee código a medio escribir)."""
    s = re.sub(r"```[^\n]*\n?([\s\S]*?)```", _fenced, raw)
    i = s.find("```")
    if i != -1:
        s = s[:i]                              # fence abierto: retén el resto
    s = re.sub(r"`([^`]+)`", r" \1 ", s)       # inline: leer el contenido
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"https?://\S+", " enlace ", s)
    s = re.sub(r"^\s*\d+\.\s+", "", s, flags=re.M)
    s = re.sub(r"^\s*[-•]\s+", "", s, flags=re.M)
    s = s.replace("|", " ")                    # tablas: leer celdas, quitar pipes
    s = re.sub(r"[*#>`]", "", s)               # markdown (conserva _ de identificadores)
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
        state = {"turn_id": tid, "message_id": None, "raw": "", "sent": 0, "lang": None}
    # Nuevo mensaje dentro del turno → reinicia buffer (sin cortar audio).
    if state.get("message_id") != mid:
        state["message_id"] = mid
        state["raw"] = ""
        state["sent"] = 0
        state["lang"] = None

    state["raw"] += delta
    cleaned = _clean_stream(state["raw"])
    parts = [p.strip() for p in _SENT_SPLIT.split(cleaned) if p.strip()]
    # La última oración es parcial salvo que el mensaje haya terminado o ya
    # cierre con un terminador.
    if parts and not final and not _ENDS_TERM.search(cleaned):
        parts = parts[:-1]

    # Agrupa oraciones en bloques de mínimo MIN_CHUNK caracteres antes de
    # mandarlas: evita fragmentos diminutos (ej. "Listo.") que generan un hueco
    # mientras se sintetiza el siguiente. Bloques más largos dejan que la
    # síntesis vaya adelantada → reproducción fluida sin pausas en los puntos.
    sent = state.get("sent", 0)
    unsent = parts[sent:]
    if unsent:
        if not _daemon_alive():
            # Pre-calienta sin bloquear el render; se reintenta al siguiente delta.
            _spawn_daemon()
        else:
            # Fija el idioma UNA vez por mensaje (desde todo el texto acumulado),
            # así no salta entre español e inglés a media respuesta.
            if not state.get("lang"):
                state["lang"] = _detect_lang(cleaned)
            lang = state["lang"]

            def emit(txt):
                return (not _speakable(txt)
                        or _send({"op": "speak_stream", "text": txt, "lang": lang}))

            chunk, clen, n = [], 0, 0
            for p in unsent:
                chunk.append(p); clen += len(p); n += 1
                if clen >= MIN_CHUNK:
                    if emit(" ".join(chunk)):
                        sent += n
                    chunk, clen, n = [], 0, 0
            # Sobrante: solo se manda si el mensaje ya terminó.
            if final and chunk and emit(" ".join(chunk)):
                sent += n

    state["sent"] = sent
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
