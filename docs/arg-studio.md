# ARG Studio — primera versión funcional

ARG Studio agrega producción de series ARG a MoneyPrinterTurbo 1.3.10 sin reemplazar sus pantallas, configuración ni flujos actuales. No carga ni publica videos desde el trabajador de producción. Es una herramienta local: mantén Streamlit en `127.0.0.1`.

## Arquitectura y decisiones

- `app/argstudio/models.py`: contratos validados de configuración, canon, escenas, episodios e ideas.
- `store.py`: SQLite con transacciones, calendario, episodios, trabajos, reservas estimadas y aprobaciones. Clave única `(serie, identificador editorial)` para evitar duplicados.
- `story.py`: reutiliza el adaptador de modelos de `app/services/llm.py`, con contexto narrativo y validación del resultado. El canon se confirma al guardar el guion, antes del renderizado. Una historia anterior fallida bloquea las siguientes; reinténtala primero. Las ideas se guardan por separado y no cambian el canon.
- `media.py`: reutiliza Edge TTS, sus marcas de tiempo y el descargador Pexels/Pixabay. FFmpeg renderiza escenas secuencialmente, concatena, mezcla audio local autorizado y produce miniaturas. Esta ruta usa FFmpeg directamente para controlar resolución y consumo; el motor original permanece intacto.
- `worker.py`: un trabajador protegido por bloqueo de archivo, trabajos recuperables y calendario semanal materializado con zona horaria. No necesita Redis ni un proceso por escena.
- `youtube.py`: autorización oficial OAuth de escritorio con PKCE y callback de loopback; sesiones de carga reanudables, inicialmente privadas. Aprobación vinculada al SHA-256 del video.
- `webui/pages/ARG_Studio.py`: página adicional de Streamlit para crear series, ver canon/calendario, procesar tareas, revisar videos, aprobar/revocar y consultar reservas/errores.
- `tools/`: supervisor sin consola y registro opcional en el Programador de tareas de Windows.

Se revisó Upload-Post: tiene seguimiento de cargas y opciones de YouTube, pero su privacidad predeterminada es pública y emplea un servicio adicional. Por ello se conserva sin cambios y el nuevo flujo usa YouTube Data API directamente, sin activar cross-posting.

## Aplicar en tu computadora Windows

No se accedió a tu computadora ni se hizo push a GitHub. Los cambios están en la rama local `feature/arg-studio`; el paquete de entrega contiene únicamente archivos nuevos y un parche. Conserva primero una copia de tu instalación, `config.toml` y `storage` **fuera de Git**. No copies credenciales a este chat.

Desde tu fork local, con Git, Python 3.11, uv y FFmpeg/ffprobe disponibles:

```powershell
cd C:\ruta\MoneyPrinterTurbo
git status --short
git switch -c feature/arg-studio
# Usa el archivo arg-studio.patch entregado, sin sobreescribir cambios propios:
git apply --check C:\Descargas\arg-studio.patch
git apply C:\Descargas\arg-studio.patch
uv sync --frozen --python 3.11
.\.venv\Scripts\python.exe -m app.argstudio doctor
.\.venv\Scripts\python.exe -m pytest -q test/services/test_argstudio.py
```

Si no tienes FFmpeg, instala una distribución confiable con FFmpeg y ffprobe (por ejemplo el paquete Gyan.FFmpeg de winget) y añade su directorio `bin` al PATH de Windows. Abre una nueva sesión antes de comprobar `ffmpeg -version` y `ffprobe -version`. La interfaz original depende también de estos ejecutables para varios flujos.

No cambian `pyproject.toml`, `uv.lock` ni `config.toml`. El desarrollo usa Python 3.11 y las dependencias fijadas existentes. El parche agrega archivos; `git apply --check` debe pasar antes de aplicarlo. Si tu revisión es distinta, revisa cualquier conflicto sin borrar tus archivos.

## Prueba local sin servicios externos

```powershell
.\.venv\Scripts\python.exe -m app.argstudio demo
```

Produce un episodio 16:9 y dos Shorts 9:16, subtítulos incrustados, miniaturas y metadatos en `storage/argstudio/demo/`. Repetirlo reutiliza el mismo episodio. Es una **prueba técnica de unos segundos, con fondo generado y audio silencioso**: no demuestra calidad de narración ni producción de 8–12 minutos. El sistema impide aprobar estas demostraciones para YouTube. No hay solicitudes de IA, descargas de stock, gastos ni cargas a plataformas.

## Producción narrada real

1. Configura el proveedor de lenguaje y sus credenciales desde la interfaz original. También configura Pexels/Pixabay si usarás stock. No se copian claves a SQLite ni a los ejemplos de configuración.
2. Abre la página **ARG Studio**, crea una serie en modo `live`, define premisa/voz/duración y confirma explícitamente el permiso para llamar al modelo. Elige una tarifa estimada conservadora y un presupuesto.
3. Añade un episodio o programa una semana. Pulsa **Procesar tareas pendientes**; después actualiza el panel. El calendario solo funciona mientras el trabajador esté activo; recupera trabajos atrasados al volver a ejecutarlo.
4. Revisa los videos, los subtítulos, la duración efectiva, el canon y las licencias. Corrige el perfil creando otra serie si necesitas otro proveedor; los episodios ya encolados conservan su configuración.

Alternativa CLI: copia `docs/arg-studio.settings.example.json` a una ruta local, ajústalo y conserva allí solo preferencias, nunca claves. Para habilitar llamadas al modelo cambia conscientemente `allow_llm_requests` a `true` y asigna presupuesto.

```powershell
$Python = '.\.venv\Scripts\python.exe'
& $Python -m app.argstudio create --name 'Archivo Umbral' --premise 'Una archivista recibe señales del futuro en una ciudad ficticia' --settings C:\ruta\arg-settings.json
# Copia el identificador de serie devuelto:
& $Python -m app.argstudio ideas ID_SERIE
& $Python -m app.argstudio enqueue ID_SERIE --slot episodio-001
& $Python -m app.argstudio worker --once
& $Python -m app.argstudio status
& $Python -m app.argstudio plan-week ID_SERIE --start 2026-11-23 --weekdays 0,3 --hour 18 --timezone America/Panama
# Modo continuo para procesar el calendario:
& $Python -m app.argstudio worker
```

El modo `worker --once` procesa los trabajos vencidos y termina; devuelve estado no cero si hay trabajos fallidos. El calendario crea fechas concretas para siete días, **no una recurrencia perpetua**. Largo y Shorts son trabajos independientes, con vínculo al mismo episodio; los Shorts son adaptaciones narradas autónomas de escenas seleccionadas, no simples recortes temporales del MP4 largo.

Objetivo de guion: 135 palabras/minuto, con validación de rango. Se mide la duración real; una desviación mayor del 20% aparece como advertencia. No se rellena con silencio para simular duración. El perfil de bajo consumo usa 640×360 / 360×640, 24 fps, un hilo, un trabajador. Cambia `width` a 1280 o 1920 si tu equipo lo permite; debe ser múltiplo de 32. El reencuadre inicial es un recorte central, no seguimiento inteligente de caras.

## Credenciales, red y costos

| Función | Requisito que configuras personalmente |
| --- | --- |
| Guion/ideas | Credenciales del proveedor seleccionado en MoneyPrinterTurbo; su dominio API autorizado en el entorno donde ejecutes |
| Edge TTS | Acceso al servicio de voz de Microsoft; sin clave en este adaptador |
| Pexels / Pixabay | Clave de API en la configuración original y acceso a sus API/CDN |
| Whisper opcional | Descarga del modelo y memoria/disco suficientes; solo se activa al elegir `subtitles: "whisper"` |
| YouTube | Proyecto Google Cloud con YouTube Data API v3, consentimiento OAuth y cliente tipo **Desktop app**; autorización del canal por su propietario |

El modo predeterminado es `demo` y no consume servicios. En modo live, `allow_llm_requests=false` bloquea el lenguaje antes de llamar al proveedor. Cada intento autorizado reserva `llm_estimate_usd`; las reservas fallidas no se devuelven automáticamente. Si la suma supera `budget_usd`, se bloquea el siguiente intento. Cada episodio tiene su límite; las ideas tienen un límite acumulado separado por serie (`ideas:ID_SERIE`).

**Son estimaciones, no costos facturados ni un tope real impuesto al proveedor.** Configura también límites de gasto en la cuenta del proveedor. Los adaptadores existentes no entregan una facturación uniforme. No hay reintentos automáticos de IA tras errores. No se habilitan proveedores de video/imagen/música de pago en esta primera ruta. Las integraciones opcionales originales siguen disponibles en la aplicación original; se pueden usar sus imágenes como archivos locales con derechos documentados.

La música y los efectos opcionales deben ser archivos locales con una nota de licencia (`audio_license_note`). Se mezclan a bajo volumen en bucle, no se diseñan efectos por evento. El perfil inicial no añade música aleatoria de licencia desconocida. Los archivos visuales locales también requieren nota de derechos. Para stock, conserva y revisa `sources.json` y los registros del descargador original; una clave API no acredita por sí sola todos los usos posibles. La aprobación requiere revisar derechos, contenido y subtítulos.

## YouTube: aprobación, carga privada y programación explícita

Descarga el JSON del cliente OAuth **fuera del repositorio**. Configura su ruta en tu equipo; no pegues el JSON ni los tokens en GitHub:

```powershell
$env:MPT_YOUTUBE_CLIENT_FILE = 'C:\Credenciales\youtube-desktop.json'
.\.venv\Scripts\python.exe -m app.argstudio youtube-auth
```

Se abre el navegador local. OAuth usa `youtube.upload` y `youtube.readonly`; el token se guarda en `storage/argstudio/auth/youtube.json`. Protege la carpeta con permisos de tu cuenta Windows. La aplicación no es un proveedor OAuth verificado: Google puede exigir usuarios de prueba, verificación/auditoría o limitar las cargas a privadas. La duración de los refresh tokens depende del estado de consentimiento del proyecto.

Después de revisar un video real, apruébalo en el panel o por CLI, indicando decisiones reales sobre audiencia y contenido sintético:

```powershell
# Ejemplo solamente: sustituye yes/no según el contenido real.
.\.venv\Scripts\python.exe -m app.argstudio approve ID_TRABAJO --made-for-kids no --synthetic yes --rights-confirmed
# Este comando realiza una carga REAL privada, solo cuando tú lo ejecutes:
.\.venv\Scripts\python.exe -m app.argstudio upload-private ID_TRABAJO
.\.venv\Scripts\python.exe -m app.argstudio upload-status ID_TRABAJO
.\.venv\Scripts\python.exe -m app.argstudio thumbnail ID_TRABAJO
```

La miniatura inicial es el primer fotograma. Su envío requiere un comando separado para que un error de miniatura no repita una carga. Las restricciones de miniaturas personalizadas y de Shorts dependen de YouTube y del canal. YouTube decide la clasificación como Short; la aplicación genera video vertical y conserva el vínculo al episodio.

Las cargas usan sesiones reanudables persistentes. Si se pierde la respuesta, el siguiente comando consulta esa sesión antes de enviar más bytes. Si la sesión expiró y no puede confirmarse el resultado, **no se crea otra carga automáticamente**: revisa YouTube Studio. El bloqueo evita cargas simultáneas y la huella impide cargar un archivo distinto del aprobado. No es posible garantizar exactamente una carga frente a todos los fallos remotos; el caso ambiguo se bloquea para conciliación manual.

Programar una publicación hará el video **público** en el futuro. Por eso está separado del calendario de producción y requiere `--allow-public`. No se ha ejecutado durante el desarrollo. Solo tras probar cargas privadas y autorizarlo expresamente:

```powershell
.\.venv\Scripts\python.exe -m app.argstudio schedule-publication ID_TRABAJO --at '2027-01-04T18:00:00-05:00' --allow-public
```

Se conservan las declaraciones aprobadas y `privacyStatus=private` junto a `publishAt`. YouTube exige un video elegible para programarse, generalmente privado y nunca publicado. Consulta después su estado. Las operaciones consumen la cuota de YouTube Data API del proyecto; comprueba los límites vigentes en Google Cloud. La cola no hace sondeos de YouTube en segundo plano.

## Arranque de Windows sin PowerShell permanente

Cierra primero la instancia original si ya ocupa el puerto 8501. Para iniciar manualmente sin consola:

```powershell
Start-Process .\.venv\Scripts\pythonw.exe -ArgumentList 'tools\argstudio_windows.py'
```

Para instalar el arranque **al iniciar sesión** (sin privilegios de administrador):

```powershell
powershell -File .\tools\Install-ARGStudio.ps1
```

Se registra la tarea `MoneyPrinterTurbo ARG Studio`, que inicia Streamlit en `127.0.0.1:8501` y un trabajador continuo. No inicia la API HTTP ni abre puertos administrativos externos. Después solo abre tu navegador en esa dirección. Requiere una sesión de usuario activa; no es un servicio de Windows previo al inicio de sesión. No se cambia la política de ejecución de PowerShell: si la política de tu organización bloquea scripts, necesitarás el método aprobado por ella.

Para detenerlo o quitar el inicio automático:

```powershell
.\.venv\Scripts\pythonw.exe tools\argstudio_windows.py --stop
powershell -File .\tools\Install-ARGStudio.ps1 -Uninstall
```

No se eliminan configuraciones ni datos. Si cambias de directorio la instalación, vuelve a registrar la tarea. Este instalador fue preparado e inspeccionado en Linux; aún necesita una prueba real en Windows.

## Recuperación, registros y pruebas

Los datos están en `storage/argstudio` (o bajo `MPT_HOME` si ya lo utilizas). Respalda esta carpeta junto con `config.toml` con el trabajador detenido. No edites SQLite mientras está trabajando. `status` no muestra tokens ni URLs de sesión de carga. Nunca subas esta carpeta a Git.

- Tras un reinicio, el siguiente trabajador recupera trabajos marcados como `running`.
- Un error terminal queda `failed` sin reintentar llamadas potencialmente costosas. Usa `retry ID_TRABAJO` después de corregir su causa y vuelve a ejecutar el trabajador.
- Se conservan audio/subtítulos, material local, escenas, MP4 final y manifiestos con hashes. Archivos incompletos no se consideran checkpoints válidos. Cada render es una instantánea: editar los originales locales no cambia automáticamente un episodio ya renderizado.
- Los errores y resultados quedan en SQLite; la ejecución desde el panel escribe `worker.log`; el supervisor escribe `windows.log`. No se guardan cuerpos de errores de proveedores que puedan contener credenciales.

```powershell
.\.venv\Scripts\python.exe -m pytest -q test/services/test_argstudio.py
.\.venv\Scripts\ruff.exe check app/argstudio webui/pages/ARG_Studio.py tools/argstudio_windows.py test/services/test_argstudio.py
```

La validación de desarrollo incluye renderizado/decodificación real con FFmpeg, persistencia, concurrencia, recuperación, presupuesto, continuidad, interacción de Streamlit y transporte simulado de OAuth/YouTube. Las pruebas no llaman a proveedores pagados ni a un canal real.

## Límites de esta versión

No se ha verificado un episodio real de 8–12 minutos con servicios externos, calidad editorial, permisos de tu canal, carga real ni instalación Windows. La demostración local silenciosa y las respuestas simuladas de API no sustituyen esas pruebas. El calendario es semanal explícito, los Shorts son adaptaciones narradas y el recorte es central. La edición avanzada del canon, cambios de perfiles existentes, selección inteligente de encuadres, diseño de miniaturas, efectos por escena y conciliación automática de cargas ambiguas quedan para una iteración posterior. El panel aprueba/revoca, pero las operaciones de red de YouTube requieren CLI explícita. Ningún entorno se publicó como parte de estos cambios.
