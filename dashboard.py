import os
import sys
import warnings

# Silenciar el aviso de deprecación de asyncio en versiones recientes de Python
warnings.filterwarnings("ignore", category=DeprecationWarning, module="asyncio")

# Ignorar excepciones de reconexión/cierre de socket brusco en Windows
if sys.platform == 'win32':
    try:
        from asyncio import ProactorEventLoop
        # Evita el traceback en consola cuando un cliente WebBrowser cierra el socket
        ProactorEventLoop._call_connection_lost = lambda self, exc=None: None
    except Exception:
        pass

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

# ----------------------------------------------------
# 1. CONFIGURACIÓN DE PÁGINA Y RUTAS
# ----------------------------------------------------
st.set_page_config(
    page_title="Dashboard FTTH & MTTR",
    page_icon="📡",
    layout="wide"
)

if getattr(sys, 'frozen', False):
    DIRECTORIO_BASE = os.path.dirname(sys.executable)
else:
    DIRECTORIO_BASE = os.path.dirname(os.path.abspath(__file__))

ruta_env = os.path.join(DIRECTORIO_BASE, '.env')
load_dotenv(dotenv_path=ruta_env)

# Lectura de variables de entorno de la base de datos
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "ftth_bot_db")
DB_PORT = os.getenv("DB_PORT", "3306")

# Carga la ruta de resguardo desde el archivo .env
CARPETA_BASE_RESGUARDO = os.getenv(
    "RUTA_RESGUARDO", 
    os.path.join(DIRECTORIO_BASE, "dist", "Bot_TK", "Resguardo_FTTH")
)

# ----------------------------------------------------
# 2. CONEXIÓN A LA BASE DE DATOS
# ----------------------------------------------------
@st.cache_resource
def get_db_engine():
    DATABASE_URL = f"mysql+mysqlconnector://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    return create_engine(DATABASE_URL)

try:
    engine = get_db_engine()
except Exception as e:
    st.error(f"❌ Error al conectar con la base de datos: {e}")
    st.stop()

# ----------------------------------------------------
# 3. FUNCIONES PARA CARGAR Y ACTUALIZAR DATOS
# ----------------------------------------------------
def cargar_datos_tickets():
    query = """
        SELECT 
            t.id_ticket,
            tec.nombre AS tecnico,
            t.estado,
            t.diagnostico_cierre,
            t.fecha_inicio,
            t.fecha_cierre,
            e.clientes_afectados_totales,
            e.mttr_ponderado_horas,
            e.ttr_maximo_horas,
            e.tiempo_vortex_horas,
            e.brecha_vortex_mttr
        FROM tickets t
        LEFT JOIN tecnicos tec ON t.id_tecnico = tec.id_tecnico
        LEFT JOIN mttr_eventos e ON t.id_ticket = e.id_ticket
        ORDER BY t.fecha_inicio DESC
    """
    return pd.read_sql(query, con=engine)

def cargar_evidencias_ticket(ticket_id):
    query = f"""
        SELECT id_evidencia, tipo, latitud, longitud, ruta_nube, fecha_registro
        FROM evidencias
        WHERE id_ticket = '{ticket_id}'
        ORDER BY fecha_registro ASC
    """
    return pd.read_sql(query, con=engine)

def cargar_chat_ticket(ticket_id):
    query = f"""
        SELECT emisor_nombre, mensaje, fecha_hora
        FROM historial_chat
        WHERE id_ticket = '{ticket_id}'
        ORDER BY fecha_hora ASC
    """
    return pd.read_sql(query, con=engine)

def cargar_recuperaciones_ticket(ticket_id):
    query = f"""
        SELECT porcentaje_online, clientes_online, fecha_registro
        FROM mttr_recuperaciones
        WHERE id_ticket = '{ticket_id}'
        ORDER BY fecha_registro ASC
    """
    return pd.read_sql(query, con=engine)

