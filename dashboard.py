import sys
import warnings

# Silenciar el aviso de deprecación de asyncio en versiones recientes de Python
warnings.filterwarnings("ignore", category=DeprecationWarning, module="asyncio")

# Windows: evita el traceback en consola cuando el navegador cierra el socket bruscamente.
# (Parche global sobre asyncio; solo afecta al ruido en consola.)
if sys.platform == "win32":
    try:
        from asyncio import ProactorEventLoop
        ProactorEventLoop._call_connection_lost = lambda self, exc=None: None
    except Exception:
        pass

import io
import mimetypes
import os

import pandas as pd
import pydeck as pdk
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

# ----------------------------------------------------
# 1. CONFIGURACIÓN DE PÁGINA Y ENTORNO
# ----------------------------------------------------
st.set_page_config(
    page_title="Dashboard FTTH & Mapeo MTTR",
    page_icon="📡",
    layout="wide",
)

load_dotenv()

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_USER = os.getenv("DB_USER", "root")  # Recomendado: usuario de solo lectura en producción
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "ftth_bot_db")
DB_PORT = os.getenv("DB_PORT", "3306")

RUTA_RESGUARDO = os.path.abspath(
    os.getenv("RUTA_RESGUARDO", os.path.join(os.getcwd(), "Resguardo_FTTH"))
)

TODOS = "-- Todos los Tickets --"


@st.cache_resource
def get_engine():
    url = f"mysql+mysqlconnector://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    return create_engine(url, pool_recycle=3600, pool_pre_ping=True)


engine = get_engine()

# ----------------------------------------------------
# 2. EXTRACCIÓN DE DATOS (CON CACHÉ)
# ----------------------------------------------------
def _sin_zona_horaria(serie: pd.Series) -> pd.Series:
    """Convierte a datetime y quita la zona horaria (Excel no la soporta)."""
    serie = pd.to_datetime(serie, errors="coerce")
    if getattr(serie.dt, "tz", None) is not None:
        serie = serie.dt.tz_localize(None)
    return serie


@st.cache_data(ttl=60, show_spinner="Cargando tickets...")
def cargar_datos_tickets() -> pd.DataFrame:
    query = text("""
        SELECT
            t.id_ticket,
            tec.nombre AS tecnico,
            t.estado,
            t.diagnostico_cierre,
            t.fecha_inicio,
            t.fecha_cierre,
            ev.latitud,
            ev.longitud,
            e.clientes_afectados_totales,
            e.mttr_ponderado_horas,
            e.ttr_maximo_horas,
            e.tiempo_vortex_horas,
            e.brecha_vortex_mttr
        FROM tickets t
        LEFT JOIN tecnicos tec ON t.id_tecnico = tec.id_tecnico
        LEFT JOIN mttr_eventos e ON t.id_ticket = e.id_ticket
        LEFT JOIN (
            SELECT e1.id_ticket, e1.latitud, e1.longitud
            FROM evidencias e1
            INNER JOIN (
                SELECT id_ticket, MAX(id_evidencia) AS max_id
                FROM evidencias
                WHERE tipo = 'UBICACION' AND latitud IS NOT NULL
                GROUP BY id_ticket
            ) e2 ON e1.id_evidencia = e2.max_id
        ) ev ON t.id_ticket = ev.id_ticket
        ORDER BY t.fecha_inicio DESC
    """)
    with engine.connect() as conn:
        df = pd.read_sql(query, con=conn)

    # Tipos numéricos
    columnas_num = [
        "latitud", "longitud", "clientes_afectados_totales",
        "mttr_ponderado_horas", "ttr_maximo_horas",
        "tiempo_vortex_horas", "brecha_vortex_mttr",
    ]
    for col in columnas_num:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Fechas seguras (evita fallos con .dt.date si hay nulos o tipos mixtos)
    df["fecha_inicio"] = _sin_zona_horaria(df["fecha_inicio"])
    df["fecha_cierre"] = _sin_zona_horaria(df["fecha_cierre"])

    # Textos sin nulos para que los filtros no fallen
    df["tecnico"] = df["tecnico"].fillna("Sin asignar")
    df["estado"] = df["estado"].fillna("SIN_ESTADO")

    # Un ticket = una fila. Si mttr_eventos tuviera varias filas por ticket,
    # los JOIN duplicarían filas e inflarían métricas. Se conserva la primera
    # (la más reciente por el ORDER BY).
    df = df.drop_duplicates(subset="id_ticket", keep="first").reset_index(drop=True)
    return df


