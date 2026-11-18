# Validación de ARG Studio

Fecha: 2026-11-17. Entorno Linux, Python 3.11.18, FFmpeg 7.1.5.
Base: `92e0f9a55b91e353565015aab74b7c50cc28f148`.
Fork: `Jsstrns25/MoneyPrinterTurbo`. Rama local: `feature/arg-studio`.
Permiso de escritura local confirmado. No se realizó push ni se comprobó permiso de escritura remoto.

## Comandos y resultados

```sh
.venv/bin/python -m pytest -q test/services/test_argstudio.py test/test_main.py test/services/test_config.py test/services/test_webui_headless_task_actions.py test/services/test_upload_post.py test/services/test_packaging.py
```

**90 passed**, una advertencia de deprecación de Starlette/TestClient ya presente en el proyecto; 18.83 segundos. Incluye contratos narrativos, continuidad, presupuestos, concurrencia, calendario, recuperación, renderizado y decodificación FFmpeg, cachés, material local, mezcla de audio, panel Streamlit, OAuth simulado y cargas/programación YouTube simuladas. No equivale a la suite completa de CI.

`ruff check` de todos los archivos Python nuevos: correcto. `compileall`: correcto. `git diff --check`: correcto.

API original iniciada en loopback: `/ping` respondió `"pong"`. Streamlit: `/_stcore/health` respondió `ok`. La nueva página se ejercitó con Streamlit AppTest, incluida la creación de una serie.

Demostración real con FFmpeg:

| Archivo | Resolución | Duración |
| --- | --- | --- |
| Episodio | 640×360 | 6.27 s |
| Short 1 | 360×640 | 2.10 s |
| Short 2 | 360×640 | 2.10 s |

Los tres videos tienen subtítulos incrustados y pista de audio silenciosa. Se generaron miniaturas. Segunda ejecución: mismas huellas SHA-256, sin regeneración. Materiales de prueba generados localmente; sin llamadas externas.

El parche solo agrega archivos; se verificó su reversibilidad mediante `git apply --reverse --check`. No se modificaron los archivos originales, dependencias, lockfile ni credenciales. Los archivos de datos y resultados permanecen ignorados por Git.

## No validado en este entorno

- Narración real de Edge, respuestas reales del proveedor LLM o descargas de Pexels/Pixabay.
- Episodio narrado completo de 8–12 minutos, calidad editorial o costos facturados.
- Autorización Google real, cuotas del proyecto, cargas al canal, miniaturas o publicación programada real.
- Instalador y supervisor sobre Windows.

No se hicieron cargas, publicaciones, operaciones pagadas, push ni publicación del entorno.