def actualizar_vortex_db(ticket_id, tiempo_vortex, mttr_ponderado):
    try:
        mttr_val = float(mttr_ponderado) if pd.notnull(mttr_ponderado) else 0.0
        brecha = round(abs(tiempo_vortex - mttr_val), 2)
        
        query = text("""
            UPDATE mttr_eventos 
            SET tiempo_vortex_horas = :vortex, brecha_vortex_mttr = :brecha 
            WHERE id_ticket = :ticket
        """)
        
        with engine.begin() as conn:
            conn.execute(query, {"vortex": tiempo_vortex, "brecha": brecha, "ticket": ticket_id})
        return True
    except Exception as e:
        st.error(f"Error actualizando VORTEX: {e}")
        return False

# ----------------------------------------------------
# 4. INTERFAZ PRINCIPAL
# ----------------------------------------------------
st.title("📡 Panel de Control FTTH & Módulo MTTR / VORTEX")
st.markdown("Monitoreo unificado de tickets, evidencias fotográficas, chats e insumo de VORTEX.")

if st.button("🔄 Actualizar Datos"):
    st.rerun()

try:
    df_tickets = cargar_datos_tickets()
except Exception as e:
    st.error(f"Error al ejecutar la consulta principal: {e}")
    st.stop()

# --- MÉTRICAS GENERALES ---
col1, col2, col3, col4, col5 = st.columns(5)

total_tickets = len(df_tickets)
tickets_en_proceso = len(df_tickets[df_tickets['estado'] == 'EN_PROCESO'])
tickets_cerrados = len(df_tickets[df_tickets['estado'] == 'CERRADO'])
promedio_mttr = round(df_tickets['mttr_ponderado_horas'].mean(), 2) if not df_tickets.empty and df_tickets['mttr_ponderado_horas'].notnull().any() else 0.0
promedio_vortex = round(df_tickets['tiempo_vortex_horas'].mean(), 2) if not df_tickets.empty and df_tickets['tiempo_vortex_horas'].notnull().any() else 0.0

col1.metric("Total Tickets", total_tickets)
col2.metric("En Proceso 🛠️", tickets_en_proceso)
col3.metric("Cerrados ✅", tickets_cerrados)
col4.metric("Prom. MTTR Campo ⏱️", f"{promedio_mttr} hrs")
col5.metric("Prom. VORTEX 🌐", f"{promedio_vortex} hrs")

st.divider()

# --- PESTAÑAS PRINCIPALES ---
tab_dashboard, tab_mttr_vortex, tab_multimedia = st.tabs(["📊 Visión General", "📈 Analítica MTTR vs VORTEX", "📸 Evidencias & Conversación"])

# TAB 1: VISIÓN GENERAL
with tab_dashboard:
    col_graf1, col_graf2 = st.columns(2)

    with col_graf1:
        st.subheader("📊 Tickets por Estado")
        if not df_tickets.empty:
            fig_estado = px.pie(
                df_tickets, 
                names='estado', 
                color='estado',
                color_discrete_map={
                    'EN_PROCESO': '#3498db',
                    'ESPERANDO_CIERRE': '#f39c12',
                    'CERRADO': '#2ecc71'
                },
                hole=0.4
            )
            st.plotly_chart(fig_estado, width="stretch")
        else:
            st.info("No hay registros de tickets.")

    with col_graf2:
        st.subheader("👨‍🔧 Tickets por Técnico")
        if not df_tickets.empty and 'tecnico' in df_tickets.columns:
            df_tec = df_tickets['tecnico'].value_counts().reset_index()
            df_tec.columns = ['Técnico', 'Cantidad']
            fig_tec = px.bar(df_tec, x='Técnico', y='Cantidad', color='Cantidad', color_continuous_scale='Blues')
            st.plotly_chart(fig_tec, width="stretch")
        else:
            st.info("No hay datos de técnicos asignados.")

    st.subheader("📋 Registro de Tickets Integrados")
    st.dataframe(df_tickets, width="stretch", hide_index=True)

