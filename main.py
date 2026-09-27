import logging
import asyncio
import functools
import os
import sys
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
# 1. CARGA DE CONFIGURACIÓN Y SILENCIADO DE LOGS HTTP
# ----------------------------------------------------
if getattr(sys, 'frozen', False):
    DIRECTORIO_BASE = os.path.dirname(sys.executable)
else:
    DIRECTORIO_BASE = os.path.dirname(os.path.abspath(__file__))

ruta_env = os.path.join(DIRECTORIO_BASE, '.env')
load_dotenv(dotenv_path=ruta_env)

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

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

# Configuración del Logger General
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# SOLUCIÓN PROBLEMA 3: Silenciar solicitudes HTTP constantes en terminal
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

def get_db_connection():
    return mysql.connector.connect(**DB_CONFIG)


# SOLUCIÓN PROBLEMA 4: mysql.connector es síncrono/bloqueante. run_sync() ejecuta
# esas llamadas en un hilo aparte (executor) para no congelar el event loop de
# asyncio mientras se espera a la base de datos.
async def run_sync(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))


# SOLUCIÓN PROBLEMA 3: escapa texto proveniente del usuario (diagnósticos, nombres,
# IDs de ticket) antes de insertarlo en mensajes con parse_mode="Markdown", para
# evitar que caracteres como *, _ o ` rompan el formato o hagan fallar el envío.
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
# 2. COMANDOS (/start, /inicio, /vincular)
# ----------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await update.message.reply_text(
        f"¡Hola {user.first_name}! Sistema FTTH activo.",
        parse_mode="Markdown"
    )

def formatear_duracion(segundos):
    if segundos is None:
        return "No disponible"

    segundos = int(segundos)
    horas, resto = divmod(segundos, 3600)
    minutos, _ = divmod(resto, 60)

    if horas > 0:
        return f"{horas} h {minutos} min"
    return f"{minutos} min"


