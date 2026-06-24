# FeengSpeak

Herramienta interna de Feengster que le da **voz a Claude Code**: lee en voz alta
las respuestas del asistente, en español, **100% local** — sin claves de API, sin
nube, sin enviar tu código a ningún servicio.

## Overview

FeengSpeak es una utilidad de **developer experience (DX)**: una herramienta de
línea de comandos + daemon que se engancha a Claude Code mediante un hook `Stop`.
Cada vez que Claude termina de responder, FeengSpeak sintetiza la respuesta y la
reproduce. No es un producto de cara al cliente; es tooling interno del equipo.

- **Motor:** [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx) (Kokoro 82M
  vía ONNX Runtime — sin PyTorch, corre en CPU).
- **Bilingüe (auto):** detecta el idioma de cada bloque y lo lee con la voz correcta
  — español con `em_alex`, inglés US con `am_michael`. Los términos técnicos en
  inglés dentro del español se pronuncian en inglés.
- **Lectura en vivo:** lee mientras Claude escribe (hook `MessageDisplay`), oración
  por oración, no al terminar.
- **Para dev:** lee todo lo que el agente comunica (código inline, rutas, comandos);
  solo omite bloques de código largos.
- **Latencia:** el daemon carga el modelo una vez (~2s); luego TTFA ~0.7s con un
  pipeline de dos etapas (sintetiza la oración siguiente mientras suena la actual).
- **Privacidad:** todo local. Nada sale de la máquina.

## Configuración

`~/.config/feengspeak/config.json` (estado de usuario, fuera del repo):

| Clave | Default | Qué hace |
|-------|---------|----------|
| `voice` | `em_alex` | Voz española (`ef_dora`, `em_alex`, `em_santa`). |
| `voice_en` | `am_michael` | Voz inglesa US (`am_michael`, `am_adam`, `am_onyx`, `af_heart`, `af_nova`). |
| `auto_lang` | `true` | Auto-detecta es/en por bloque. |
| `english_terms` | `true` | Pronuncia términos de `EN_TERMS` en inglés dentro del español. |
| `speed` | `0.93` | Velocidad de voz (más bajo = más pausado). |
| `stream_mode` | — | Lectura en vivo (`feengspeak stream on|off`). |
| `enabled` | `true` | On/off global (`feengspeak on|off`). |

Tras cambiar la config o el código: `feengspeak daemon-stop` (el daemon revive solo
y relee la config).

Fork interno de [`claude-voice`](https://github.com/Null-Phnix/claude-voice)
(MIT, © 2026 Null-Phnix). Ver [`NOTICE`](./NOTICE) y [`LICENSE`](./LICENSE).

## Estructura

```
FeengSpeak/
├── feengspeak.py        # Todo el tool: CLI, daemon, hooks, síntesis, render
├── install.sh           # Crea venv, instala deps, descarga modelos (idempotente)
├── requirements.txt     # Dependencias Python
├── models/              # Modelos Kokoro (gitignored, los baja install.sh)
├── venv/                # Entorno virtual (gitignored)
├── LICENSE  · NOTICE    # MIT + atribución del upstream
└── CLAUDE.md            # Estándares de desarrollo
```

Config en `~/.config/feengspeak/config.json` · runtime/daemon en `~/.cache/feengspeak/`.

## Instalación

```bash
./install.sh
venv/bin/python feengspeak.py setup     # instala los hooks Stop + UserPromptSubmit
# Reinicia Claude Code para que los hooks tomen efecto.
```

Opcional, para el resaltado karaoke palabra-por-palabra en la terminal:

```bash
sudo apt install -y libportaudio2
```

Sin `libportaudio2` funciona igual (reproduce vía `aplay`), solo sin el efecto visual.

## Uso

```bash
feengspeak demo            # demo de voz
feengspeak on | off        # activa / desactiva la lectura
feengspeak --voices        # lista las voces
feengspeak daemon-status   # estado del daemon
feengspeak daemon-stop     # detiene el daemon
feengspeak --voice em_alex "texto a leer"
```

## Modo streaming (experimental)

Por defecto FeengSpeak lee la respuesta **al terminar** (hook `Stop`). El modo
streaming la lee **mientras Claude escribe**, oración por oración, usando el hook
`MessageDisplay` (que entrega el texto en deltas durante el render).

```bash
feengspeak stream on     # lee en vivo mientras se genera la respuesta
feengspeak stream off    # vuelve a leer al terminar (modo normal)
```

Requiere haber corrido `feengspeak setup` (registra el hook `MessageDisplay`) y
reiniciar Claude Code. Con `stream on`, el hook `Stop` se vuelve no-op para no
leer dos veces; el daemon encola las oraciones y las reproduce en orden sin
interrumpirse entre sí. Un prompt nuevo corta la lectura anterior.

## Cómo funciona

1. Claude Code dispara el hook `Stop` al terminar una respuesta.
2. FeengSpeak lee el `transcript_path` del payload, espera a que se escriba, y
   une **todos los bloques de texto** del asistente desde el último prompt real
   del usuario (ignora los `tool_result`).
3. Salta respuestas mayormente-código, limpia markdown, aplica el diccionario de
   pronunciación, y manda el texto al daemon por socket Unix.
4. El daemon sintetiza con Kokoro y reproduce por streaming; una respuesta nueva
   interrumpe la anterior.

## Requisitos

- Python 3.11+
- `libespeak-ng1` (fonemización; suele venir con speech-dispatcher en Ubuntu)
- `aplay` (ALSA) para reproducción; `libportaudio2` opcional para karaoke
