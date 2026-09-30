import logging
import asyncio
import functools
import os
import sys
import re
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from contextlib import contextmanager
import mysql.connector
from dotenv import load_dotenv  # type: ignore
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.helpers import escape_markdown

# ----------------------------------------------------
# 1. CARGA DE CONFIGURACIÓN Y ENTORNO
# ----------------------------------------------------
if getattr(sys, 'frozen', False):
    DIRECTORIO_BASE = os.path.dirname(sys.executable)
else:
    DIRECTORIO_BASE = os.path.dirname(os.path.abspath(__file__))

ruta_env = os.path.join(DIRECTORIO_BASE, '.env')
load_dotenv(dotenv_path=ruta_env)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# Carga de lista blanca de usuarios autorizados (admins/supervisores)
raw_admin_ids = os.getenv("ALLOWED_ADMIN_IDS", "")
ALLOWED_ADMIN_IDS = [int(x.strip()) for x in raw_admin_ids.split(",") if x.strip().isdigit()]

DB_CONFIG = {
    'host': os.getenv("DB_HOST", "localhost"),
    'user': os.getenv("DB_USER", "root"),
    'password': os.getenv("DB_PASSWORD", ""),
    'database': os.getenv("DB_NAME", "ftth_bot_db"),
    'port': int(os.getenv("DB_PORT", 3306))
}

CARPETA_BASE_RESGUARDO = os.path.join(os.getcwd(), "Resguardo_FTTH")

if not TELEGRAM_TOKEN:
    raise ValueError("❌ Error: No se encontró TELEGRAM_TOKEN en el archivo .env")

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

def get_db_connection():
    return mysql.connector.connect(**DB_CONFIG)