def _consultar_ticket_sync(ticket_id):
    """Todo el acceso a MySQL de /consultar en una sola función síncrona,
    ejecutada fuera del event loop vía run_sync()."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            """
            SELECT
                t.id_ticket,
                t.estado,
                t.fecha_inicio,
                t.fecha_cierre,
                t.diagnostico_cierre,
                te.nombre AS tecnico,
                TIMESTAMPDIFF(SECOND, t.fecha_inicio, COALESCE(t.fecha_cierre, NOW())) AS duracion_segundos
            FROM tickets t
            LEFT JOIN tecnicos te ON te.id_tecnico = t.id_tecnico
            WHERE t.id_ticket = %s
            """,
            (ticket_id,)
        )
        ticket = cursor.fetchone()

        if not ticket:
            return None

        cursor.execute(
            "SELECT COUNT(*) AS total FROM evidencias WHERE id_ticket = %s AND tipo = 'FOTO'",
            (ticket_id,)
        )
        ticket["fotos"] = cursor.fetchone()["total"]

        cursor.execute(
            "SELECT COUNT(*) AS total FROM evidencias WHERE id_ticket = %s AND tipo = 'UBICACION'",
            (ticket_id,)
        )
        ticket["ubicaciones"] = cursor.fetchone()["total"]

        return ticket
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


async def consultar_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "⚠️ Indica el ID del ticket. Ejemplo: `/consultar TK-101`",
            parse_mode="Markdown"
        )
        return

    ticket_id = context.args[0].upper()

    try:
        ticket = await run_sync(_consultar_ticket_sync, ticket_id)

        if not ticket:
            await update.message.reply_text(
                f"🔎 No se encontró el ticket `{md(ticket_id)}`.",
                parse_mode="Markdown"
            )
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
            f"⏱️ *{'Tiempo de reparación' if ticket['fecha_cierre'] else 'Tiempo transcurrido'}:* {formatear_duracion(ticket['duracion_segundos'])}\n\n"
            f"📸 *Fotografías:* {ticket['fotos']}\n"
            f"📍 *Ubicaciones:* {ticket['ubicaciones']}\n"
        )

        if ticket["diagnostico_cierre"]:
            respuesta += f"\n📝 *Solución registrada:*\n{md(ticket['diagnostico_cierre'])}"

        await update.message.reply_text(respuesta, parse_mode="Markdown")

    except Exception as e:
        logging.error(f"Error consultando ticket {ticket_id}: {e}")
        await update.message.reply_text("❌ No se pudo consultar el ticket.")


def _obtener_estado_ticket_sync(ticket_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT estado, telegram_topic_id FROM tickets WHERE id_ticket = %s",
            (ticket_id,)
        )
        return cursor.fetchone()
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def _registrar_inicio_ticket_sync(ticket_id, topic_id, telegram_user_id, nombre_tecnico):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            "INSERT INTO tecnicos (telegram_user_id, nombre) VALUES (%s, %s) ON DUPLICATE KEY UPDATE nombre=%s",
            (telegram_user_id, nombre_tecnico, nombre_tecnico)
        )

        cursor.execute("SELECT id_tecnico FROM tecnicos WHERE telegram_user_id = %s", (telegram_user_id,))
        id_tecnico = cursor.fetchone()[0]

        cursor.execute(
            """
            INSERT INTO tickets
                (id_ticket, id_tecnico, telegram_topic_id, estado, fecha_inicio, fecha_cierre, diagnostico_cierre)
            VALUES
                (%s, %s, %s, 'EN_PROCESO', NOW(), NULL, NULL)
            ON DUPLICATE KEY UPDATE
                id_tecnico=%s,
                telegram_topic_id=%s,
                estado='EN_PROCESO',
                fecha_inicio=NOW(),
                fecha_cierre=NULL,
                diagnostico_cierre=NULL
            """,
            (ticket_id, id_tecnico, topic_id, id_tecnico, topic_id)
        )
        conn.commit()
        return id_tecnico
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


async def inicio_ticket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user = update.effective_user

    if not context.args:
        await update.message.reply_text("⚠️ Indica el ID del ticket. Ejemplo: `/inicio TK-101`", parse_mode="Markdown")
        return

    ticket_id = context.args[0].upper()

    try:
        # SOLUCIÓN PROBLEMA 5: si el ticket ya tiene un hilo activo, no se crea uno
        # nuevo (evita topics duplicados en Telegram cuando /inicio se llama dos veces).
        ticket_existente = await run_sync(_obtener_estado_ticket_sync, ticket_id)

        if (
            ticket_existente
            and ticket_existente["estado"] in ("EN_PROCESO", "ESPERANDO_CIERRE")
            and ticket_existente["telegram_topic_id"]
        ):
            await update.message.reply_text(
                f"⚠️ El ticket `{md(ticket_id)}` ya está en curso en otro hilo "
                f"(estado: {md(ticket_existente['estado'])}).\n"
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
                "Puedes enviar ubicaciones, imágenes, documentos, videos, audios o notas.\n\n"
                "Al finalizar escribe `/cerrar`."
            ),
            parse_mode="Markdown"
        )

    except Exception as e:
        logging.error(f"Error al iniciar ticket: {e}")
        await update.message.reply_text("❌ Error al iniciar el ticket.")

# SOLUCIÓN PROBLEMA 1: Comando para recuperar o vincular manualmente un hilo existente tras caída
def _vincular_ticket_sync(ticket_id, thread_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE tickets
            SET telegram_topic_id = %s
            WHERE id_ticket = %s
              AND estado IN ('EN_PROCESO', 'ESPERANDO_CIERRE')
            """,
            (thread_id, ticket_id)
        )

        conn.commit()
        return cursor.rowcount
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


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
        await msg.reply_text("❌ No se pudo vincular el ticket. Revisa si el ID existe en la BD.")

