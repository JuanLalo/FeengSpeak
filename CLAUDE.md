# FeengSpeak Development Standards

Reglas de desarrollo de FeengSpeak. Overview y uso en [README.md](./README.md).

## 1. Arquitectura

### 1.1 Clasificación de directorios

| Clasificación | Rutas | Política |
|---------------|-------|----------|
| Código | `feengspeak.py` | Único archivo fuente: CLI, daemon, hooks, síntesis, render. |
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
- Idioma de salida: español. Mensajes de CLI en español neutro.
- Los hooks se instalan/identifican por el substring `feengspeak` en el comando.

## 3. Licencia

Fork interno de `claude-voice` (MIT, Null-Phnix). Conservar `LICENSE` y `NOTICE`
intactos. Documentar cambios sustanciales del upstream en `NOTICE`.
