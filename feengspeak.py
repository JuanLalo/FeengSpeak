#!/usr/bin/env python3
"""
FeengSpeak — herramienta interna de voz de Feengster para Claude Code.

Lee en voz alta las respuestas de Claude Code, en español, 100% local
(sin API keys, sin nube). Motor Kokoro vía ONNX (kokoro-onnx). Se instala
como hook Stop de Claude Code y, si hay PortAudio, muestra resaltado
karaoke palabra-por-palabra en la terminal.

Fork interno de "claude-voice" (MIT, © 2026 Null-Phnix). Ver NOTICE.
Adaptaciones Feengster: backend torch→ONNX, voces en español, extracción
robusta del transcript, branding.

Comandos:
    feengspeak setup            Instala los hooks en Claude Code
    feengspeak demo             Demo para grabar pantalla
    feengspeak on / off         Activa o desactiva la voz
    feengspeak --voices         Lista las voces disponibles
    feengspeak --voice em_alex "texto"   Habla con una voz específica
    feengspeak daemon-status    Estado del daemon
    feengspeak daemon-stop      Detiene el daemon
"""
import argparse
import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import termios
import queue
import threading
import time
import tty
import wave
import warnings

warnings.filterwarnings("ignore")
os.environ["TOKENIZERS_PARALLELISM"] = "false"

import numpy as np

# sounddevice requiere PortAudio (libportaudio2). Si no está, degradamos a aplay.
try:
    import sounddevice as sd
    HAVE_SD = True
except (OSError, ImportError):
    sd = None
    HAVE_SD = False

# ── rutas / marca ──
# BASE_DIR = el directorio de la app (repo). Relocatable: venv y models son
# relativos al script, así mover el repo no rompe rutas.
SCRIPT_PATH = os.path.abspath(__file__)
BASE_DIR = os.path.dirname(SCRIPT_PATH)
MODEL_PATH = os.path.join(BASE_DIR, "models", "kokoro-v1.0.onnx")
VOICES_PATH = os.path.join(BASE_DIR, "models", "voices-v1.0.bin")
VENV_PYTHON = os.path.join(BASE_DIR, "venv", "bin", "python")

# ── defaults ──
DEFAULT_VOICE = "ef_dora"          # voz española por defecto
DEFAULT_VOICE_EN = "am_michael"    # voz inglesa US por defecto (auto-detección)
LANG = "es"                        # idioma Kokoro primario
SAMPLE_RATE = 24000
WINDOW = 8
MIN_CHARS = 30
MAX_CHARS = 1500
DONE_PAUSE = 0.5
CHIME_ENABLED = True
TRANSCRIPT_FLUSH_WAIT = 0.4        # espera a que el transcript termine de escribirse
CONFIG_PATH = os.path.expanduser("~/.config/feengspeak/config.json")
SETTINGS_PATH = os.path.expanduser("~/.claude/settings.json")

# ── daemon / runtime ──
RUNTIME_DIR = os.path.expanduser("~/.cache/feengspeak")
SOCK_PATH = os.path.join(RUNTIME_DIR, "daemon.sock")
PID_PATH = os.path.join(RUNTIME_DIR, "daemon.pid")
LOG_PATH = os.path.join(RUNTIME_DIR, "daemon.log")
DAEMON_IDLE_TIMEOUT = 1800
DAEMON_SPAWN_WAIT = 20             # ONNX carga rápido, pero damos margen
ACTIVE_TTY_STALE = 600

# ── stubs de reanudación que no vale la pena leer ──
STUB_MESSAGES = {
    "no response requested.",
    "no response requested",
}
STUB_PREFIXES = ("continuing from where you left off",)

# ── correcciones de pronunciación (términos técnicos -> fonética española) ──
PRONOUNCE = {
    "Claude": "Clod",
    "hooks": "jucs", "hook": "juc",
    "commits": "cómits", "commit": "cómit",
    "deploy": "diplóy",
    "bugs": "bags", "bug": "bag",
    "branch": "branch",
    "merge": "merch",
    "Piper": "Paiper",
    "Kokoro": "Kokoro",
    "npm": "ene pe eme",
    "jq": "jota kiú",
    "CLI": "ce ele i", "API": "a pe i", "CPU": "ce pe u", "GPU": "ge pe u",
    "URL": "u erre ele", "SQL": "ese cu ele", "SSH": "ese ese hache",
    "JSON": "yeson", "YAML": "iamal", "HTTP": "hache te te pe",
    "TTS": "te te ese", "LLM": "ele ele eme", "MCP": "eme ce pe",
    "stdout": "standard out", "stderr": "standard error",
    "ONNX": "o ene ene equis",
}

# ── ANSI ──
RESET = "\033[0m"; DIM = "\033[2m"; BOLD = "\033[1m"
HIGHLIGHT = "\033[1;38;2;120;200;255m"; UNDERLINE = "\033[4m"
NEAR = "\033[38;2;80;150;210m"; SPOKEN = "\033[38;2;65;65;85m"
LABEL = "\033[38;2;90;90;120m"
BAR_FILL = "\033[38;2;120;200;255m"; BAR_EMPTY = "\033[38;2;40;40;55m"
HIDE_CURSOR = "\033[?25l"; SHOW_CURSOR = "\033[?25h"
GREEN = "\033[38;2;100;220;100m"; RED = "\033[38;2;220;80;80m"
CYAN = "\033[38;2;120;200;255m"