# ----------------------------------------------------
# 3. SOLICITUD DE CIERRE (/cerrar)
# ----------------------------------------------------
def _solicitar_cerrar_ticket_sync(thread_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        # Permite cerrar reconociendo el ID de hilo sin perder estado al reiniciar el bot
        cursor.execute(
            "SELECT id_ticket FROM tickets WHERE telegram_topic_id = %s AND estado IN ('EN_PROCESO', 'ESPERANDO_CIERRE')",
            (thread_id,)
        )
        ticket = cursor.fetchone()

        if not ticket:
            return None

        ticket_id = ticket['id_ticket']
        cursor.execute("UPDATE tickets SET estado = 'ESPERANDO_CIERRE' WHERE id_ticket = %s", (ticket_id,))
        conn.commit()
        return ticket_id
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


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
        f"📝 *Resumen de Solución ({md(ticket_id)})*\n\n"
        "Por favor escribe en un mensaje una **breve explicación del trabajo o solución realizada** para concluir el ticket.")


# ----------------------------------------------------
# FASE 1.2 - COLA Y DESCARGA ROBUSTA DE EVIDENCIAS
# ----------------------------------------------------
MAX_DESCARGAS_SIMULTANEAS = 3
SEM_DESCARGAS = asyncio.Semaphore(MAX_DESCARGAS_SIMULTANEAS)


async def descargar_archivo_con_reintentos(bot, file_id, ruta_local, ticket_id, intentos=3):
    """
    Descarga una evidencia con máximo 3 intentos.
    Solo ocupa uno de los 3 espacios simultáneos mientras realmente descarga.
    """
    ultimo_error = None
    esperas = (0, 2, 5)

    for intento in range(1, intentos + 1):
        if esperas[intento - 1]:
            await asyncio.sleep(esperas[intento - 1])

        try:
            async with SEM_DESCARGAS:
                logging.info(
                    f"📥 [{ticket_id}] Descargando evidencia "
                    f"(intento {intento}/{intentos})"
                )

                archivo = await bot.get_file(
                    file_id,
                    read_timeout=45,
                    connect_timeout=20,
                    pool_timeout=20,
                )

                await archivo.download_to_drive(
                    custom_path=ruta_local,
                    read_timeout=45,
                    connect_timeout=20,
                    pool_timeout=20,
                )

            logging.info(f"✅ [{ticket_id}] Evidencia descargada correctamente")
            return True

        except Exception as e:
            ultimo_error = e
            logging.warning(
                f"⚠️ [{ticket_id}] Falló intento {intento}/{intentos}: "
                f"{type(e).__name__}: {e}"
            )

            # Si quedó un archivo incompleto, eliminarlo antes del siguiente intento
            try:
                if os.path.exists(ruta_local):
                    os.remove(ruta_local)
            except Exception as cleanup_error:
                logging.warning(
                    f"⚠️ [{ticket_id}] No se pudo eliminar archivo incompleto: "
                    f"{cleanup_error}"
                )

    logging.error(
        f"❌ [{ticket_id}] Evidencia falló después de {intentos} intentos. "
        f"Último error: {type(ultimo_error).__name__}: {ultimo_error}"
    )
    return False


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Evita que excepciones no controladas generen 'No error handlers are registered'."""
    logging.error(
        "❌ Excepción no controlada en el bot",
        exc_info=context.error
    )


# ----------------------------------------------------
# 4. CAPTURA SILENCIOSA DE EVIDENCIAS Y PROCESAMIENTO
# ----------------------------------------------------
def _buscar_ticket_activo_por_topic_sync(thread_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        # Busca el ticket asignado a este topic sin importar si el bot se cerró/reinició
        cursor.execute(
            "SELECT id_ticket, estado FROM tickets WHERE telegram_topic_id = %s AND estado IN ('EN_PROCESO', 'ESPERANDO_CIERRE')",
            (thread_id,)
        )
        return cursor.fetchone()
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def _finalizar_cierre_ticket_sync(ticket_id, diagnostico):
    """Trae el historial de chat y marca el ticket como CERRADO. Devuelve los mensajes."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)

        cursor.execute(
            "SELECT emisor_nombre, mensaje, fecha_hora FROM historial_chat WHERE id_ticket = %s ORDER BY fecha_hora ASC",
            (ticket_id,)
        )
        mensajes = cursor.fetchall()

        cursor.execute(
            "UPDATE tickets SET estado = 'CERRADO', diagnostico_cierre = %s, fecha_cierre = NOW() WHERE id_ticket = %s",
            (diagnostico, ticket_id)
        )
        conn.commit()
        return mensajes
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def _escribir_historial_cierre(ruta_carpeta, ticket_id, diagnostico, mensajes):
    """Escritura en disco del informe de cierre (E/S bloqueante -> se ejecuta vía run_sync)."""
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
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        if tipo == 'UBICACION':
            cursor.execute(
                "INSERT INTO evidencias (id_ticket, tipo, latitud, longitud) VALUES (%s, %s, %s, %s)",
                (ticket_id, tipo, latitud, longitud)
            )
        else:
            cursor.execute(
                "INSERT INTO evidencias (id_ticket, tipo, ruta_nube) VALUES (%s, %s, %s)",
                (ticket_id, tipo, ruta_nube)
            )
        conn.commit()
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def _contar_fotos_sync(ticket_id):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT COUNT(*) as total FROM evidencias WHERE id_ticket = %s AND tipo = 'FOTO'",
            (ticket_id,)
        )
        return cursor.fetchone()['total']
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def _registrar_mensaje_chat_sync(ticket_id, emisor_id, emisor_nombre, mensaje):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO historial_chat (id_ticket, emisor_id, emisor_nombre, mensaje) VALUES (%s, %s, %s, %s)",
            (ticket_id, emisor_id, emisor_nombre, mensaje)
        )
        conn.commit()
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