@st.cache_data(ttl=60)
def cargar_chat_ticket(ticket_id: str) -> pd.DataFrame:
    query = text("""
        SELECT emisor_nombre, mensaje, fecha_hora
        FROM historial_chat
        WHERE id_ticket = :ticket_id
        ORDER BY fecha_hora ASC
    """)
    with engine.connect() as conn:
        df = pd.read_sql(query, con=conn, params={"ticket_id": ticket_id})
    df["fecha_hora"] = _sin_zona_horaria(df["fecha_hora"])
    return df


def ruta_segura_ticket(ticket_id: str) -> str | None:
    """Devuelve la carpeta del ticket solo si está dentro de RUTA_RESGUARDO."""
    nombre = os.path.basename(str(ticket_id))
    ruta = os.path.abspath(os.path.join(RUTA_RESGUARDO, nombre))
    if os.path.commonpath([RUTA_RESGUARDO, ruta]) != RUTA_RESGUARDO:
        return None
    return ruta


def fmt_horas(valor, decimales=1) -> str:
    return f"{valor:.{decimales}f} hrs" if pd.notnull(valor) else "N/A"


# ----------------------------------------------------
# 3. INTERFAZ
# ----------------------------------------------------
st.title("📡 FTTH Maintenance Dashboard & MTTR Analytics")

try:
    df_tickets = cargar_datos_tickets()
except Exception as e:
    st.error(f"❌ Error conectando a la base de datos: {e}")
    st.stop()

if df_tickets.empty:
    st.info("No hay tickets registrados todavía.")
    st.stop()

# ---------------- BARRA LATERAL: FILTROS GLOBALES ----------------
with st.sidebar:
    st.header("🔎 Filtros globales")

    if st.button("🔄 Actualizar datos", width="stretch"):
        st.cache_data.clear()
        st.rerun()

    # Rango de fechas (por fecha de inicio)
    fechas_validas = df_tickets["fecha_inicio"].dropna()
    rango_fechas = None
    if not fechas_validas.empty:
        f_min, f_max = fechas_validas.min().date(), fechas_validas.max().date()
        seleccion = st.date_input(
            "Rango de fechas (inicio del ticket)",
            value=(f_min, f_max),
            min_value=f_min,
            max_value=f_max,
        )
        if isinstance(seleccion, (tuple, list)) and len(seleccion) == 2:
            rango_fechas = seleccion
        else:
            st.caption("Selecciona fecha de inicio y de fin para aplicar el rango.")

    tecnicos = sorted(df_tickets["tecnico"].unique().tolist())
    sel_tecnicos = st.multiselect("Técnico", tecnicos, default=[])

    estados = sorted(df_tickets["estado"].unique().tolist())
    sel_estados = st.multiselect("Estado", estados, default=[])

# Aplicar filtros (vacío en multiselect = sin filtrar)
df_filtrado = df_tickets.copy()

if rango_fechas:
    f_ini, f_fin = rango_fechas
    fechas = df_filtrado["fecha_inicio"].dt.date
    # Se conservan los tickets sin fecha para no ocultarlos por error
    df_filtrado = df_filtrado[fechas.isna() | ((fechas >= f_ini) & (fechas <= f_fin))]

if sel_tecnicos:
    df_filtrado = df_filtrado[df_filtrado["tecnico"].isin(sel_tecnicos)]

if sel_estados:
    df_filtrado = df_filtrado[df_filtrado["estado"].isin(sel_estados)]

with st.sidebar:
    lista_tickets = df_filtrado["id_ticket"].tolist()
    ticket_filtro = st.selectbox("Ticket específico", [TODOS] + lista_tickets)
    st.caption(f"{len(df_filtrado)} de {len(df_tickets)} tickets con los filtros actuales.")

if ticket_filtro != TODOS:
    df_filtrado = df_filtrado[df_filtrado["id_ticket"] == ticket_filtro]

if df_filtrado.empty:
    st.warning("⚠️ Ningún ticket coincide con los filtros seleccionados.")
    st.stop()