_pipe = None
_tty = None
_interrupted = False
_config = None
_tty_fd = None
_old_term = None
_aplay_proc = None

_playback_lock = threading.Lock()
_daemon_idle_t0 = 0.0
_active_tty = None
_active_tty_t = 0.0
_muted_ttys = set()

# Modo streaming: pipeline de dos etapas alimentado por el hook MessageDisplay.
# _stream_q = texto entrante; _play_q = audio ya sintetizado listo para sonar.
# Separar síntesis de reproducción evita el hueco: la oración siguiente se
# sintetiza MIENTRAS suena la actual (sin pausa larga en cada punto).
_stream_q = queue.Queue()
_play_q = queue.Queue(maxsize=4)

# Voces en español de Kokoro.
VOICE_LIST = {
    "ef_dora": "Español, femenina, cálida",
    "em_alex": "Español, masculina, natural",
    "em_santa": "Español, masculina, grave",
    "am_michael": "Inglés US, masculina, natural",
    "am_adam": "Inglés US, masculina",
    "am_onyx": "Inglés US, masculina, profunda",
    "af_heart": "Inglés US, femenina, cálida",
    "af_nova": "Inglés US, femenina, clara",
}

DEMO_TEXT = (
    "Listo. Ambos repos quedaron en GitHub con historial limpio. "
    "La herramienta interna ahora lee mis respuestas en voz alta, en español, "
    "completamente local, sin claves de API y sin nube. "
    "Esto es FeengSpeak corriendo con Kokoro."
)


# ── terminal ──
def _restore_terminal():
    global _old_term, _tty_fd
    if _old_term is not None and _tty_fd is not None:
        try:
            termios.tcsetattr(_tty_fd, termios.TCSADRAIN, _old_term)
        except (termios.error, OSError):
            pass
        _old_term = None


def _stop_playback():
    """Detiene la reproducción en curso, con o sin sounddevice."""
    global _aplay_proc
    if HAVE_SD:
        sd.stop()
    if _aplay_proc is not None and _aplay_proc.poll() is None:
        try:
            _aplay_proc.terminate()
        except OSError:
            pass


def _handle_signal(sig, frame):
    global _interrupted
    _interrupted = True
    _stop_playback()
    _restore_terminal()
    if _tty:
        try:
            _tty.write("\033[1A\r\033[K\033[1A\r\033[K\033[1A\r\033[K")
            _tty.write(SHOW_CURSOR)
            _tty.flush()
        except (OSError, ValueError):
            pass
    sys.exit(0)


signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


def _start_keypress_listener(tty_path="/dev/tty"):
    global _tty_fd, _old_term

    def _listen():
        global _interrupted, _tty_fd, _old_term
        fd = None
        try:
            fd = os.open(tty_path, os.O_RDONLY)
            _tty_fd = fd
            _old_term = termios.tcgetattr(fd)
            tty.setraw(fd)
            while not _interrupted:
                ready, _, _ = select.select([fd], [], [], 0.1)
                if ready:
                    os.read(fd, 1)
                    _interrupted = True
                    _stop_playback()
                    break
        except (OSError, termios.error):
            pass
        finally:
            _restore_terminal()
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    t = threading.Thread(target=_listen, daemon=True)
    t.start()
    return t


# ── config ──
def load_config():
    global _config
    if _config is not None:
        return _config
    defaults = {
        "voice": DEFAULT_VOICE, "min_chars": MIN_CHARS, "max_chars": MAX_CHARS,
        "window": WINDOW, "chime": CHIME_ENABLED, "done_pause": DONE_PAUSE,
        "enabled": True, "use_daemon": True,
        "english_terms": True, "speed": 0.93,
        "auto_lang": True, "voice_en": DEFAULT_VOICE_EN,
    }
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                defaults.update(json.load(f))
        except (json.JSONDecodeError, OSError):
            pass
    _config = defaults
    return _config


def save_config(cfg):
    global _config
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    _config = cfg


def save_default_config():
    if not os.path.exists(CONFIG_PATH):
        save_config({
            "voice": DEFAULT_VOICE, "min_chars": MIN_CHARS,
            "max_chars": MAX_CHARS, "chime": True, "enabled": True,
        })


# ── modelo (kokoro-onnx) ──
def get_pipe():
    global _pipe
    if _pipe is None:
        # Apunta phonemizer a la librería espeak-ng incluida (no depende del sistema).
        try:
            import espeakng_loader
            from phonemizer.backend.espeak.wrapper import EspeakWrapper
            EspeakWrapper.set_library(espeakng_loader.get_library_path())
            EspeakWrapper.set_data_path(espeakng_loader.get_data_path())
        except Exception:
            pass
        from kokoro_onnx import Kokoro
        _pipe = Kokoro(MODEL_PATH, VOICES_PATH)
    return _pipe


def get_tty(tty_path="/dev/tty"):
    global _tty
    try:
        _tty = open(tty_path, "w")
    except OSError:
        _tty = sys.stderr
    return _tty


# ── chimes (solo con sounddevice) ──
def play_chime_start():
    if not HAVE_SD:
        return
    sr = 44100
    t = np.linspace(0, 0.08, int(sr * 0.08), False)
    freq = np.linspace(600, 900, len(t))
    tone = np.sin(2 * np.pi * freq * t) * 0.15
    fade = np.minimum(t / 0.02, 1.0) * np.minimum((0.08 - t) / 0.02, 1.0)
    sd.play((tone * fade).astype(np.float32), samplerate=sr); sd.wait()


