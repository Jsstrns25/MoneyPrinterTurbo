"""Additional Streamlit page; the original MoneyPrinterTurbo page is preserved."""

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.argstudio.models import Settings  # noqa: E402
from app.argstudio.store import Store  # noqa: E402
from app.argstudio.worker import plan_week  # noqa: E402
from app.argstudio.youtube import approve, reject  # noqa: E402

st.set_page_config(page_title="ARG Studio", page_icon="📻", layout="wide")
st.title("ARG Studio")
st.caption(
    "Historias de ficción, producción local y revisión editorial. Las cargas se ejecutan por separado y siempre comienzan como privadas."
)
store = Store()


def action(callback):
    try:
        callback()
        st.success("Guardado correctamente")
    except (ValueError, OSError) as exc:
        st.error(str(exc))


with st.expander("Crear una serie", expanded=not store.rows("series")):
    with st.form("new_series"):
        name = st.text_input("Nombre")
        premise = st.text_area("Premisa y estilo narrativo")
        mode = st.selectbox(
            "Modo",
            ["demo", "live"],
            format_func=lambda v: (
                "Prueba técnica sin narración ni servicios externos"
                if v == "demo"
                else "Producción con proveedores configurados"
            ),
        )
        minutes = st.slider("Duración objetivo (minutos)", 8, 12, 10)
        shorts = st.slider("Shorts por episodio", 2, 4, 2)
        provider = st.text_input(
            "Proveedor de lenguaje (ID de MoneyPrinterTurbo; vacío usa el actual)"
        )
        visuals = st.selectbox("Material visual", ["pexels", "pixabay", "local"])
        local_files = st.text_area("Archivos visuales locales (una ruta por línea)")
        visual_rights = st.text_area("Licencias / procedencia de archivos locales")
        voice = st.text_input("Voz Edge TTS", "es-PA-MargaritaNeural-Female")
        music = st.text_input("Archivo de música opcional")
        effect = st.text_input(
            "Archivo de ambiente / efecto opcional (se mezcla en bucle)"
        )
        audio_rights = st.text_area("Licencias / derechos de música y efectos")
        authorized = st.checkbox(
            "Autorizo solicitudes al modelo seleccionado; entiendo su tarifa"
        )
        estimate = st.number_input(
            "Reserva estimada por solicitud de guion (USD)", min_value=0.0, value=0.10
        )
        budget = st.number_input(
            "Límite de reservas por episodio (USD)", min_value=0.0, value=0.0
        )
        st.caption(
            "Las reservas no son facturas ni un límite de gasto garantizado por el proveedor. Configura también límites en su cuenta. Un proceso y un hilo de renderizado por defecto."
        )
        if st.form_submit_button("Crear serie"):
            action(
                lambda: store.create_series(
                    name,
                    premise,
                    Settings(
                        mode=mode,
                        target_minutes=minutes,
                        shorts_count=shorts,
                        llm_provider=provider,
                        visual_provider=visuals,
                        local_visuals=[
                            p.strip() for p in local_files.splitlines() if p.strip()
                        ],
                        asset_license_note=visual_rights,
                        voice=voice,
                        music_file=music,
                        sound_effect_file=effect,
                        audio_license_note=audio_rights,
                        allow_llm_requests=authorized,
                        llm_estimate_usd=estimate,
                        budget_usd=budget,
                    ),
                )
            )

series = store.rows("series")
if series:
    selected = st.selectbox("Serie", series, format_func=lambda s: s["name"])
    st.json(json.loads(selected["canon"]), expanded=False)
    st.caption(
        "Ideas originales: ejecuta el siguiente comando. En modo live usa la autorización y un presupuesto acumulado de ideas para esta serie, separado del presupuesto de cada episodio."
    )
    st.code(f"python -m app.argstudio ideas {selected['id']}")
    for idea_file in sorted((store.root / "ideas" / selected["id"]).glob("*.json")):
        with st.expander("Ideas: " + idea_file.stem):
            st.json(json.loads(idea_file.read_text(encoding="utf-8")))
    with st.expander("Configuración de esta serie"):
        st.json(json.loads(selected["settings"]))
        st.caption(
            "Los episodios guardan una copia de su configuración. Para otro perfil de producción, crea una nueva serie; no se modifican tareas ya iniciadas."
        )
    with st.form("calendar"):
        day = st.date_input("Primer día", date.today())
        weekdays = st.multiselect(
            "Días de producción (lunes=0)", list(range(7)), default=[0, 3]
        )
        hour = st.number_input("Hora local", 0, 23, 18)
        zone = st.text_input("Zona horaria", "America/Panama")
        if st.form_submit_button("Programar una semana"):
            action(
                lambda: plan_week(
                    store, selected["id"], day.isoformat(), tuple(weekdays), hour, zone
                )
            )
    slot = st.text_input(
        "Identificador editorial del episodio", value=date.today().isoformat()
    )
    if st.button("Añadir episodio a la cola ahora"):
        action(lambda: store.enqueue(selected["id"], slot))

st.subheader("Cola y calendario")
st.dataframe(store.rows("jobs"), use_container_width=True)
if st.button("Procesar tareas pendientes en segundo plano"):
    log = store.root / "worker.log"
    with log.open("a", encoding="utf-8") as output:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "app.argstudio",
                "--root",
                str(store.root),
                "worker",
                "--once",
            ],
            cwd=Path(__file__).resolve().parents[2],
            stdout=output,
            stderr=output,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    st.info(
        "Procesamiento solicitado. Un bloqueo impide ejecutar dos trabajadores simultáneos. Actualiza para consultar el resultado."
    )
if st.button("Actualizar"):
    st.rerun()
for job in store.rows("jobs"):
    if job["state"] == "failed" and st.button(f"Reintentar {job['kind']}: {job['id']}"):
        action(lambda j=job: store.retry(j["id"]))

st.subheader("Revisión de videos")
for job in store.rows("jobs"):
    if job["state"] != "complete" or job["kind"] == "story":
        continue
    result = json.loads(job["result"])
    with st.expander(f"{result['title']} · {job['kind']}"):
        st.video(result["path"])
        st.json(result)
        if result["demo"]:
            st.info(
                "Prueba técnica breve y sin narración: no se permite cargarla a YouTube."
            )
            continue
        with st.form("review_" + job["id"]):
            kids = st.selectbox("¿Dirigido a niños?", ["Seleccionar", "Sí", "No"])
            synthetic = st.selectbox(
                "¿Requiere declarar contenido sintético?", ["Seleccionar", "Sí", "No"]
            )
            rights = st.checkbox(
                "Revisé el video, subtítulos y licencias de todos los materiales"
            )
            if st.form_submit_button("Aprobar carga privada"):
                action(
                    lambda j=job: approve(
                        store,
                        j["id"],
                        made_for_kids=None if kids == "Seleccionar" else kids == "Sí",
                        contains_synthetic_media=None
                        if synthetic == "Seleccionar"
                        else synthetic == "Sí",
                        rights_confirmed=rights,
                    )
                )
        if st.button("Revocar aprobación", key="reject_" + job["id"]):
            action(lambda j=job: reject(store, j["id"]))
        st.code(f"python -m app.argstudio upload-private {job['id']}")
st.subheader("Reservas de costos y publicaciones")
st.dataframe(store.rows("costs"), use_container_width=True)
st.dataframe(store.rows("publications"), use_container_width=True)
st.caption(
    "Conecta YouTube y ejecuta cargas explícitas desde el equipo local siguiendo docs/arg-studio.md. El calendario solo produce contenido; no publica."
)