# ---------------- MÉTRICAS SUPERIORES ----------------
col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Total Tickets", len(df_filtrado))
col2.metric("En Proceso 🛠️", int((df_filtrado["estado"] == "EN_PROCESO").sum()))
col3.metric("Cerrados ✅", int((df_filtrado["estado"] == "CERRADO").sum()))
col4.metric(
    "Prom. MTTR Campo ⏱️",
    fmt_horas(df_filtrado["mttr_ponderado_horas"].mean()),
    help="Promedio solo de los tickets con MTTR calculado.",
)
col5.metric(
    "Prom. VORTEX 🌐",
    fmt_horas(df_filtrado["tiempo_vortex_horas"].mean()),
    help="Promedio solo de los tickets con tiempo VORTEX registrado.",
)

st.markdown("---")

tab1, tab2, tab3, tab4 = st.tabs([
    "📊 Visión General & Mapa",
    "📈 Analítica MTTR vs VORTEX",
    "📸 Evidencias & Conversación",
    "📥 Reportes Excel",
])

# ----------------------------------------------------
# PESTAÑA 1: VISIÓN GENERAL Y MAPA
# ----------------------------------------------------
with tab1:
    st.subheader("📋 Registro Detallado de Tickets")

    st.dataframe(
        df_filtrado[[
            "id_ticket", "tecnico", "estado", "fecha_inicio", "fecha_cierre",
            "latitud", "longitud", "mttr_ponderado_horas", "diagnostico_cierre",
        ]],
        width="stretch",
        column_config={
            "id_ticket": "ID Ticket",
            "tecnico": "Técnico Asignado",
            "estado": "Estado Actual",
            "fecha_inicio": "Inicio",
            "fecha_cierre": "Cierre",
            "latitud": "Latitud (GPS)",
            "longitud": "Longitud (GPS)",
            "mttr_ponderado_horas": "MTTR (hrs)",
            "diagnostico_cierre": "Diagnóstico / Solución",
        },
    )

    st.markdown("---")
    st.subheader("📍 Mapa de Calor & Geolocalización de Eventos")

    df_mapa = df_filtrado.dropna(subset=["latitud", "longitud"]).copy()

    if not df_mapa.empty:
        tipo_mapa = st.radio(
            "Estilo de Mapa:",
            ["Mapa de Calor (Heatmap)", "Puntos Específicos"],
            horizontal=True,
        )

        lat_prom = df_mapa["latitud"].mean()
        lon_prom = df_mapa["longitud"].mean()
        zoom_val = 13 if ticket_filtro != TODOS else 10

        if tipo_mapa == "Mapa de Calor (Heatmap)":
            layer = pdk.Layer(
                "HeatmapLayer",
                data=df_mapa,
                get_position=["longitud", "latitud"],
                radiusPixels=40,
                intensity=2,
                threshold=0.05,
            )
            view_state = pdk.ViewState(
                latitude=lat_prom, longitude=lon_prom, zoom=zoom_val, pitch=0
            )
            st.pydeck_chart(
                pdk.Deck(
                    layers=[layer],
                    initial_view_state=view_state,
                    # Estilo Carto integrado: no requiere token de Mapbox
                    map_style="dark",
                    tooltip={"text": "Densidad de incidencias"},
                ),
                width="stretch",
            )
        else:
            df_mapa_render = df_mapa.rename(
                columns={"latitud": "latitude", "longitud": "longitude"}
            )
            st.map(df_mapa_render, zoom=zoom_val, width="stretch")

        if ticket_filtro != TODOS:
            lat = df_mapa.iloc[0]["latitud"]
            lon = df_mapa.iloc[0]["longitud"]
            st.caption(
                f"📍 Coordenadas `{ticket_filtro}`: `{lat}, {lon}` | "
                f"[Abrir en Google Maps](https://maps.google.com/?q={lat},{lon})"
            )
    else:
        st.info("ℹ️ No hay coordenadas GPS registradas para la selección actual.")