def play_chime_end():
    if not HAVE_SD:
        return
    sr = 44100
    t = np.linspace(0, 0.08, int(sr * 0.08), False)
    freq = np.linspace(900, 600, len(t))
    tone = np.sin(2 * np.pi * freq * t) * 0.12
    fade = np.minimum(t / 0.02, 1.0) * np.minimum((0.08 - t) / 0.02, 1.0)
    sd.play((tone * fade).astype(np.float32), samplerate=sr); sd.wait()


# ── procesamiento de texto ──


MAX_CODE_READ = 200   # bloques cercados más largos que esto se anuncian, no se leen


def _fenced(m):
    """Bloque cercado: corto se lee; largo (>200 chars o >5 líneas) se anuncia."""
    inner = m.group(1).strip()
    largo = len(inner) > MAX_CODE_READ or inner.count('\n') > 5
    return ' bloque de código. ' if largo else f' {inner}. '


def clean_for_speech(text):
    text = re.sub(r'```[^\n]*\n?([\s\S]*?)```', _fenced, text)
    text = re.sub(r'`([^`]+)`', r' \1 ', text)         # inline: leer contenido
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'https?://\S+', ' enlace ', text)
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*[-•]\s+', '', text, flags=re.MULTILINE)
    text = text.replace('|', ' ')                       # tablas: leer celdas
    text = re.sub(r'[*#>]', '', text)                   # markdown (conserva _)
    text = re.sub(r'\n{2,}', '. ', text)
    text = re.sub(r'\n', ' ', text)
    text = re.sub(r'\s{2,}', ' ', text)
    text = re.sub(r'-{2,}', ' ', text)
    return text.strip()


def is_stub(text):
    t = " ".join(text.lower().split()).strip()
    return t in STUB_MESSAGES or any(t.startswith(p) for p in STUB_PREFIXES)


def fix_pronunciation(text):
    for term, replacement in PRONOUNCE.items():
        text = re.sub(rf'\b{re.escape(term)}\b', replacement, text)
    return text


# Términos técnicos en inglés que se pronuncian EN INGLÉS (fonemización en-us).
# Editable: agrega/quita palabras. La detección es por palabra completa, sin
# distinguir mayúsculas. Si una palabra NO está aquí, se lee en español.
EN_TERMS = {
    # Nombres propios / productos (siempre en inglés)
    "claude", "anthropic", "feengspeak", "kokoro", "piper", "github", "git",
    "onnx", "python", "warp", "linux", "ubuntu", "docker", "kubernetes",
    # Jerga git / devops fuertemente anglo
    "commit", "commits", "deploy", "deployment", "branch", "branches", "merge",
    "push", "pull", "request", "rebase", "checkout", "clone", "fork", "rollback",
    "pipeline", "repo", "repository", "release", "staging",
    # Jerga de desarrollo que se dice en inglés
    "hook", "hooks", "bug", "bugs", "debug", "frontend", "backend", "framework",
    "endpoint", "daemon", "stream", "streaming", "prompt", "token", "runtime",
    "backend", "fullstack",
}
_TOKEN_RE = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+|\d+|[^A-Za-zÁÉÍÓÚÜÑáéíóúüñ\d]+")


def _phonemes_for(text):
    """Fonemas mezclando inglés (términos de EN_TERMS) y español (el resto),
    para que las palabras técnicas suenen en inglés dentro de la frase."""
    k = get_pipe()
    out, es_buf = [], []

    def flush_es():
        if es_buf:
            seg = "".join(es_buf)
            if seg.strip():
                p = k.tokenizer.phonemize(seg, "es")
                if p:
                    out.append(p)
            es_buf.clear()

    for tok in _TOKEN_RE.findall(text):
        if tok.isalpha() and tok.lower() in EN_TERMS:
            flush_es()
            p = k.tokenizer.phonemize(tok, "en-us")
            if p:
                out.append(p)
        else:
            es_buf.append(tok)
    flush_es()
    return " ".join(out)


# Pistas léxicas para auto-detección de idioma (palabras comunes muy frecuentes).
_ES_HINT = {"el", "la", "los", "las", "un", "una", "de", "que", "y", "en", "por",
            "con", "para", "se", "no", "es", "está", "más", "cómo", "qué", "pero",
            "este", "esta", "su", "al", "del", "lo", "le", "ya", "como", "hay", "fue"}
_EN_HINT = {"the", "is", "are", "and", "to", "of", "in", "that", "it", "for",
            "with", "you", "this", "be", "on", "not", "we", "your", "can", "will",
            "here", "i", "have", "do", "was", "but", "they", "from", "at"}
_WORD_RE = re.compile(r"[a-záéíóúñü]+", re.IGNORECASE)


def _detect_lang(text):
    """Heurística ligera es vs en-us: diacríticos/signos solo-español + conteo
    de palabras funcionales comunes. Empate → español (idioma primario)."""
    es = 2 if re.search(r"[áéíóúñ¿¡]", text, re.IGNORECASE) else 0
    en = 0
    for w in _WORD_RE.findall(text.lower()):
        if w in _ES_HINT:
            es += 1
        elif w in _EN_HINT:
            en += 1
    return "en-us" if en > es else "es"


def _synthesize(text, voice):
    """Sintetiza una oración. Si `auto_lang`, detecta el idioma: inglés se lee
    con voz US (`voice_en`); español con `voice` y, si `english_terms`, los
    términos técnicos en inglés vía fonemas mixtos. `speed` ajusta naturalidad."""
    k = get_pipe()
    cfg = load_config()
    speed = float(cfg.get("speed", 1.0))
    lang = _detect_lang(text) if cfg.get("auto_lang", True) else cfg.get("lang", LANG)

    if lang == "en-us":
        v = cfg.get("voice_en", DEFAULT_VOICE_EN)
        return k.create(text, voice=v, speed=speed, lang="en-us")

    # Español (voz `voice`), con términos técnicos en inglés mezclados.
    v = voice or cfg.get("voice", DEFAULT_VOICE)
    if cfg.get("english_terms", True):
        try:
            phon = _phonemes_for(text)
            if phon.strip():
                return k.create(phon, voice=v, speed=speed, is_phonemes=True)
        except Exception as e:
            _daemon_log(f"mixed phonemes fallback: {e}")
    return k.create(fix_pronunciation(text), voice=v, speed=speed, lang=LANG)


def split_sentences(text):
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    return [p.strip() for p in parts if p.strip()]


def extract_from_transcript(path):
    """Une todos los bloques de texto del asistente desde el último prompt REAL
    del usuario, ignorando mensajes role=user que son en realidad tool_result.
    Más robusto que leer un único `last_assistant_message`."""
    try:
        with open(path, encoding="utf-8") as f:
            events = [json.loads(ln) for ln in f if ln.strip()]
    except (OSError, json.JSONDecodeError):
        return ""

    last_user = -1
    for i, ev in enumerate(events):
        msg = ev.get("message", {})
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        is_tool_result = isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
        if not is_tool_result:
            last_user = i

    texts = []
    for ev in events[last_user + 1:]:
        msg = ev.get("message", {})
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                    texts.append(b["text"])
        elif isinstance(content, str):
            texts.append(content)
    return "  ".join(t for t in texts if t.strip())


# ── timing / render ──
def estimate_word_timings(words, duration):
    total_chars = sum(len(w) for w in words)
    if total_chars == 0:
        return [(0.0, duration)] * len(words)
    timings, cursor = [], 0.0
    for w in words:
        word_dur = (len(w) / total_chars) * duration
        timings.append((cursor, cursor + word_dur))
        cursor += word_dur
    return timings


def render_karaoke(all_words, idx, window):
    total = len(all_words)
    start = max(0, idx - window)
    end = min(total, idx + window + 1)
    parts = []
    if start > 0:
        parts.append(f"{DIM}...{RESET}")
    for i in range(start, end):
        if i < idx - 1:
            parts.append(f"{SPOKEN}{all_words[i]}{RESET}")
        elif i == idx - 1 or i == idx + 1:
            parts.append(f"{NEAR}{all_words[i]}{RESET}")
        elif i == idx:
            parts.append(f"{HIGHLIGHT}{UNDERLINE}{all_words[i]}{RESET}")
        else:
            parts.append(f"{DIM}{all_words[i]}{RESET}")
    if end < total:
        parts.append(f"{DIM}...{RESET}")
    return " ".join(parts)


def mini_bar(current, total, width=20):
    if total == 0:
        return ""
    filled = int((current / total) * width)
    return f"{BAR_FILL}{'━' * filled}{BAR_EMPTY}{'━' * (width - filled)}{RESET} {LABEL}{current}/{total}{RESET}"