# TAB 2: DETALLE MTTR Y CONCILIACIÓN CON VORTEX
with tab_mttr_vortex:
    st.subheader("🔎 Análisis de Des-afectación por Ticket")
    
    if not df_tickets.empty:
        ticket_sel_mttr = st.selectbox("Selecciona un Ticket para analizar métricas:", df_tickets['id_ticket'].unique(), key="sb_mttr")
        
        info_ticket = df_tickets[df_tickets['id_ticket'] == ticket_sel_mttr].iloc[0]
        
        col_info, col_chart = st.columns([1, 2])
        
        with col_info:
            st.markdown(f"### Ticket: `{ticket_sel_mttr}`")
            st.write(f"**Técnico:** {info_ticket['tecnico'] or 'N/A'}")
            st.write(f"**Estado:** {info_ticket['estado']}")
            st.write(f"**Afectados Totales:** {info_ticket['clientes_afectados_totales'] or 0} clientes")
            st.write(f"**TTR Máximo Campo:** `{info_ticket['ttr_maximo_horas'] or 0.0} hrs`")
            st.write(f"**MTTR Ponderado Campo:** `{info_ticket['mttr_ponderado_horas'] or 0.0} hrs`")
            
            st.divider()
            st.markdown("#### 🌐 Cargar/Editar Registro VORTEX")
            vortex_actual = float(info_ticket['tiempo_vortex_horas']) if pd.notnull(info_ticket['tiempo_vortex_horas']) else 0.0
            
            with st.form(key=f"form_vortex_{ticket_sel_mttr}"):
                nuevo_vortex = st.number_input("Horas Offline VORTEX:", min_value=0.0, value=vortex_actual, step=0.1)
                btn_vortex = st.form_submit_button("Guardar VORTEX")
                
                if btn_vortex:
                    if actualizar_vortex_db(ticket_sel_mttr, nuevo_vortex, info_ticket['mttr_ponderado_horas']):
                        st.success("✅ VORTEX actualizado")
                        st.rerun()

            if pd.notnull(info_ticket['brecha_vortex_mttr']):
                st.info(f"**Brecha (VORTEX vs MTTR):** {info_ticket['brecha_vortex_mttr']} hrs")

        with col_chart:
            st.markdown("#### Curva de Recuperación (% Clientes Online)")
            df_rec = cargar_recuperaciones_ticket(ticket_sel_mttr)
            
            if not df_rec.empty:
                fig_curve = go.Figure()
                fig_curve.add_trace(go.Scatter(
                    x=df_rec['fecha_registro'], 
                    y=df_rec['porcentaje_online'],
                    mode='lines+markers',
                    line_shape='hv',
                    name='% Online',
                    fill='tozeroy',
                    line=dict(color='#2ecc71', width=3)
                ))
                
                fig_curve.update_layout(
                    xaxis_title="Fecha y Hora",
                    yaxis_title="% Clientes Restablecidos",
                    yaxis=dict(range=[0, 105]),
                    margin=dict(l=20, r=20, t=30, b=20)
                )
                st.plotly_chart(fig_curve, use_container_width=True)
            else:
                st.warning("Este ticket no cuenta aún con puntos intermedios de recuperación registrados en el bot.")