# ----------------------------------------------------
# PESTAÑA 2: ANALÍTICA MTTR VS VORTEX
# ----------------------------------------------------
with tab2:
    st.subheader("📈 Analítica Comparativa MTTR vs VORTEX")

    df_analitica = df_filtrado.dropna(subset=["mttr_ponderado_horas"]).copy()

    if not df_analitica.empty:
        c_m1, c_m2, c_m3 = st.columns(3)
        avg_ttr = df_analitica["ttr_maximo_horas"].mean()
        avg_mttr = df_analitica["mttr_ponderado_horas"].mean()
        avg_vortex = df_analitica["tiempo_vortex_horas"].mean()

        c_m1.metric("Prom. TTR Máximo", fmt_horas(avg_ttr, 2))
        c_m2.metric("Prom. MTTR Ponderado", fmt_horas(avg_mttr, 2))

        if pd.notnull(avg_vortex) and pd.notnull(avg_mttr):
            brecha = avg_vortex - avg_mttr
            c_m3.metric(
                "Prom. VORTEX vs Campo",
                f"{avg_vortex:.2f} hrs",
                delta=f"{brecha:.2f} hrs brecha",
                delta_color="inverse",
            )
        else:
            c_m3.metric("Prom. VORTEX", "N/A")

        st.markdown("### 📊 Tabla de Tiempos y Brechas por Evento")
        st.dataframe(
            df_analitica[[
                "id_ticket", "tecnico", "clientes_afectados_totales",
                "ttr_maximo_horas", "mttr_ponderado_horas",
                "tiempo_vortex_horas", "brecha_vortex_mttr",
            ]],
            width="stretch",
            column_config={
                "id_ticket": "Ticket",
                "tecnico": "Técnico",
                "clientes_afectados_totales": "Clientes Afectados",
                "ttr_maximo_horas": "TTR Máximo (hrs)",
                "mttr_ponderado_horas": "MTTR Campo (hrs)",
                "tiempo_vortex_horas": "Tiempo VORTEX (hrs)",
                "brecha_vortex_mttr": "Brecha VORTEX-MTTR",
            },
        )

        st.markdown("### 📉 Comparativa de Tiempos Campo vs VORTEX")
        chart_data = (
            df_analitica.set_index("id_ticket")[["mttr_ponderado_horas", "tiempo_vortex_horas"]]
            .dropna()
        )
        if not chart_data.empty:
            st.bar_chart(chart_data, width="stretch")

        # ---- Tendencia semanal ----
        st.markdown("### 📆 Tendencia Semanal (promedio de horas)")
        df_tend = df_analitica.dropna(subset=["fecha_inicio"])
        if not df_tend.empty:
            tendencia = (
                df_tend.set_index("fecha_inicio")
                .resample("W")[["mttr_ponderado_horas", "tiempo_vortex_horas"]]
                .mean()
                .dropna(how="all")
            )
            if len(tendencia) >= 2:
                tendencia = tendencia.rename(columns={
                    "mttr_ponderado_horas": "MTTR Campo",
                    "tiempo_vortex_horas": "VORTEX",
                })
                st.line_chart(tendencia, width="stretch")
            else:
                st.caption("Se necesitan al menos 2 semanas de datos para mostrar la tendencia.")
        else:
            st.caption("No hay fechas de inicio válidas para calcular la tendencia.")

        # ---- Ranking por técnico ----
        st.markdown("### 🧑‍🔧 Desempeño por Técnico")
        ranking = (
            df_analitica.groupby("tecnico")
            .agg(
                tickets=("id_ticket", "nunique"),
                clientes_afectados=("clientes_afectados_totales", "sum"),
                mttr_promedio=("mttr_ponderado_horas", "mean"),
                vortex_promedio=("tiempo_vortex_horas", "mean"),
            )
            .sort_values("mttr_promedio")
            .reset_index()
        )
        st.dataframe(
            ranking,
            width="stretch",
            hide_index=True,
            column_config={
                "tecnico": "Técnico",
                "tickets": "Tickets",
                "clientes_afectados": "Clientes Afectados",
                "mttr_promedio": st.column_config.NumberColumn("MTTR Prom. (hrs)", format="%.2f"),
                "vortex_promedio": st.column_config.NumberColumn("VORTEX Prom. (hrs)", format="%.2f"),
            },
        )
        if len(ranking) > 1:
            st.bar_chart(
                ranking.set_index("tecnico")[["mttr_promedio"]].rename(
                    columns={"mttr_promedio": "MTTR Prom. (hrs)"}
                ),
                width="stretch",
            )
    else:
        st.info("No hay datos comparativos calculados para mostrar en esta sección.")