async def guardar_evidencia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    thread_id = msg.message_thread_id
    if not thread_id:
        return

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

    # A) EXPLICACIÓN Y FINALIZACIÓN DE CIERRE
    if estado_actual == 'ESPERANDO_CIERRE' and msg.text and not msg.text.startswith('/'):
        diagnostico = msg.text

        try:
            mensajes = await run_sync(_finalizar_cierre_ticket_sync, ticket_id, diagnostico)
            await run_sync(_escribir_historial_cierre, ruta_carpeta, ticket_id, diagnostico, mensajes)
        except Exception as e:
            logging.error(f"Error cerrando ticket {ticket_id}: {e}")
            await msg.reply_text("❌ No se pudo cerrar el ticket. Intenta de nuevo.")
            return

        await msg.reply_text(
            f"✅ *Ticket {md(ticket_id)} cerrado oficialmente.*\n"
            f"📋 **Diagnóstico registrado:** _{md(diagnostico)}_\n"
            f"📁 Toda la información quedó consolidada.",
            parse_mode="Markdown"
        )

        try:
            await context.bot.close_forum_topic(chat_id=msg.chat_id, message_thread_id=thread_id)
        except Exception as e:
            logging.warning(f"No se pudo archivar el hilo: {e}")

        return

    # B) PROCESAMIENTO SILENCIOSO DE EVIDENCIAS (SOLO NOTIFICA SI HAY ERROR)
    # SOLUCIÓN PROBLEMA 2: Sin avisos cuando guarda exitosamente
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
            nombre_foto = f"foto_{num_foto:02d}.jpg"
            ruta_local = os.path.join(ruta_carpeta, nombre_foto)

            descargado = await descargar_archivo_con_reintentos(
                context.bot, file_id, ruta_local, ticket_id, intentos=3
            )
            if not descargado:
                await msg.reply_text(
                    "⚠️ No se pudo descargar esta fotografía después de 3 intentos. "
                    "Por favor, vuelve a enviarla."
                )
                return

            await run_sync(_registrar_evidencia_sync, ticket_id, 'FOTO', ruta_local)

        elif msg.video:
            # SOLUCIÓN PROBLEMA 1: los videos ahora se descargan y registran igual
            # que documentos/audios (antes se perdían en silencio).
            video = msg.video
            file_id = video.file_id
            nombre_video = f"video_{video.file_unique_id}.mp4"
            ruta_local = os.path.join(ruta_carpeta, nombre_video)

            descargado = await descargar_archivo_con_reintentos(
                context.bot, file_id, ruta_local, ticket_id, intentos=3
            )
            if not descargado:
                await msg.reply_text(
                    "⚠️ No se pudo descargar este video después de 3 intentos. "
                    "Por favor, vuelve a enviarlo."
                )
                return

            await run_sync(_registrar_evidencia_sync, ticket_id, 'VIDEO', ruta_local)

        elif msg.document:
            doc = msg.document
            file_id = doc.file_id
            nombre_original = doc.file_name or f"documento_{doc.file_unique_id}"
            ruta_local = os.path.join(ruta_carpeta, nombre_original)

            descargado = await descargar_archivo_con_reintentos(
                context.bot, file_id, ruta_local, ticket_id, intentos=3
            )
            if not descargado:
                await msg.reply_text(
                    "⚠️ No se pudo descargar este documento después de 3 intentos. "
                    "Por favor, vuelve a enviarlo."
                )
                return

            await run_sync(_registrar_evidencia_sync, ticket_id, 'DOCUMENTO', ruta_local)

        elif msg.voice or msg.audio:
            audio_obj = msg.voice or msg.audio
            file_id = audio_obj.file_id
            nombre_audio = f"audio_{audio_obj.file_unique_id}.ogg"
            ruta_local = os.path.join(ruta_carpeta, nombre_audio)

            descargado = await descargar_archivo_con_reintentos(
                context.bot, file_id, ruta_local, ticket_id, intentos=3
            )
            if not descargado:
                await msg.reply_text(
                    "⚠️ No se pudo descargar este audio después de 3 intentos. "
                    "Por favor, vuelve a enviarlo."
                )
                return

            await run_sync(_registrar_evidencia_sync, ticket_id, 'AUDIO', ruta_local)

        elif msg.text and not msg.text.startswith('/'):
            await run_sync(
                _registrar_mensaje_chat_sync, ticket_id, msg.from_user.id, msg.from_user.full_name, msg.text
            )

    except Exception as e:
        logging.error(f"Error resguardando evidencia en ticket {ticket_id}: {e}")
        # ÚNICAMENTE envía mensaje si hubo falla al guardar
        await msg.reply_text("❌ *Error:* No se pudo guardar este archivo/ubicación. Intenta reenviarlo.")

# ----------------------------------------------------
# RECUPERACIÓN AUTOMÁTICA DE TICKETS ABIERTOS
# ----------------------------------------------------
def recuperar_ticket_por_topic(thread_id):
    """Consulta MySQL; no depende de memoria, por lo que sobrevive reinicios."""
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT id_ticket, estado FROM tickets "
            "WHERE telegram_topic_id = %s "
            "AND estado IN ('EN_PROCESO', 'ESPERANDO_CIERRE')",
            (thread_id,)
        )
        return cursor.fetchone()
    finally:
        if cursor: cursor.close()
        if conn: conn.close()


# ----------------------------------------------------
# 5. ARRANQUE DEL BOT
# ----------------------------------------------------
if __name__ == '__main__':
    # Construcción limpia de la aplicación
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
    
    app.add_handler(MessageHandler(
        filters.LOCATION | filters.PHOTO | filters.Document.ALL | filters.VOICE | filters.AUDIO | filters.VIDEO | filters.TEXT,
        guardar_evidencia
    ))

    app.add_error_handler(error_handler)

    print(
        "🚀 Bot en marcha. Máximo 3 descargas simultáneas; "
        "cada evidencia tiene hasta 3 intentos."
    )
    app.run_polling()