# ── síntesis ──
def _play_blocking(full_audio):
    """Reproduce audio completo. Con sounddevice usa sd; si no, aplay."""
    global _aplay_proc
    if HAVE_SD:
        sd.play(full_audio, samplerate=SAMPLE_RATE)
        sd.wait()
        return
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    wav_path = os.path.join(RUNTIME_DIR, "out.wav")
    pcm = (np.clip(full_audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    _aplay_proc = subprocess.Popen(["aplay", "-q", wav_path],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _aplay_proc.wait()


def _synth_producer(sentences, voice, audio_q):
    """Hilo productor: sintetiza cada oración y la encola con su índice.
    El maxsize de la cola da backpressure (sintetiza ~adelante, no todo de golpe)."""
    for i, s in enumerate(sentences):
        if _interrupted:
            break
        try:
            samples, _sr = _synthesize(s, voice)
            audio_q.put((i, np.asarray(samples, dtype=np.float32)))
        except Exception:
            continue
    audio_q.put(None)


def speak_and_highlight(text, voice, show_stats=False, tty_path="/dev/tty"):
    cfg = load_config()
    window = cfg.get("window", WINDOW)
    done_pause = cfg.get("done_pause", DONE_PAUSE)
    chime = cfg.get("chime", CHIME_ENABLED)
    global _interrupted
    _interrupted = False
    t0 = time.monotonic()

    synth_sentences = split_sentences(text)  # crudo: _synthesize maneja pronunciación
    display_sentences = split_sentences(text)
    all_words = text.split()
    total_words = len(all_words)
    if not synth_sentences:
        return {}

    # Productor: sintetiza oración por oración en paralelo a la reproducción.
    audio_q = queue.Queue(maxsize=3)
    threading.Thread(target=_synth_producer,
                     args=(synth_sentences, voice, audio_q), daemon=True).start()

    tty = get_tty(tty_path)
    if HAVE_SD:
        if chime:
            play_chime_start()
        _start_keypress_listener(tty_path)
        tty.write(HIDE_CURSOR)
        header = f"  {LABEL}leyendo en voz alta{RESET}  {LABEL}|{RESET}  {LABEL}{voice}{RESET}  {DIM}(presiona una tecla para saltar){RESET}"
        tty.write(f"{header}\n\n")
        tty.flush()
    else:
        try:
            tty.write(f"  {LABEL}leyendo en voz alta{RESET}  {LABEL}|{RESET}  {LABEL}{voice}{RESET}\n")
            tty.flush()
        except (OSError, ValueError):
            pass

    # Consumidor: reproduce cada oración en cuanto está lista. Con SD hace karaoke
    # por oración (TTFA ≈ síntesis de la 1ª); sin SD reproduce por aplay.
    ttfa = None
    audio_total = 0.0
    word_idx = 0
    while not _interrupted:
        item = audio_q.get()
        if item is None:
            break
        idx, audio = item
        if ttfa is None:
            ttfa = time.monotonic() - t0
        audio_total += len(audio) / SAMPLE_RATE

        if not HAVE_SD:
            _play_blocking(audio)
            continue

        words = display_sentences[idx].split() if idx < len(display_sentences) else []
        timings = estimate_word_timings(words, len(audio) / SAMPLE_RATE)
        seg_start = time.monotonic()
        sd.play(audio, samplerate=SAMPLE_RATE)
        for wstart, _wend in timings:
            if _interrupted:
                break
            elapsed = time.monotonic() - seg_start
            if elapsed < wstart:
                time.sleep(wstart - elapsed)
            if _interrupted:
                break
            karaoke = render_karaoke(all_words, word_idx, window)
            bar = mini_bar(min(word_idx + 1, total_words), total_words)
            tty.write(f"\033[1A\r\033[K  {karaoke}\n\r\033[K  {bar}")
            tty.flush()
            word_idx += 1
        sd.wait()

    if HAVE_SD:
        sd.stop()
        _restore_terminal()
        if not _interrupted:
            bar = mini_bar(total_words, total_words)
            tty.write(f"\033[1A\r\033[K  {SPOKEN}listo{RESET}\n\r\033[K  {bar}")
            tty.flush()
            time.sleep(done_pause)
            if chime:
                play_chime_end()
        tty.write("\033[1A\r\033[K\033[1A\r\033[K\033[1A\r\033[K")
        tty.write(SHOW_CURSOR)
        tty.flush()

    total_time = time.monotonic() - t0
    stats = {"ttfa": ttfa or total_time, "gen_time": 0.0, "audio_duration": audio_total,
             "total_time": total_time, "words": total_words, "chars": len(text), "voice": voice}
    if not show_stats:
        sys.stderr.write(f"feengspeak: ttfa={stats['ttfa']:.2f}s "
                         f"total={total_time:.2f}s words={total_words} voice={voice}\n")
    if tty is not sys.stderr:
        tty.close()
    return stats


# ── daemon ──
def _daemon_log(msg):
    try:
        os.makedirs(RUNTIME_DIR, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except OSError:
        pass


def _resolve_tty():
    for fd in (2, 1, 0):
        try:
            return os.ttyname(fd)
        except OSError:
            continue
    fd = None
    try:
        fd = os.open("/dev/tty", os.O_RDONLY)
        return os.ttyname(fd)
    except OSError:
        return "/dev/tty"
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def _daemon_alive():
    if not os.path.exists(SOCK_PATH):
        return False
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(SOCK_PATH)
        s.sendall(json.dumps({"op": "ping"}).encode() + b"\n")
        resp = s.recv(256)
        s.close()
        return b'"ok"' in resp
    except (OSError, socket.timeout):
        return False


def _spawn_daemon():
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    log = open(LOG_PATH, "a")
    subprocess.Popen([sys.executable, SCRIPT_PATH, "--daemon"],
                     stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                     start_new_session=True, close_fds=True)


def _send_to_daemon(payload, timeout=2.0):
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(SOCK_PATH)
        s.sendall(json.dumps(payload).encode() + b"\n")
        buf = b""
        while b"\n" not in buf and len(buf) < 8192:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
        s.close()
        return json.loads(buf.split(b"\n", 1)[0].decode("utf-8", errors="replace"))
    except (OSError, socket.timeout, json.JSONDecodeError):
        return None


def _ensure_daemon(wait_seconds=DAEMON_SPAWN_WAIT):
    if _daemon_alive():
        return True
    _spawn_daemon()
    for _ in range(wait_seconds * 5):
        time.sleep(0.2)
        if _daemon_alive():
            return True
    return False


def _stream_synth_worker():
    """Etapa 1: sintetiza el texto entrante y deja el audio listo en _play_q.
    Corre ADELANTADO de la reproducción, así no hay hueco entre oraciones."""
    while True:
        item = _stream_q.get()
        if item is None:
            continue
        text, voice = item
        try:
            samples, _sr = _synthesize(text, voice)
            _play_q.put(np.asarray(samples, dtype=np.float32))
        except Exception as e:
            _daemon_log(f"stream synth error: {e}")


def _stream_play_worker():
    """Etapa 2: reproduce en orden el audio ya sintetizado, sin preempción."""
    while True:
        audio = _play_q.get()
        if audio is None:
            continue
        try:
            with _playback_lock:
                _play_blocking(audio)
        except Exception as e:
            _daemon_log(f"stream playback error: {e}")


def _handle_client(conn):
    global _daemon_idle_t0, _interrupted, _active_tty, _active_tty_t
    try:
        conn.settimeout(2.0)
        buf = b""
        while b"\n" not in buf and len(buf) < 2_000_000:
            chunk = conn.recv(8192)
            if not chunk:
                break
            buf += chunk
        line = buf.split(b"\n", 1)[0]
        if not line:
            return
        req = json.loads(line.decode("utf-8", errors="replace"))
        _daemon_idle_t0 = time.monotonic()
        op = req.get("op", "speak")

        if op == "ping":
            conn.sendall(json.dumps({"ok": True}).encode() + b"\n"); return
        if op == "shutdown":
            conn.sendall(json.dumps({"ok": True}).encode() + b"\n")
            _daemon_log("shutdown requested")
            os.kill(os.getpid(), signal.SIGTERM); return
        if op == "claim":
            _active_tty = req.get("tty_path") or _active_tty
            _active_tty_t = time.monotonic()
            conn.sendall(json.dumps({"ok": True, "active_tty": _active_tty}).encode() + b"\n"); return
        if op == "unclaim":
            _active_tty = None; _active_tty_t = 0.0
            conn.sendall(json.dumps({"ok": True}).encode() + b"\n"); return
        if op in ("mute", "unmute", "toggle"):
            tty_path = req.get("tty_path") or ""
            if not tty_path:
                conn.sendall(json.dumps({"error": "no tty_path"}).encode() + b"\n"); return
            if op == "mute":
                _muted_ttys.add(tty_path)
            elif op == "unmute":
                _muted_ttys.discard(tty_path)
            else:
                _muted_ttys.discard(tty_path) if tty_path in _muted_ttys else _muted_ttys.add(tty_path)
            muted = tty_path in _muted_ttys
            conn.sendall(json.dumps({"ok": True, "muted": muted}).encode() + b"\n"); return
        if op == "status":
            stale = _active_tty and (time.monotonic() - _active_tty_t > ACTIVE_TTY_STALE)
            conn.sendall(json.dumps({
                "ok": True, "pid": os.getpid(),
                "idle_s": time.monotonic() - _daemon_idle_t0,
                "active_tty": None if stale else _active_tty,
                "muted_ttys": sorted(_muted_ttys),
            }).encode() + b"\n"); return

        if op == "speak_stream":
            # Encola una oración para lectura en streaming (no preempta).
            stext = (req.get("text") or "").strip()
            svoice = req.get("voice") or load_config().get("voice", DEFAULT_VOICE)
            if stext:
                _stream_q.put((stext, svoice))
            conn.sendall(json.dumps({"queued": True}).encode() + b"\n"); return

        if op == "reset_stream":
            # Nuevo turno: vacía ambas colas y corta lo que esté sonando.
            for q in (_stream_q, _play_q):
                try:
                    while True:
                        q.get_nowait()
                except queue.Empty:
                    pass
            _stop_playback()
            conn.sendall(json.dumps({"ok": True}).encode() + b"\n"); return

        # op == "speak"
        text = req.get("text", "")
        voice = req.get("voice") or load_config().get("voice", DEFAULT_VOICE)
        tty_path = req.get("tty_path") or "/dev/tty"
        override = bool(req.get("override"))
        if not text.strip():
            conn.sendall(json.dumps({"error": "empty"}).encode() + b"\n"); return
        if not override:
            if tty_path in _muted_ttys:
                conn.sendall(json.dumps({"skipped": "muted"}).encode() + b"\n"); return
            stale = _active_tty and (time.monotonic() - _active_tty_t > ACTIVE_TTY_STALE)
            if _active_tty and not stale and tty_path != _active_tty:
                conn.sendall(json.dumps({"skipped": "not_active_tty"}).encode() + b"\n"); return

        conn.sendall(json.dumps({"queued": True}).encode() + b"\n")
        try:
            conn.close()
        except OSError:
            pass

        _interrupted = True
        _stop_playback()
        with _playback_lock:
            _interrupted = False
            try:
                speak_and_highlight(text, voice, show_stats=False, tty_path=tty_path)
            except Exception as e:
                _daemon_log(f"playback error: {e}")
    except (json.JSONDecodeError, OSError, ValueError) as e:
        try:
            conn.sendall(json.dumps({"error": str(e)}).encode() + b"\n")
        except OSError:
            pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def cmd_daemon():
    global _daemon_idle_t0
    os.makedirs(RUNTIME_DIR, exist_ok=True)
    if _daemon_alive():
        _daemon_log("daemon already running, exiting"); return
    try:
        os.unlink(SOCK_PATH)
    except FileNotFoundError:
        pass
    try:
        with open(PID_PATH, "w") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass

    _daemon_log("loading kokoro (onnx) model...")
    t0 = time.monotonic()
    get_pipe()
    _daemon_log(f"kokoro loaded in {time.monotonic()-t0:.2f}s")

    # Pipeline de streaming: síntesis adelantada + reproducción en orden.
    threading.Thread(target=_stream_synth_worker, daemon=True).start()
    threading.Thread(target=_stream_play_worker, daemon=True).start()

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o600)
    srv.listen(8)
    srv.settimeout(5.0)
    _daemon_log(f"listening on {SOCK_PATH}")
    _daemon_idle_t0 = time.monotonic()

    def _cleanup(*_a):
        for path in (SOCK_PATH, PID_PATH):
            try:
                os.unlink(path)
            except OSError:
                pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    while True:
        if time.monotonic() - _daemon_idle_t0 > DAEMON_IDLE_TIMEOUT:
            _daemon_log("idle timeout, exiting"); _cleanup()
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        except OSError as e:
            _daemon_log(f"accept error: {e}"); continue
        threading.Thread(target=_handle_client, args=(conn,), daemon=True).start()


# ── comandos ──
def cmd_setup():
    save_default_config()
    settings = {}
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH) as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    hooks = settings.get("hooks", {})

    def _has_us(event):
        for entry in hooks.get(event, []):
            for h in entry.get("hooks", []):
                if "feengspeak" in str(h).lower():
                    return True
        return False

    cmd = f"{VENV_PYTHON} {SCRIPT_PATH}"
    added = []
    if not _has_us("Stop"):
        hooks.setdefault("Stop", []).append({
            "matcher": "",
            "hooks": [{"type": "command", "command": cmd, "timeout": 60}],
        })
        added.append("Stop")
    if not _has_us("UserPromptSubmit"):
        hooks.setdefault("UserPromptSubmit", []).append({
            "matcher": "",
            "hooks": [{"type": "command", "command": f"{cmd} claim", "timeout": 5}],
        })
        added.append("UserPromptSubmit (claim)")

    # MessageDisplay → hook ligero de streaming. Siempre apunta al stream_hook
    # (reemplaza cualquier sonda previa). Se activa/desactiva con `feengspeak stream`.
    stream_cmd = f"{VENV_PYTHON} {os.path.join(BASE_DIR, 'stream_hook.py')}"
    desired_md = [{"hooks": [{"type": "command", "command": stream_cmd}]}]
    if hooks.get("MessageDisplay") != desired_md:
        hooks["MessageDisplay"] = desired_md
        added.append("MessageDisplay (streaming)")

    if not added:
        print(f"{GREEN}FeengSpeak ya está instalado.{RESET}")
        print(f"Config: {CONFIG_PATH}")
        return

    settings["hooks"] = hooks
    os.makedirs(os.path.dirname(SETTINGS_PATH), exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(settings, f, indent=2)
    print(f"{GREEN}FeengSpeak instalado.{RESET}")
    print(f"  Hooks:    {', '.join(added)}")
    print(f"  Settings: {SETTINGS_PATH}")
    print(f"  Config:   {CONFIG_PATH}")
    print(f"  Voz:      {load_config().get('voice', DEFAULT_VOICE)}")
    print(f"\nReinicia Claude Code para que los hooks tomen efecto.")


def cmd_demo():
    cfg = load_config()
    voice = cfg.get("voice", DEFAULT_VOICE)
    mode = "kokoro onnx | local" + ("" if HAVE_SD else " | sin PortAudio (aplay)")
    print(f"\n  {BOLD}FeengSpeak demo{RESET}")
    print(f"  {LABEL}voz: {voice}  |  {mode}{RESET}\n")
    time.sleep(0.3)
    speak_and_highlight(DEMO_TEXT, voice, show_stats=True)
    print(f"\n  {GREEN}Demo completa.{RESET}")
    print(f"  {LABEL}Todo local — sin claves de API, sin nube.{RESET}\n")


def cmd_toggle(enable):
    cfg = load_config()
    cfg["enabled"] = enable
    save_config(cfg)
    state = f"{GREEN}activada{RESET}" if enable else f"{RED}desactivada{RESET}"
    print(f"  FeengSpeak: voz {state}")


def cmd_stream(enable):
    cfg = load_config()
    cfg["stream_mode"] = enable
    save_config(cfg)
    if enable:
        print(f"  FeengSpeak: lectura en {GREEN}streaming{RESET} — lee mientras Claude escribe.")
        print(f"  {DIM}Requiere el hook MessageDisplay (corre `feengspeak setup`) y reiniciar Claude Code.{RESET}")
    else:
        print(f"  FeengSpeak: lectura {GREEN}al terminar{RESET} — modo normal (hook Stop).")


def cmd_daemon_status():
    if not _daemon_alive():
        print(f"  {RED}daemon detenido{RESET}"); return
    resp = _send_to_daemon({"op": "status"}, timeout=1.0) or {}
    pid = resp.get("pid", "?")
    idle = resp.get("idle_s")
    idle_str = f"{idle:.0f}s" if isinstance(idle, (int, float)) else "?"
    active = resp.get("active_tty") or f"{DIM}(ninguna){RESET}"
    print(f"  {GREEN}daemon activo{RESET}  pid={pid}  idle={idle_str}  tty_activa={active}")


def cmd_claim():
    if not _daemon_alive():
        # Pre-calienta el daemon al enviar un prompt, para que esté listo cuando
        # llegue el streaming de la respuesta. No bloquea (fire-and-forget).
        if load_config().get("enabled", True):
            _spawn_daemon()
        return
    _send_to_daemon({"op": "claim", "tty_path": _resolve_tty()}, timeout=1.0)


def cmd_unclaim():
    if not _daemon_alive():
        return
    _send_to_daemon({"op": "unclaim"}, timeout=1.0)


def _cmd_mute_op(op):
    tty_path = _resolve_tty()
    if not _ensure_daemon():
        print(f"  {RED}no se pudo contactar el daemon{RESET}"); return
    resp = _send_to_daemon({"op": op, "tty_path": tty_path}, timeout=1.5) or {}
    muted = resp.get("muted")
    if muted is True:
        print(f"  {RED}voz OFF{RESET} en esta terminal  ({DIM}{tty_path}{RESET})")
    elif muted is False:
        print(f"  {GREEN}voz ON{RESET} en esta terminal  ({DIM}{tty_path}{RESET})")
    else:
        print(f"  {RED}error{RESET}: {resp}")


def cmd_mute(): _cmd_mute_op("mute")
def cmd_unmute(): _cmd_mute_op("unmute")
def cmd_toggle_voice(): _cmd_mute_op("toggle")


def cmd_daemon_stop():
    if not _daemon_alive():
        print(f"  {DIM}daemon detenido{RESET}"); return
    _send_to_daemon({"op": "shutdown"}, timeout=1.0)
    print(f"  daemon detenido")


def main():
    # `stream on|off` toma argumento, se maneja aparte.
    if len(sys.argv) >= 2 and sys.argv[1] == "stream":
        cmd_stream(len(sys.argv) >= 3 and sys.argv[2] == "on")
        sys.exit(0)

    SUBCOMMANDS = {
        "setup", "demo", "on", "off", "claim", "unclaim",
        "mute", "unmute", "toggle", "daemon-status", "daemon-stop",
    }
    if len(sys.argv) >= 2 and sys.argv[1] in SUBCOMMANDS:
        cmd = sys.argv[1]
        {
            "setup": cmd_setup, "demo": cmd_demo,
            "on": lambda: cmd_toggle(True), "off": lambda: cmd_toggle(False),
            "claim": cmd_claim, "unclaim": cmd_unclaim,
            "mute": cmd_mute, "unmute": cmd_unmute, "toggle": cmd_toggle_voice,
            "daemon-status": cmd_daemon_status, "daemon-stop": cmd_daemon_stop,
        }[cmd]()
        sys.exit(0)

    parser = argparse.ArgumentParser(
        description="FeengSpeak — voz local para Claude Code (español)",
        usage="feengspeak [setup|demo|on|off|daemon-status|daemon-stop] | feengspeak [--voice V] [texto]",
    )
    parser.add_argument("text", nargs="*", help="Texto a leer")
    parser.add_argument("--voice", "-v", default=None, help="ID de voz Kokoro")
    parser.add_argument("--voices", action="store_true", help="Lista las voces")
    parser.add_argument("--long", action="store_true", help="Sin truncado")
    parser.add_argument("--daemon", action="store_true", help="Corre como daemon (interno)")
    parser.add_argument("--no-daemon", action="store_true", help="Fuerza in-process")
    args = parser.parse_args()

    if args.daemon:
        cmd_daemon(); sys.exit(0)

    if args.voices:
        cfg = load_config()
        current = cfg.get("voice", DEFAULT_VOICE)
        print(f"\n  {BOLD}Voces disponibles{RESET}\n")
        for vid, desc in VOICE_LIST.items():
            marker = f" {GREEN}*{RESET}" if vid == current else ""
            print(f"  {CYAN}{vid:16s}{RESET} {desc}{marker}")
        print(f"\n  {DIM}Voz por defecto: edita {CONFIG_PATH}{RESET}\n")
        sys.exit(0)

    cfg = load_config()
    if not cfg.get("enabled", True):
        sys.exit(0)
    voice = args.voice or cfg.get("voice", DEFAULT_VOICE)

    text = None
    if args.text:
        text = " ".join(args.text)
    elif not sys.stdin.isatty():
        # En modo streaming, MessageDisplay ya leyó en vivo: el Stop hook no relee.
        if cfg.get("stream_mode"):
            sys.exit(0)
        raw = sys.stdin.read().strip()
        msg_text = ""
        try:
            data = json.loads(raw)
            tp = data.get("transcript_path")
            if tp and os.path.exists(tp):
                time.sleep(TRANSCRIPT_FLUSH_WAIT)  # deja que el transcript se escriba
                msg_text = extract_from_transcript(tp)
            else:
                msg_text = data.get("last_assistant_message", "") or raw
        except (json.JSONDecodeError, TypeError):
            msg_text = raw
        text = msg_text

    if not text or not text.strip() or is_stub(text):
        sys.exit(0)
    text = clean_for_speech(text)
    if not text or is_stub(text):
        sys.exit(0)

    min_chars = cfg.get("min_chars", MIN_CHARS)
    max_chars = cfg.get("max_chars", MAX_CHARS)
    if len(text) < min_chars:
        sys.exit(0)
    if not args.long and len(text) > max_chars:
        text = text[:max_chars].rsplit(" ", 1)[0] + "..."

    tty_path = _resolve_tty()
    use_daemon = cfg.get("use_daemon", True) and not args.no_daemon
    if use_daemon and _ensure_daemon():
        resp = _send_to_daemon({"op": "speak", "text": text, "voice": voice,
                                "tty_path": tty_path}, timeout=5.0)
        if resp is not None and "queued" in resp:
            sys.exit(0)

    try:
        speak_and_highlight(text, voice, tty_path=tty_path)
    except Exception:
        if _tty:
            try:
                _tty.write(SHOW_CURSOR); _tty.flush()
            except (OSError, ValueError):
                pass
        sys.exit(0)


if __name__ == "__main__":
    main()
