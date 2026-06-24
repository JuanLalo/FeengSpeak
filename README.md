# FeengSpeak 🎙️

**Give Claude Code a voice.** FeengSpeak reads Claude Code's responses out loud, in
real time, as they stream — fully local, no API keys, nothing leaves your machine.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Local](https://img.shields.io/badge/runs-100%25%20local-success)
![Engine: Kokoro ONNX](https://img.shields.io/badge/engine-Kokoro%20ONNX-orange)

> Text-to-speech (TTS) voice mode for [Claude Code](https://claude.com/claude-code).
> Hear the agent talk back while you code — hands-free, bilingual, offline.

---

## Why

Claude Code talks to you in text. FeengSpeak adds the missing half: it **speaks the
responses aloud** so you can keep your eyes on what matters, multitask, or just rest
them. It hooks into Claude Code and starts reading **while** the response is still
being generated — you don't wait for it to finish.

Built for developers: it reads the things you actually need — inline code, file paths,
commands — and only skips long code blocks.

## Features

- 🌎 **Bilingual, automatic** — detects the language of each response and reads
  Spanish with a Spanish voice and English with a US voice. No toggles. The language
  is locked per response, so the voice never flips mid-message.
- ⚡ **Live streaming** — reads sentence by sentence *as Claude types*, via the
  `MessageDisplay` hook, not after the response completes.
- 🔤 **English technical terms in English** — words like `commit`, `deploy`,
  `daemon`, `branch` are pronounced in English even inside Spanish prose (mixed-language
  phonemization). Editable term list.
- 👨‍💻 **Dev-friendly** — reads inline code, paths and commands in full; only long
  fenced code blocks are summarized as "code block".
- 🚀 **Fast & local** — [Kokoro](https://github.com/thewh1teagle/kokoro-onnx) via
  ONNX Runtime (no PyTorch, runs on CPU). A background daemon keeps the model warm;
  time-to-first-audio is ~0.7s thanks to a two-stage streaming pipeline.
- 🔒 **Private** — 100% offline after the one-time model download. No cloud, no API
  keys, no telemetry.

## How it works

1. Claude Code streams a response. The `MessageDisplay` hook feeds the text to a
   lightweight handler (`stream_hook.py`) in deltas.
2. The handler accumulates text, detects complete sentences, strips markdown (keeping
   inline code/paths/commands), and groups them into chunks.
3. Chunks go over a Unix socket to a daemon that synthesizes and plays them **in
   order, without gaps** — the next sentence is synthesized while the current one plays.
4. A new prompt interrupts the previous reading. A `Stop` hook is the fallback when
   live streaming is off.

## Requirements

- **Python 3.11+**
- Linux with ALSA (`aplay`) for playback
- *(optional)* `libportaudio2` for `sounddevice` playback + karaoke word-highlighting:
  `sudo apt install -y libportaudio2`

The TTS engine and its phonemizer (espeak-ng) are installed via pip — no other system
packages required.

## Install

```bash
git clone https://github.com/JuanLalo/FeengSpeak
cd FeengSpeak
./install.sh                          # venv + deps + Kokoro models (~340 MB, once)
venv/bin/python feengspeak.py setup   # installs the Claude Code hooks
feengspeak stream on                  # enable live (streaming) reading
# Restart Claude Code for the hooks to take effect.
```

`install.sh` is idempotent. Models and the venv are gitignored — a fresh clone
recreates them.

## Usage

```bash
feengspeak demo            # play a voice demo
feengspeak on | off        # enable / disable reading
feengspeak stream on | off # live reading while Claude types, vs reading on completion
feengspeak --voices        # list voices
feengspeak daemon-status   # daemon state
feengspeak daemon-stop     # stop the daemon (it revives on next read, reloading config)
feengspeak --voice am_onyx "any text to read"
```

## Configuration

`~/.config/feengspeak/config.json` (per-user, outside the repo):

| Key | Default | Description |
|-----|---------|-------------|
| `voice` | `em_alex` | Spanish voice (`ef_dora`, `em_alex`, `em_santa`). |
| `voice_en` | `am_michael` | US English voice (`am_michael`, `am_adam`, `am_onyx`, `af_heart`, `af_nova`). |
| `auto_lang` | `true` | Auto-detect Spanish vs English per response. |
| `english_terms` | `true` | Pronounce `EN_TERMS` words in English inside Spanish. |
| `speed` | `0.93` | Speech rate (lower = slower / calmer). |
| `stream_mode` | `false` | Live reading while typing (`feengspeak stream on`). |
| `enabled` | `true` | Global on/off (`feengspeak on/off`). |

After changing config or code: `feengspeak daemon-stop` (the daemon revives and reloads).

## Project layout

```
feengspeak.py     # CLI + daemon + synthesis + language detection
stream_hook.py    # MessageDisplay hook: live, stdlib-only, non-blocking
install.sh        # reproducible setup
requirements.txt
```

## Credits & License

FeengSpeak is a fork of [`claude-voice`](https://github.com/Null-Phnix/claude-voice)
by Null-Phnix (MIT), re-engineered for a Spanish-first, bilingual workflow: ONNX
backend (no PyTorch), Spanish + English voices, mixed-language pronunciation,
transcript-based extraction, and per-message language locking. See [`NOTICE`](./NOTICE).

Voices and engine: [Kokoro](https://github.com/hexgrad/kokoro) /
[kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx).

Licensed under the [MIT License](./LICENSE).
