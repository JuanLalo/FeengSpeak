# FeengSpeak Development Standards

Reglas de desarrollo de FeengSpeak. Overview y uso en [README.md](./README.md).

## 1. Arquitectura

### 1.1 Clasificación de directorios

| Clasificación | Rutas | Política |
|---------------|-------|----------|
| Código | `feengspeak.py`, `stream_hook.py` | CLI + daemon + síntesis; `stream_hook.py` es el hook ligero (stdlib) de streaming. |
| Build/Setup | `install.sh`, `requirements.txt` | Instalación reproducible. Mantener idempotente. |
| Generado | `venv/`, `models/`, `__pycache__/` | Nunca commitear. Los regenera `install.sh`. |
| Legal | `LICENSE`, `NOTICE` | MIT del upstream + atribución. **No remover** (lo exige MIT). |

### 1.2 Principios

- **Un solo archivo.** Toda la lógica vive en `feengspeak.py`. No fragmentar sin razón fuerte.
- **Relocatable.** `BASE_DIR` se deriva de la ubicación del script; `venv/` y `models/`
  son relativos. Mover el repo no debe romper rutas.
- **Local-first.** Nada de red en runtime. Sin claves de API. Sin telemetría.
- **Degradación elegante.** Si falta una dependencia opcional (p.ej. PortAudio),
  el tool sigue funcionando con menos features, nunca crashea.

## 2. Convenciones

- Config de usuario en `~/.config/feengspeak/`; runtime/daemon en `~/.cache/feengspeak/`.
  Nunca escribir estado dentro del repo.
- Idioma de salida: bilingüe ES/EN con auto-detección por mensaje. Mensajes de CLI en español neutro.
- Los hooks se instalan/identifican por el substring `feengspeak` en el comando.

## 3. Commits (doctrina Feengster)

- **Conventional Commits**: `type(scope): description`. Tipos: `feat`, `fix`,
  `perf`, `refactor`, `docs`, `chore`, `build`, `ci`, `test`.
- **Scopes por componente**: `repo`, `daemon`, `stream`, `tts`, `docs`, `build`.
  El nombre de la feature va en la descripción, no en el scope.
- **Atómicos**: un commit = un propósito, un cambio completo.
- **Sin atribución de IA**: nunca incluir `Co-Authored-By: Claude`,
  "Generated with Claude Code" ni similares. Los commits deben verse
  human-authored; la responsabilidad es del desarrollador.

## 4. Licencia

Fork interno de `claude-voice` (MIT, Null-Phnix). Conservar `LICENSE` y `NOTICE`
intactos. Documentar cambios sustanciales del upstream en `NOTICE`.