@contextmanager
def db_cursor(dictionary=False):
    """Gestor de contexto para manejo automatizado de cursores, transacciones y conexiones."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=dictionary)
    try:
        yield cursor
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()

async def run_sync(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))

def md(texto):
    if texto is None:
        return ""
    return escape_markdown(str(texto), version=1)

def obtener_o_crear_carpeta_ticket(ticket_id):
    ruta_ticket = os.path.join(CARPETA_BASE_RESGUARDO, ticket_id)
    if not os.path.exists(ruta_ticket):
        os.makedirs(ruta_ticket)
    return ruta_ticket

# ----------------------------------------------------
# 2. MÓDULO DE CÁLCULO DE MTTR PONDERADO
# ----------------------------------------------------
def _calcular_mttr_ponderado_sync(ticket_id):
    """Calcula el MTTR Ponderado integrando los intervalos de clientes fuera de servicio."""
    with db_cursor(dictionary=True) as cursor:
        cursor.execute(
            """SELECT clientes_afectados_totales, time_cuadrilla_sitio, time_fin_afectacion 
               FROM mttr_eventos WHERE id_ticket = %s""",
            (ticket_id,)
        )
        evento = cursor.fetchone()

        if not evento or not evento['time_cuadrilla_sitio'] or not evento['time_fin_afectacion']:
            return 0.0, 0.0

        t_inicio = evento['time_cuadrilla_sitio']
        t_fin = evento['time_fin_afectacion']
        totales = evento['clientes_afectados_totales'] or 0

        # TTR Máximo en horas
        ttr_maximo = (t_fin - t_inicio).total_seconds() / 3600.0

        if totales == 0:
            return round(ttr_maximo, 2), round(ttr_maximo, 2)

        # Recuperaciones intermedias ordenadas por tiempo
        cursor.execute(
            """SELECT porcentaje_online, fecha_registro 
               FROM mttr_recuperaciones 
               WHERE id_ticket = %s 
               ORDER BY fecha_registro ASC""",
            (ticket_id,)
        )
        recuperaciones = cursor.fetchall()

        # Construcción de intervalos (t_inicio, %_offline)
        puntos = [(t_inicio, 100.0)]
        for r in recuperaciones:
            puntos.append((r['fecha_registro'], 100.0 - float(r['porcentaje_online'])))
        puntos.append((t_fin, 0.0))

        area_horas_pct = 0.0
        for i in range(len(puntos) - 1):
            t_actual, pct_offline = puntos[i]
            t_siguiente, _ = puntos[i+1]
            duracion_intervalo = (t_siguiente - t_actual).total_seconds() / 3600.0
            if duracion_intervalo > 0:
                area_horas_pct += (pct_offline / 100.0) * duracion_intervalo

        mttr_ponderado = area_horas_pct

        # Guardar resultados consolidados en MySQL
        cursor.execute(
            """UPDATE mttr_eventos 
               SET mttr_ponderado_horas = %s, ttr_maximo_horas = %s 
               WHERE id_ticket = %s""",
            (mttr_ponderado, ttr_maximo, ticket_id)
        )

        return round(mttr_ponderado, 2), round(ttr_maximo, 2)

def _procesar_palabras_clave_mttr_sync(ticket_id, user_id, texto_mensaje):
    """Evalúa si el mensaje contiene palabras clave y actualiza las tablas MTTR."""
    if ALLOWED_ADMIN_IDS and user_id not in ALLOWED_ADMIN_IDS:
        return None  

    texto_lower = texto_mensaje.strip().lower()

    # Regex para buscar métricas
    match_afectados = re.search(r"clientes\s+afectados\s+totales\s*:\s*(\d+)", texto_lower)
    match_online = re.search(r"%\s*de\s+clientes\s+online\s*:\s*(\d+(?:\.\d+)?)%?", texto_lower)

    # Identificación previa sin abrir transacciones
    accion = None
    valor_extra = None

    if "corte de fo" in texto_lower:
        accion = "CORTE_FO"
    elif "cuadrilla en sitio" in texto_lower:
        accion = "SITIO"
    elif "corte encontrado" in texto_lower:
        accion = "ENCONTRADO"
    elif "inicio de fusiones" in texto_lower:
        accion = "FUSIONES"
    elif match_afectados:
        totales = int(match_afectados.group(1))
        if totales < 0:
            return "⚠️ *Error:* El número de clientes afectados no puede ser negativo."
        accion = "AFECTADOS"
        valor_extra = totales
    elif match_online:
        porcentaje = float(match_online.group(1))
        if not (0.0 <= porcentaje <= 100.0):
            return "⚠️ *Error:* El porcentaje de clientes online debe estar entre `0%` y `100%`."
        accion = "ONLINE"
        valor_extra = porcentaje
    elif "fin de afectación" in texto_lower:
        accion = "FIN"

    if not accion:
        return None

    # Escritura solo cuando existe coincidencia validada
    with db_cursor(dictionary=True) as cursor:
        cursor.execute("INSERT IGNORE INTO mttr_eventos (id_ticket) VALUES (%s)", (ticket_id,))

        if accion == "CORTE_FO":
            cursor.execute("UPDATE mttr_eventos SET es_corte_fo = TRUE WHERE id_ticket = %s", (ticket_id,))
            return "🌐 *Evento clasificado como Corte de Fibra Óptica.*"

        elif accion == "SITIO":
            cursor.execute("UPDATE mttr_eventos SET time_cuadrilla_sitio = NOW() WHERE id_ticket = %s", (ticket_id,))
            return "⏱️ *Marca de tiempo registrada:* Cuadrilla en sitio."

        elif accion == "ENCONTRADO":
            cursor.execute("UPDATE mttr_eventos SET time_corte_encontrado = NOW() WHERE id_ticket = %s", (ticket_id,))
            return "🔍 *Marca de tiempo registrada:* Corte de FO localizado."

        elif accion == "FUSIONES":
            cursor.execute("UPDATE mttr_eventos SET time_inicio_fusiones = NOW() WHERE id_ticket = %s", (ticket_id,))
            return "🔌 *Marca de tiempo registrada:* Inicio de fusiones."

        elif accion == "AFECTADOS":
            cursor.execute("UPDATE mttr_eventos SET clientes_afectados_totales = %s WHERE id_ticket = %s", (valor_extra, ticket_id))
            return f"👥 *Universo de clientes afectados registrado:* `{valor_extra}`"

        elif accion == "ONLINE":
            porcentaje = valor_extra
            cursor.execute("SELECT clientes_afectados_totales FROM mttr_eventos WHERE id_ticket = %s", (ticket_id,))
            res = cursor.fetchone()
            totales = res['clientes_afectados_totales'] if res else 0
            clientes_recuperados = int((porcentaje / 100.0) * totales)

            cursor.execute(
                """INSERT INTO mttr_recuperaciones (id_ticket, porcentaje_online, clientes_online, fecha_registro) 
                   VALUES (%s, %s, %s, NOW())""",
                (ticket_id, porcentaje, clientes_recuperados)
            )
            return f"📈 *Avance registrado:* `{porcentaje}%` online ({clientes_recuperados}/{totales} clientes)."

        elif accion == "FIN":
            cursor.execute("UPDATE mttr_eventos SET time_fin_afectacion = NOW() WHERE id_ticket = %s", (ticket_id,))
            mttr_ponderado, ttr_maximo = _calcular_mttr_ponderado_sync(ticket_id)

            return (
                f"🏁 *Fin de afectación registrado.*\n\n"
                f"📊 *RESUMEN DE MÉTRICAS DE REPARACIÓN:*\n"
                f"• *TTR Máximo (Duración total):* `{ttr_maximo} hrs`\n"
                f"• *MTTR Ponderado:* `{mttr_ponderado} hrs/cliente`"
            )

    return None

# ----------------------------------------------------
# 3. COMANDOS PRINCIPALES
# ----------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(f"¡Hola {user.first_name}! Sistema FTTH activo con módulo MTTR.", parse_mode="Markdown")

def formatear_duracion(segundos):
    if segundos is None:
        return "No disponible"
    segundos = int(segundos)
    horas, resto = divmod(segundos, 3600)
    minutos, _ = divmod(resto, 60)
    return f"{horas} h {minutos} min" if horas > 0 else f"{minutos} min"

def _consultar_ticket_sync(ticket_id):
    with db_cursor(dictionary=True) as cursor:
        cursor.execute(
            """SELECT t.id_ticket, t.estado, t.fecha_inicio, t.fecha_cierre, t.diagnostico_cierre,
                      te.nombre AS tecnico, TIMESTAMPDIFF(SECOND, t.fecha_inicio, COALESCE(t.fecha_cierre, NOW())) AS duracion_segundos
               FROM tickets t LEFT JOIN tecnicos te ON te.id_tecnico = t.id_tecnico WHERE t.id_ticket = %s""",
            (ticket_id,)
        )
        ticket = cursor.fetchone()
        if not ticket: return None

        cursor.execute("SELECT COUNT(*) AS total FROM evidencias WHERE id_ticket = %s AND tipo = 'FOTO'", (ticket_id,))
        ticket["fotos"] = cursor.fetchone()["total"]

        cursor.execute("SELECT COUNT(*) AS total FROM evidencias WHERE id_ticket = %s AND tipo = 'UBICACION'", (ticket_id,))
        ticket["ubicaciones"] = cursor.fetchone()["total"]

        cursor.execute("SELECT mttr_ponderado_horas, ttr_maximo_horas FROM mttr_eventos WHERE id_ticket = %s", (ticket_id,))
        mttr_info = cursor.fetchone()
        ticket["mttr"] = mttr_info if mttr_info else None

        return ticket

async def consultar_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("⚠️ Indica el ID del ticket. Ejemplo: `/consultar TK-101`", parse_mode="Markdown")
        return

    ticket_id = context.args[0].upper()
    try:
        ticket = await run_sync(_consultar_ticket_sync, ticket_id)
        if not ticket:
            await update.message.reply_text(f"🔎 No se encontró el ticket `{md(ticket_id)}`.", parse_mode="Markdown")
            return

        fecha_inicio = ticket["fecha_inicio"].strftime("%d/%m/%Y %H:%M") if ticket["fecha_inicio"] else "No registrada"
        fecha_cierre = ticket["fecha_cierre"].strftime("%d/%m/%Y %H:%M") if ticket["fecha_cierre"] else "Pendiente"

        respuesta = (
            f"🔎 *CONSULTA DE TICKET*\n\n"
            f"🎫 *Ticket:* `{md(ticket['id_ticket'])}`\n"
            f"👷 *Técnico:* {md(ticket['tecnico']) or 'No asignado'}\n"
            f"📌 *Estado:* {md(ticket['estado'])}\n\n"
            f"🕐 *Inicio:* {fecha_inicio}\n"
            f"🏁 *Fin:* {fecha_cierre}\n"
            f"⏱️ *Duración:* {formatear_duracion(ticket['duracion_segundos'])}\n\n"
            f"📸 *Fotografías:* {ticket['fotos']}\n"
            f"📍 *Ubicaciones:* {ticket['ubicaciones']}\n"
        )

        if ticket["mttr"] and ticket["mttr"]["mttr_ponderado_horas"] is not None:
            respuesta += (
                f"\n📊 *MÉTRICAS MTTR:*\n"
                f"• MTTR Ponderado: `{ticket['mttr']['mttr_ponderado_horas']} hrs`\n"
                f"• TTR Máximo: `{ticket['mttr']['ttr_maximo_horas']} hrs`\n"
            )

        if ticket["diagnostico_cierre"]:
            respuesta += f"\n📝 *Solución registrada:*\n{md(ticket['diagnostico_cierre'])}"

        await update.message.reply_text(respuesta, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Error consultando ticket {ticket_id}: {e}")
        await update.message.reply_text("❌ No se pudo consultar el ticket.")

def _obtener_estado_ticket_sync(ticket_id):
    with db_cursor(dictionary=True) as cursor:
        cursor.execute("SELECT estado, telegram_topic_id FROM tickets WHERE id_ticket = %s", (ticket_id,))
        return cursor.fetchone()

def _registrar_inicio_ticket_sync(ticket_id, topic_id, telegram_user_id, nombre_tecnico):
    with db_cursor() as cursor:
        cursor.execute("INSERT INTO tecnicos (telegram_user_id, nombre) VALUES (%s, %s) ON DUPLICATE KEY UPDATE nombre=%s", (telegram_user_id, nombre_tecnico, nombre_tecnico))
        cursor.execute("SELECT id_tecnico FROM tecnicos WHERE telegram_user_id = %s", (telegram_user_id,))
        id_tecnico = cursor.fetchone()[0]

        cursor.execute(
            """INSERT INTO tickets (id_ticket, id_tecnico, telegram_topic_id, estado, fecha_inicio, fecha_cierre, diagnostico_cierre)
               VALUES (%s, %s, %s, 'EN_PROCESO', NOW(), NULL, NULL)
               ON DUPLICATE KEY UPDATE id_tecnico=%s, telegram_topic_id=%s, estado='EN_PROCESO', fecha_inicio=NOW(), fecha_cierre=NULL, diagnostico_cierre=NULL""",
            (ticket_id, id_tecnico, topic_id, id_tecnico, topic_id)
        )
        cursor.execute("INSERT IGNORE INTO mttr_eventos (id_ticket) VALUES (%s)", (ticket_id,))
        return id_tecnico

async def inicio_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    if not context.args:
        await update.message.reply_text("⚠️ Indica el ID del ticket. Ejemplo: `/inicio TK-101`", parse_mode="Markdown")
        return

    ticket_id = context.args[0].upper()

    try:
        ticket_existente = await run_sync(_obtener_estado_ticket_sync, ticket_id)
        if ticket_existente and ticket_existente["estado"] in ("EN_PROCESO", "ESPERANDO_CIERRE") and ticket_existente["telegram_topic_id"]:
            await update.message.reply_text(
                f"⚠️ El ticket `{md(ticket_id)}` ya está en curso en otro hilo.\n"
                "Usa ese hilo para continuar, o ejecuta `/cerrar` ahí antes de reiniciarlo.",
                parse_mode="Markdown"
            )
            return

        obtener_o_crear_carpeta_ticket(ticket_id)
        topic_name = f"[{ticket_id}] - {user.first_name}"
        topic = await context.bot.create_forum_topic(chat_id=chat_id, name=topic_name)
        topic_id = topic.message_thread_id

        await run_sync(_registrar_inicio_ticket_sync, ticket_id, topic_id, user.id, user.full_name)

        await context.bot.send_message(
            chat_id=chat_id,
            message_thread_id=topic_id,
            text=(
                f"📌 *Ticket {md(ticket_id)} iniciado*\n"
                "Puedes enviar ubicaciones, imágenes, documentos, videos o audios.\n\n"
                "Al finalizar escribe `/cerrar`."
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logging.error(f"Error al iniciar ticket: {e}")
        await update.message.reply_text("❌ Error al iniciar el ticket.")

def _vincular_ticket_sync(ticket_id, thread_id):
    with db_cursor() as cursor:
        cursor.execute("UPDATE tickets SET telegram_topic_id = %s WHERE id_ticket = %s AND estado IN ('EN_PROCESO', 'ESPERANDO_CIERRE')", (thread_id, ticket_id))
        return cursor.rowcount

async def vincular_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    thread_id = msg.message_thread_id

    if not thread_id:
        await msg.reply_text("⚠️ `/vincular` debe ejecutarse dentro del hilo del ticket.", parse_mode="Markdown")
        return

    if not context.args:
        await msg.reply_text("⚠️ Indica el ID del ticket a vincular. Ejemplo: `/vincular TK-101`", parse_mode="Markdown")
        return

    ticket_id = context.args[0].upper()
    try:
        filas_afectadas = await run_sync(_vincular_ticket_sync, ticket_id, thread_id)
        if not filas_afectadas:
            await msg.reply_text(f"⚠️ No existe el ticket `{md(ticket_id)}` en la base de datos.", parse_mode="Markdown")
            return
        await msg.reply_text(f"🔄 Hilo reconectado exitosamente con el ticket `{md(ticket_id)}`.", parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Error al vincular ticket: {e}")
        await msg.reply_text("❌ No se pudo vincular el ticket.")

def _solicitar_cerrar_ticket_sync(thread_id):
    with db_cursor(dictionary=True) as cursor:
        cursor.execute("SELECT id_ticket FROM tickets WHERE telegram_topic_id = %s AND estado IN ('EN_PROCESO', 'ESPERANDO_CIERRE')", (thread_id,))
        ticket = cursor.fetchone()
        if not ticket: return None

        ticket_id = ticket['id_ticket']
        cursor.execute("UPDATE tickets SET estado = 'ESPERANDO_CIERRE' WHERE id_ticket = %s", (ticket_id,))
        return ticket_id

async def solicitar_cerrar_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    thread_id = msg.message_thread_id

    if not thread_id:
        await msg.reply_text("⚠️ `/cerrar` debe ejecutarse dentro del hilo del ticket.", parse_mode="Markdown")
        return

    try:
        ticket_id = await run_sync(_solicitar_cerrar_ticket_sync, thread_id)
    except Exception as e:
        logging.error(f"Error al solicitar cierre de ticket: {e}")
        await msg.reply_text("❌ No se pudo procesar la solicitud de cierre.")
        return

    if not ticket_id:
        await msg.reply_text("⚠️ No hay un ticket activo asociado a este hilo.")
        return

    await msg.reply_text(
        f"📝 *Resumen de Solución ({md(ticket_id)})\n\n"
        "Por favor escribe en un mensaje una **breve explicación del trabajo realizado** para concluir el ticket."
    )

MAX_DESCARGAS_SIMULTANEAS = 3
SEM_DESCARGAS = asyncio.Semaphore(MAX_DESCARGAS_SIMULTANEAS)

async def descargar_archivo_con_reintentos(bot, file_id, ruta_local, ticket_id, intentos=3):
    ultimo_error = None
    esperas = (0, 2, 5)

    for intento in range(1, intentos + 1):
        if esperas[intento - 1]:
            await asyncio.sleep(esperas[intento - 1])

        try:
            async with SEM_DESCARGAS:
                logging.info(f"📥 [{ticket_id}] Descargando evidencia (intento {intento}/{intentos})")
                archivo = await bot.get_file(file_id, read_timeout=45, connect_timeout=20, pool_timeout=20)
                await archivo.download_to_drive(custom_path=ruta_local, read_timeout=45, connect_timeout=20, pool_timeout=20)

            logging.info(f"✅ [{ticket_id}] Evidencia descargada correctamente")
            return True
        except Exception as e:
            ultimo_error = e
            logging.warning(f"⚠️ [{ticket_id}] Falló intento {intento}/{intentos}: {e}")
            if os.path.exists(ruta_local):
                try: os.remove(ruta_local)
                except Exception: pass

    logging.error(f"❌ [{ticket_id}] Evidencia falló después de {intentos} intentos: {ultimo_error}")
    return False

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logging.error("❌ Excepción no controlada en el bot", exc_info=context.error)


# Solcitud de materialess

async def abrir_bot_materiales(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Sustituye 'MaterialesAlmacen_bot' por el alias real de tu nuevo bot
    url_bot_materiales = "https://t.me/Materiales_Zooy_bot?start"
    
    keyboard = [
        [InlineKeyboardButton("📦 Abrir Solicitud de Materiales", url=url_bot_materiales)]
    ]

    await update.message.reply_text(
        "Haz clic en el botón de abajo para ir al sistema de **Solicitud de Materiales**:",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# ----------------------------------------------------
# 4. MANEJO DE ARCHIVOS Y MENSAJES (MÚLTIPLES TIPOS)
# ----------------------------------------------------
def _buscar_ticket_activo_por_topic_sync(thread_id):
    with db_cursor(dictionary=True) as cursor:
        cursor.execute("SELECT id_ticket, estado FROM tickets WHERE telegram_topic_id = %s AND estado IN ('EN_PROCESO', 'ESPERANDO_CIERRE')", (thread_id,))
        return cursor.fetchone()

def _finalizar_cierre_ticket_sync(ticket_id, diagnostico):
    with db_cursor(dictionary=True) as cursor:
        cursor.execute("SELECT emisor_nombre, mensaje, fecha_hora FROM historial_chat WHERE id_ticket = %s ORDER BY fecha_hora ASC", (ticket_id,))
        mensajes = cursor.fetchall()
        cursor.execute("UPDATE tickets SET estado = 'CERRADO', diagnostico_cierre = %s, fecha_cierre = NOW() WHERE id_ticket = %s", (diagnostico, ticket_id))
        return mensajes

def _escribir_historial_cierre(ruta_carpeta, ticket_id, diagnostico, mensajes):
    archivo_historial = os.path.join(ruta_carpeta, f"Historial_Chat_{ticket_id}.txt")
    with open(archivo_historial, "w", encoding="utf-8") as f:
        f.write("==================================================\n")
        f.write(f"     INFORME DE CIERRE - TICKET: {ticket_id}\n")
        f.write("==================================================\n")
        f.write(f"DIAGNÓSTICO / SOLUCIÓN FINAL:\n{diagnostico}\n")
        f.write("==================================================\n\n")
        f.write("--- HISTORIAL DE CONVERSACIÓN ---\n")
        for m in mensajes:
            f.write(f"[{m['fecha_hora']}] {m['emisor_nombre']}:\n{m['mensaje']}\n\n")

def _registrar_evidencia_sync(ticket_id, tipo, ruta_nube=None, latitud=None, longitud=None):
    with db_cursor() as cursor:
        if tipo == 'UBICACION':
            cursor.execute("INSERT INTO evidencias (id_ticket, tipo, latitud, longitud) VALUES (%s, %s, %s, %s)", (ticket_id, tipo, latitud, longitud))
        else:
            cursor.execute("INSERT INTO evidencias (id_ticket, tipo, ruta_nube) VALUES (%s, %s, %s)", (ticket_id, tipo, ruta_nube))

def _contar_fotos_sync(ticket_id):
    with db_cursor(dictionary=True) as cursor:
        cursor.execute("SELECT COUNT(*) as total FROM evidencias WHERE id_ticket = %s AND tipo = 'FOTO'", (ticket_id,))
        return cursor.fetchone()['total']

def _registrar_mensaje_chat_sync(ticket_id, emisor_id, emisor_nombre, mensaje):
    with db_cursor() as cursor:
        cursor.execute("INSERT INTO historial_chat (id_ticket, emisor_id, emisor_nombre, mensaje) VALUES (%s, %s, %s, %s)", (ticket_id, emisor_id, emisor_nombre, mensaje))

async def guardar_evidencia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.message_thread_id:
        return

    thread_id = msg.message_thread_id
    try:
        ticket = await run_sync(_buscar_ticket_activo_por_topic_sync, thread_id)
    except Exception as e:
        logging.error(f"Error buscando ticket activo para el topic {thread_id}: {e}")
        return

    if not ticket:
        return

    ticket_id = ticket['id_ticket']
    estado_actual = ticket['estado']
    ruta_carpeta = obtener_o_crear_carpeta_ticket(ticket_id)

    # A) FINALIZACIÓN DE CIERRE TRAS /cerrar
    if estado_actual == 'ESPERANDO_CIERRE' and msg.text and not msg.text.startswith('/'):
        diagnostico = msg.text
        try:
            mensajes = await run_sync(_finalizar_cierre_ticket_sync, ticket_id, diagnostico)
            await run_sync(_escribir_historial_cierre, ruta_carpeta, ticket_id, diagnostico, mensajes)
        except Exception as e:
            logging.error(f"Error cerrando ticket {ticket_id}: {e}")
            await msg.reply_text("❌ No se pudo cerrar el ticket.")
            return

        await msg.reply_text(
            f"✅ *Ticket {md(ticket_id)} cerrado oficialmente.*\n"
            f"📋 **Diagnóstico:** _{md(diagnostico)}_",
            parse_mode="Markdown"
        )
        try:
            await context.bot.close_forum_topic(chat_id=msg.chat_id, message_thread_id=thread_id)
        except Exception as e:
            logging.warning(f"No se pudo archivar el hilo: {e}")
        return


    # B) PROCESAMIENTO DE EVIDENCIAS Y MENSAJES MTTR
    try:
        if msg.location:
            lat, lon = msg.location.latitude, msg.location.longitude
            await run_sync(_registrar_evidencia_sync, ticket_id, 'UBICACION', None, lat, lon)
            txt_gps = os.path.join(ruta_carpeta, "ubicacion_gps.txt")
            with open(txt_gps, "a", encoding="utf-8") as f:
                f.write(f"Fecha: {msg.date} | Latitud: {lat}, Longitud: {lon} | Maps: http://maps.google.com/?q={lat},{lon}\n")

        elif msg.photo:
            file_id = msg.photo[-1].file_id
            num_foto = await run_sync(_contar_fotos_sync, ticket_id) + 1
            ruta_local = os.path.join(ruta_carpeta, f"foto_{num_foto:02d}.jpg")
            if await descargar_archivo_con_reintentos(context.bot, file_id, ruta_local, ticket_id):
                await run_sync(_registrar_evidencia_sync, ticket_id, 'FOTO', ruta_local)

        elif msg.video:
            video = msg.video
            ruta_local = os.path.join(ruta_carpeta, f"video_{video.file_unique_id}.mp4")
            if await descargar_archivo_con_reintentos(context.bot, video.file_id, ruta_local, ticket_id):
                await run_sync(_registrar_evidencia_sync, ticket_id, 'VIDEO', ruta_local)

        elif msg.document:
            doc = msg.document
            ruta_local = os.path.join(ruta_carpeta, doc.file_name or f"doc_{doc.file_unique_id}")
            if await descargar_archivo_con_reintentos(context.bot, doc.file_id, ruta_local, ticket_id):
                await run_sync(_registrar_evidencia_sync, ticket_id, 'DOCUMENTO', ruta_local)

        elif msg.voice or msg.audio:
            audio_obj = msg.voice or msg.audio
            ruta_local = os.path.join(ruta_carpeta, f"audio_{audio_obj.file_unique_id}.ogg")
            if await descargar_archivo_con_reintentos(context.bot, audio_obj.file_id, ruta_local, ticket_id):
                await run_sync(_registrar_evidencia_sync, ticket_id, 'AUDIO', ruta_local)

        elif msg.text and not msg.text.startswith('/'):
            # 1. Registrar mensaje en el historial general
            await run_sync(_registrar_mensaje_chat_sync, ticket_id, msg.from_user.id, msg.from_user.full_name, msg.text)

            # 2. Evaluar palabras clave para MTTR
            respuesta_mttr = await run_sync(_procesar_palabras_clave_mttr_sync, ticket_id, msg.from_user.id, msg.text)

            # Responder si hubo coincidencia con palabra clave
            if respuesta_mttr:
                await msg.reply_text(respuesta_mttr, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Error resguardando evidencia/mensaje en ticket {ticket_id}: {e}")

# ----------------------------------------------------
# 5. ARRANQUE DEL BOT
# ----------------------------------------------------
if __name__ == '__main__':
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .read_timeout(45)
        .connect_timeout(20)
        .pool_timeout(20)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("inicio", inicio_ticket))
    app.add_handler(CommandHandler("cerrar", solicitar_cerrar_ticket))
    app.add_handler(CommandHandler("vincular", vincular_ticket))
    app.add_handler(CommandHandler("consultar", consultar_ticket))
    app.add_handler(CommandHandler("materiales", abrir_bot_materiales))

    app.add_handler(MessageHandler(
        filters.LOCATION | filters.PHOTO | filters.Document.ALL | filters.VOICE | filters.AUDIO | filters.VIDEO | filters.TEXT,
        guardar_evidencia
    ))

    app.add_error_handler(error_handler)

    print("🚀 Bot MTTR iniciado con éxito. Escuchando eventos y palabras clave...")
    app.run_polling()