# TAB 3: VISOR DE EVIDENCIAS FOTOGRÁFICAS Y CONVERSACIÓN DE TELEGRAM
with tab_multimedia:
    st.subheader("🖼️ Galería de Evidencias e Historial del Ticket")
    
    if not df_tickets.empty:
        ticket_sel = st.selectbox("Selecciona el Ticket a revisar:", df_tickets['id_ticket'].unique(), key="sb_multimedia")
        
        col_evidencias, col_chat = st.columns([3, 2])
        
        # --- COLUMNA 1: GALERÍA DE FOTOS Y ARCHIVOS ---
        with col_evidencias:
            st.markdown(f"### 📷 Evidencias de Campo: `{ticket_sel}`")
            
            # Ruta construida con la variable que viene del .env
            carpeta_ticket = os.path.join(CARPETA_BASE_RESGUARDO, ticket_sel)
            
            # 1. BÚSQUEDA DIRECTA DE FOTOS EN DISCO
            extensiones_foto = ('.png', '.jpg', '.jpeg', '.webp')
            fotos_encontradas = []
            
            if os.path.exists(carpeta_ticket):
                for archivo in os.listdir(carpeta_ticket):
                    if archivo.lower().endswith(extensiones_foto):
                        fotos_encontradas.append(os.path.join(carpeta_ticket, archivo))

            # 2. RENDERIZADO DE FOTOS
            if fotos_encontradas:
                st.markdown(f"#### 🖼️ Fotografías Registradas ({len(fotos_encontradas)})")
                cols_fotos = st.columns(3)
                
                for idx, ruta_foto in enumerate(fotos_encontradas):
                    col_curr = cols_fotos[idx % 3]
                    nombre_archivo = os.path.basename(ruta_foto)
                    col_curr.image(
                        ruta_foto, 
                        caption=f"Foto #{idx+1} - {nombre_archivo}", 
                        width="stretch"
                    )
            else:
                st.warning(f"No se encontraron imágenes en: `{carpeta_ticket}`")

            st.divider()

            # 3. OTRAS EVIDENCIAS (Ubicaciones, Audios, Videos)
            df_ev = cargar_evidencias_ticket(ticket_sel)
            if not df_ev.empty:
                ubicaciones = df_ev[df_ev['tipo'] == 'UBICACION']
                audios = df_ev[df_ev['tipo'] == 'AUDIO']
                videos = df_ev[df_ev['tipo'] == 'VIDEO']

                # Renderizar Mapa de Ubicación
                if not ubicaciones.empty:
                    st.markdown("#### 📍 Ubicación GPS Registrada")
                    df_mapa = ubicaciones[['latitud', 'longitud']].dropna().rename(
                        columns={'latitud': 'lat', 'longitud': 'lon'}
                    )
                    if not df_mapa.empty:
                        st.map(df_mapa)

                # Renderizar Audios
                if not audios.empty:
                    st.markdown("#### 🎙️ Notas de Voz / Audios")
                    for _, row in audios.iterrows():
                        nombre_media = os.path.basename(str(row['ruta_nube']))
                        ruta_audio = os.path.join(carpeta_ticket, nombre_media)
                        if os.path.exists(ruta_audio):
                            st.audio(ruta_audio)

                # Renderizar Videos
                if not videos.empty:
                    st.markdown("#### 🎥 Videos Registrados")
                    for _, row in videos.iterrows():
                        nombre_media = os.path.basename(str(row['ruta_nube']))
                        ruta_video = os.path.join(carpeta_ticket, nombre_media)
                        if os.path.exists(ruta_video):
                            st.video(ruta_video)

        # --- COLUMNA 2: CHAT DE CONVERSACIÓN ---
        with col_chat:
            st.markdown(f"### 💬 Conversación / Chat del Hilo")
            df_chat = cargar_chat_ticket(ticket_sel)
            
            if not df_chat.empty:
                with st.container(height=500):
                    for _, msg in df_chat.iterrows():
                        autor = msg['emisor_nombre'] or "Técnico"
                        texto = msg['mensaje']
                        hora = msg['fecha_hora'].strftime("%d/%m %H:%M") if pd.notnull(msg['fecha_hora']) else ""
                        
                        with st.chat_message("user", avatar="👷‍♂️"):
                            st.markdown(f"**{autor}** `{hora}`")
                            st.write(texto)
            else:
                st.info("No hay historial de mensajes guardado para este ticket.")
