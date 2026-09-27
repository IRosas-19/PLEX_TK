import os
import sys
import pandas as pd
import plotly.express as px
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine

# ----------------------------------------------------
# 1. CONFIGURACIÓN DE PÁGINA Y RUTAS
# ----------------------------------------------------
st.set_page_config(
    page_title="Dashboard FTTH - Control de Tickets",
    page_icon="📊",
    layout="wide"
)

# Detectar ruta actual para localizar el .env de forma correcta
if getattr(sys, 'frozen', False):
    DIRECTORIO_BASE = os.path.dirname(sys.executable)
else:
    DIRECTORIO_BASE = os.path.dirname(os.path.abspath(__file__))

ruta_env = os.path.join(DIRECTORIO_BASE, '.env')
load_dotenv(dotenv_path=ruta_env)

# Lectura de variables de entorno
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "ftth_bot_db")
DB_PORT = os.getenv("DB_PORT", "3306")

# ----------------------------------------------------
# 2. CONEXIÓN A LA BASE DE DATOS
# ----------------------------------------------------
@st.cache_resource
def get_db_engine():
    # Cadena de conexión formateada correctamente para mysql.connector
    DATABASE_URL = f"mysql+mysqlconnector://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    return create_engine(DATABASE_URL)

try:
    engine = get_db_engine()
except Exception as e:
    st.error(f"❌ Error al conectar con la base de datos: {e}")
    st.stop()

# ----------------------------------------------------
# 3. FUNCIONES PARA CARGAR DATOS
# ----------------------------------------------------
def cargar_datos_tickets():
    query = """
        SELECT 
            t.id_ticket,
            tec.nombre AS tecnico,
            t.estado,
            t.diagnostico_cierre,
            t.fecha_creacion,
            t.fecha_cierre
        FROM tickets t
        LEFT JOIN tecnicos tec ON t.id_tecnico = tec.id_tecnico
        ORDER BY t.fecha_creacion DESC
    """
    return pd.read_sql(query, con=engine)

def cargar_datos_evidencias():
    query = """
        SELECT 
            e.id_evidencia,
            e.id_ticket,
            e.tipo,
            e.latitud,
            e.longitud,
            e.ruta_nube,
            e.fecha_registro
        FROM evidencias e
        ORDER BY e.fecha_registro DESC
    """
    return pd.read_sql(query, con=engine)

# ----------------------------------------------------
# 4. INTERFAZ Y PANEL VISUAL
# ----------------------------------------------------
st.title("📡 Panel de Control & Resguardo FTTH")
st.markdown("Monitoreo en tiempo real de tickets de soporte, evidencias y métricas de campo.")

# Botón para recargar datos manualmente
if st.button("🔄 Actualizar Datos"):
    st.rerun()

try:
    df_tickets = cargar_datos_tickets()
    df_evidencias = cargar_datos_evidencias()
except Exception as e:
    st.error(f"Error al ejecutar las consultas SQL: {e}")
    st.stop()

# --- MÉTRICAS GENERALES ---
col1, col2, col3, col4 = st.columns(4)

total_tickets = len(df_tickets)
tickets_en_proceso = len(df_tickets[df_tickets['estado'] == 'EN_PROCESO'])
tickets_esperando = len(df_tickets[df_tickets['estado'] == 'ESPERANDO_CIERRE'])
tickets_cerrados = len(df_tickets[df_tickets['estado'] == 'CERRADO'])

col1.metric("Total Tickets", total_tickets)
col2.metric("En Proceso 🛠️", tickets_en_proceso)
col3.metric("Por Cerrar 📝", tickets_esperando)
col4.metric("Cerrados ✅", tickets_cerrados)

st.divider()

# --- GRÁFICOS INTERACTIVOS ---
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
        st.plotly_chart(fig_estado, use_container_width=True)
    else:
        st.info("No hay registros de tickets aún.")

with col_graf2:
    st.subheader("👨‍🔧 Tickets por Técnico")
    if not df_tickets.empty and 'tecnico' in df_tickets.columns:
        df_tec = df_tickets['tecnico'].value_counts().reset_index()
        df_tec.columns = ['Técnico', 'Cantidad']
        fig_tec = px.bar(df_tec, x='Técnico', y='Cantidad', color='Cantidad', color_continuous_scale='Blues')
        st.plotly_chart(fig_tec, use_container_width=True)
    else:
        st.info("No hay datos de técnicos asignados.")

st.divider()

# --- TABLAS DE DATOS DETALLADAS ---
tab1, tab2 = st.columns(2)

with tab1:
    st.subheader("📋 Registro de Tickets")
    st.dataframe(df_tickets, use_container_width=True, hide_index=True)

with tab2:
    st.subheader("📁 Resguardo de Evidencias Registradas")
    st.dataframe(df_evidencias, use_container_width=True, hide_index=True)