# ----------------------------------------------------
# PESTAÑA 3: EVIDENCIAS & CONVERSACIÓN (CON DESCARGA)
# ----------------------------------------------------
with tab3:
    st.subheader("🖼️ Galería de Evidencias e Historial del Ticket")

    ids_disponibles = df_filtrado["id_ticket"].tolist()
    ticket_seleccionado = st.selectbox(
        "Selecciona el Ticket a revisar:", ids_disponibles, key="select_evidencias"
    )

    col_evidencias, col_chat = st.columns([0.6, 0.4])

    with col_evidencias:
        st.markdown(f"### 📷 Evidencias de Campo: `{ticket_seleccionado}`")
        ruta_carpeta_ticket = ruta_segura_ticket(ticket_seleccionado)

        if ruta_carpeta_ticket is None:
            st.error("ID de ticket no válido para acceder a la carpeta de evidencias.")
        elif os.path.isdir(ruta_carpeta_ticket):
            archivos_fotos = sorted(
                f for f in os.listdir(ruta_carpeta_ticket)
                if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
            )

            if archivos_fotos:
                cols_fotos = st.columns(3)
                for idx, foto in enumerate(archivos_fotos):
                    ruta_foto = os.path.join(ruta_carpeta_ticket, foto)
                    mime = mimetypes.guess_type(ruta_foto)[0] or "application/octet-stream"
                    with cols_fotos[idx % 3]:
                        st.image(ruta_foto, caption=foto, width="stretch")
                        with open(ruta_foto, "rb") as file_bytes:
                            st.download_button(
                                label="⬇️ Descargar",
                                data=file_bytes.read(),
                                file_name=f"{ticket_seleccionado}_{foto}",
                                mime=mime,
                                key=f"dl_{ticket_seleccionado}_{idx}",
                                width="stretch",
                            )
            else:
                st.warning(f"No hay imágenes en: `{ruta_carpeta_ticket}`")
        else:
            st.warning(f"No existe la carpeta en disco: `{ruta_carpeta_ticket}`")

    with col_chat:
        st.markdown("### 💬 Conversación / Chat del Hilo")
        try:
            df_chat = cargar_chat_ticket(ticket_seleccionado)
        except Exception as e:
            df_chat = pd.DataFrame()
            st.error(f"No se pudo cargar el chat: {e}")

        if not df_chat.empty:
            for _, fila in df_chat.iterrows():
                fecha_str = (
                    fila["fecha_hora"].strftime("%d/%m %H:%M")
                    if pd.notnull(fila["fecha_hora"]) else ""
                )
                st.chat_message("user").write(
                    f"**{fila['emisor_nombre']}** _({fecha_str})_:\n{fila['mensaje']}"
                )
        else:
            st.info("No hay historial de mensajes guardado.")

# ----------------------------------------------------
# PESTAÑA 4: REPORTE EXPORTABLE A EXCEL
# ----------------------------------------------------
with tab4:
    st.subheader("📥 Exportación de Reportes Consolidados")
    st.markdown(
        "El reporte usa los **filtros globales** de la barra lateral "
        "(fechas, técnico, estado y ticket)."
    )

    df_reporte = df_filtrado.copy()
    st.markdown(f"**Registros encontrados:** `{len(df_reporte)}`")
    st.dataframe(df_reporte, width="stretch")

    if not df_reporte.empty:
        output_buffer = io.BytesIO()
        with pd.ExcelWriter(output_buffer, engine="openpyxl") as writer:
            df_reporte.to_excel(writer, sheet_name="Reporte_FTTH", index=False)

            # Resumen por técnico (solo tickets con MTTR)
            df_res = df_reporte.dropna(subset=["mttr_ponderado_horas"])
            if not df_res.empty:
                (
                    df_res.groupby("tecnico")
                    .agg(
                        tickets=("id_ticket", "nunique"),
                        clientes_afectados=("clientes_afectados_totales", "sum"),
                        mttr_promedio=("mttr_ponderado_horas", "mean"),
                        vortex_promedio=("tiempo_vortex_horas", "mean"),
                    )
                    .reset_index()
                    .to_excel(writer, sheet_name="Resumen_Tecnicos", index=False)
                )

            # Si hay un solo ticket, adjuntar su historial de chat
            if len(df_reporte) == 1:
                ticket_rep = df_reporte.iloc[0]["id_ticket"]
                cargar_chat_ticket(ticket_rep).to_excel(
                    writer, sheet_name="Historial_Chat", index=False
                )

        output_buffer.seek(0)

        st.download_button(
            label="📊 Descargar Reporte en Excel (.xlsx)",
            data=output_buffer,
            file_name=f"Reporte_FTTH_{pd.Timestamp.now().strftime('%Y%m%d_%H%M')}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )
