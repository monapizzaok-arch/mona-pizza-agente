# agent/main.py — Servidor FastAPI + Webhook de WhatsApp
# Generado por AgentKit

"""
Servidor principal del agente.
Funciona con cualquier proveedor (Zernio, Meta) gracias a la capa de providers.
"""

import asyncio
import logging
import os
import sys
import unicodedata
from collections import defaultdict
from contextlib import asynccontextmanager

# Los mensajes de WhatsApp pueden traer emojis, y quedan en los logs. La consola de
# Windows por default usa cp1252, que no los sabe imprimir y tira UnicodeEncodeError.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse

from agent.brain import generar_respuesta, obtener_mensaje_error
from agent.memory import (
    agregar_aviso,
    borrar_avisos,
    esta_pausado,
    guardar_conversacion,
    guardar_mensaje,
    inicializar_db,
    liberar_evento,
    limpiar_avisos_viejos,
    limpiar_eventos_viejos,
    marcar_evento_procesado,
    obtener_avisos_vigentes,
    obtener_historial,
    obtener_telefono_de_conversacion,
    set_pausado,
)
from agent.providers import obtener_proveedor
from agent.providers.base import MensajeEntrante

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

# Numero de WhatsApp (solo digitos, sin "+") desde el que el dueno/staff maneja a
# Lisa por comandos (pausar/reanudar/estado) en vez de conversar con ella. Vacio =
# la funcion queda deshabilitada, nadie puede pausarla por WhatsApp.
ADMIN_WHATSAPP_NUMBER = (os.getenv("ADMIN_WHATSAPP_NUMBER") or "").lstrip("+").strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("agentkit")
# En desarrollo queremos el detalle de NUESTRO agente, no el de las librerias.
# Poner el nivel raiz en DEBUG llena la terminal de ruido de aiosqlite y httpx
# y hace imposible leer lo que hizo el agente.
logger.setLevel(logging.DEBUG if ENVIRONMENT == "development" else logging.INFO)

PORT = int(os.getenv("PORT", "8000"))

# Un candado por numero de telefono. En WhatsApp es normal que alguien mande "hola" y
# medio segundo despues la pregunta de verdad: sin esto los dos mensajes se procesarian
# en paralelo, los dos leerian el mismo historial y las escrituras quedarian intercaladas.
_candados: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# Si la configuracion esta mal, guardamos el error y lo mostramos en el health check,
# en vez de reventar en el import y dejar a Railway reiniciando el contenedor a ciegas.
proveedor = None
error_configuracion: str | None = None
try:
    proveedor = obtener_proveedor()
except Exception as e:  # noqa: BLE001 — cualquier problema de configuracion
    error_configuracion = str(e)

# Resultado del chequeo de credenciales que se hace al arrancar. Se expone en el health
# check: que el servidor conteste no significa que el agente pueda responder por WhatsApp.
estado_proveedor: dict = {"ok": None, "detalle": "sin verificar"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Prepara la base de datos y chequea el proveedor al arrancar."""
    await inicializar_db()
    await limpiar_eventos_viejos()
    await limpiar_avisos_viejos()
    logger.info("Base de datos lista")
    logger.info(f"Servidor AgentKit escuchando en el puerto {PORT}")

    global estado_proveedor
    if proveedor is not None:
        logger.info(f"Proveedor de WhatsApp: {proveedor.__class__.__name__}")
        ok, detalle = await proveedor.verificar_conexion()
        estado_proveedor = {"ok": ok, "detalle": detalle}
        logger.info(f"Conexion con el proveedor: {'OK' if ok else 'ERROR'} — {detalle}")
    else:
        logger.error(f"Proveedor de WhatsApp NO configurado: {error_configuracion}")

    yield


app = FastAPI(title="AgentKit — WhatsApp AI Agent", version="2.0.0", lifespan=lifespan)

# Railway define esta variable sola en cada deploy (no hay que configurarla a mano).
# Se expone en el health check para saber CON CERTEZA que commit esta corriendo en
# produccion, en vez de adivinar mirando el dashboard o probando por WhatsApp.
COMMIT = (os.getenv("RAILWAY_GIT_COMMIT_SHA") or "")[:7] or "local"

# Solo el motor (sqlite/postgresql), nunca la URL completa -- trae usuario y clave, y
# este endpoint es publico. Sirve para confirmar que la memoria ya es persistente
# (postgresql) y no se va a borrar en el proximo redeploy (sqlite).
_DB_URL_CRUDA = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./agentkit.db")
DB_MOTOR = _DB_URL_CRUDA.split("://", 1)[0].split("+", 1)[0] if "://" in _DB_URL_CRUDA else "desconocido"


@app.get("/")
async def health_check():
    """Endpoint de salud para Railway y monitoreo."""
    if error_configuracion:
        return {"status": "error", "service": "agentkit", "commit": COMMIT, "detalle": error_configuracion}

    # Se responde 200 aunque las credenciales esten mal, para que Railway no marque el
    # deploy como caido y puedas leer el diagnostico. El detalle esta en el cuerpo.
    return {
        "status": "ok" if estado_proveedor["ok"] else "degradado",
        "service": "agentkit",
        "commit": COMMIT,
        "proveedor": proveedor.__class__.__name__ if proveedor else None,
        "conexion": estado_proveedor,
        # Bool + ultimos 4 digitos, no el numero completo: este endpoint es publico.
        # Sirve para confirmar con un curl si ADMIN_WHATSAPP_NUMBER de verdad le llego
        # al proceso corriendo, sin tener que ir a mirar las Variables de Railway.
        "admin_configurado": bool(ADMIN_WHATSAPP_NUMBER),
        "admin_termina_en": ADMIN_WHATSAPP_NUMBER[-4:] if ADMIN_WHATSAPP_NUMBER else None,
        "db_motor": DB_MOTOR,
    }


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    """Verificacion GET del webhook. La pide Meta; para Zernio no hace nada."""
    if proveedor is None:
        raise HTTPException(status_code=503, detail=error_configuracion or "Proveedor no configurado")

    respuesta = await proveedor.validar_webhook(request)
    if respuesta is not None:
        return PlainTextResponse(respuesta)

    # Meta pide un 403 cuando manda hub.mode=subscribe y el verify_token no coincide.
    # Devolverle 200 le hace creer que la URL quedo verificada cuando no es cierto.
    if request.query_params.get("hub.mode") == "subscribe":
        raise HTTPException(status_code=403, detail="Verify token incorrecto")

    return {"status": "ok"}


@app.post("/webhook")
async def webhook_handler(request: Request, tareas: BackgroundTasks):
    """
    Recibe los mensajes de WhatsApp.

    Contesta 200 de inmediato y procesa el mensaje en segundo plano.

    Esto NO es un detalle de estilo. Los proveedores esperan un 2xx en unos 5
    segundos y, si no lo reciben, reintentan el mismo evento hasta 7 veces. Como
    llamar a Claude tarda mas que eso, procesar antes de contestar hace que el
    cliente reciba la misma respuesta repetida. Por eso: responder primero,
    trabajar despues.
    """
    if proveedor is None:
        raise HTTPException(status_code=503, detail=error_configuracion or "Proveedor no configurado")

    if not await proveedor.verificar_firma(request):
        raise HTTPException(status_code=401, detail="Firma del webhook invalida")

    try:
        mensajes = await proveedor.parsear_webhook(request)
    except Exception as e:  # noqa: BLE001
        # Un payload raro no debe hacer que el proveedor reintente para siempre
        logger.error(f"No se pudo leer el webhook: {e}")
        return {"status": "ignorado"}

    encolados = 0
    for msg in mensajes:
        if not msg.texto.strip():
            continue

        # La entrega es "al menos una vez": el mismo evento puede llegar dos veces
        evento_id = msg.contexto.get("evento_id") or msg.mensaje_id
        if evento_id and not await marcar_evento_procesado(evento_id):
            logger.info(f"Evento repetido, se ignora: {evento_id}")
            continue

        if msg.enviado_por_humano:
            # Alguien del local le escribio al cliente a mano desde el inbox. Lisa no
            # tiene que contestar nada (ya se mando), pero si tiene que enterarse.
            tareas.add_task(guardar_mensaje_humano, msg)
            encolados += 1
            continue

        if msg.es_propio:
            continue  # eco de un mensaje que ya mando la propia API (Lisa)

        # "\" o "/" al principio distingue un comando de un mensaje normal (las dos
        # se aceptan porque "\" es incomodo de escribir en el teclado de un celular).
        # Sin el prefijo, ADMIN_WHATSAPP_NUMBER es un cliente mas y habla con Lisa
        # como cualquiera -- asi el mismo numero sirve para probar Y para operar.
        if ADMIN_WHATSAPP_NUMBER and msg.telefono == ADMIN_WHATSAPP_NUMBER and msg.texto.strip()[:1] in ("\\", "/"):
            logger.info(f"Comando del admin: {msg.texto}")
            tareas.add_task(procesar_comando_admin, msg)
            encolados += 1
            continue

        # Asociar el conversation_id con el telefono ANTES de encolar: si el local le
        # contesta a mano casi en el acto, guardar_mensaje_humano ya lo puede resolver.
        conversation_id = msg.contexto.get("conversation_id", "")
        if conversation_id:
            await guardar_conversacion(conversation_id, msg.telefono)

        if await esta_pausado():
            # Pausado: se guarda lo que dijo el cliente para no perder el hilo, pero
            # no se le contesta -- alguien del local se esta ocupando a mano.
            await guardar_mensaje(msg.telefono, "user", msg.texto)
            logger.info(f"Lisa esta pausada: se guardo el mensaje de {msg.telefono} sin responder")
            encolados += 1
            continue

        logger.info(f"Mensaje de {msg.telefono}: {msg.texto}")
        tareas.add_task(procesar_mensaje, msg)
        encolados += 1

    return {"status": "ok", "encolados": encolados}


async def guardar_mensaje_humano(msg: MensajeEntrante):
    """
    Guarda en la memoria de Lisa un mensaje que alguien del local escribio a mano desde
    el inbox (no via la API). No genera ninguna respuesta -- ya se mando. Sin esto, la
    proxima vez que Lisa le conteste a ese cliente no tendria ni idea de que el local ya
    le dijo algo, y podria contradecirlo.

    El proveedor ya resuelve el telefono del cliente (Zernio lo trae en
    conversation.participantId); la tabla de conversaciones queda como respaldo por si
    algun proveedor no lo pudiera resolver directo.
    """
    telefono = msg.telefono or await obtener_telefono_de_conversacion(msg.contexto.get("conversation_id", ""))
    if not telefono:
        logger.warning(
            f"Mensaje manual del local sin telefono resuelto (conversation_id={msg.contexto.get('conversation_id')}): "
            "no se pudo guardar en la memoria de ningun cliente"
        )
        return
    try:
        # Mismo candado por telefono que usa procesar_mensaje: sin el, este guardado
        # puede pisarse con la escritura de un mensaje normal que llegue casi al
        # mismo tiempo -- SQLite solo deja un escritor a la vez, y las dos corren
        # como background tasks independientes, sin ningun orden garantizado entre si.
        async with _candados[telefono]:
            await guardar_mensaje(telefono, "assistant", msg.texto)
        logger.info(f"Mensaje manual del local guardado en la memoria de {telefono}: {msg.texto}")
    except Exception as e:  # noqa: BLE001 — esto corre en background, si explota que quede en el log
        logger.exception(f"No se pudo guardar el mensaje manual de {telefono}: {e}")


def _normalizar_comando(texto: str) -> str:
    """minusculas, sin tildes y sin puntuacion final, para que 'Pausar', 'PAUSA?' o 'pausá.' matcheen igual."""
    t = texto.strip().lower().rstrip("?!.¿¡ ")
    return "".join(c for c in unicodedata.normalize("NFD", t) if unicodedata.category(c) != "Mn")


async def procesar_comando_admin(msg: MensajeEntrante):
    """
    ADMIN_WHATSAPP_NUMBER manda comandos con "\\" o "/" adelante (/pausar, /estado,
    /historial ...). No pasa por Claude -- se resuelve directo, mas rapido y sin
    gastar tokens. Sin el prefijo el mismo numero es un cliente mas (ver webhook_handler).
    """
    texto_comando = msg.texto.strip().lstrip("\\/").strip()
    comando = _normalizar_comando(texto_comando)

    if comando in ("pausar", "pausa", "pause"):
        await set_pausado(True)
        respuesta = "Lisa quedo pausada: no va a responder a los clientes hasta que la reactives con '/reanudar'."
    elif comando in ("reanudar", "activar", "resume", "reactivar"):
        await set_pausado(False)
        respuesta = "Lisa esta activa de nuevo."
    elif comando in ("estado", "status"):
        respuesta = "Lisa esta PAUSADA ahora mismo." if await esta_pausado() else "Lisa esta ACTIVA ahora mismo."
    elif comando.startswith("historial"):
        # Diagnostico: "/historial 5493876403872 [cantidad]" muestra lo que Lisa
        # tiene guardado de ese telefono -- util para confirmar que un mensaje manual
        # quedo bien guardado, sin tener que mirar la base de datos directamente.
        # Cantidad opcional (default 10, tope 30) por si el mensaje que buscas ya
        # quedo tapado por conversacion mas reciente.
        partes = texto_comando.split()
        telefono_consulta = partes[1].lstrip("+") if len(partes) > 1 else ""
        try:
            cantidad = min(30, max(1, int(partes[2]))) if len(partes) > 2 else 10
        except ValueError:
            cantidad = 10
        if not telefono_consulta:
            respuesta = "Usa: /historial <telefono> [cantidad], ej. /historial 5493876403872 20"
        else:
            historial = await obtener_historial(telefono_consulta, limite=cantidad)
            if not historial:
                respuesta = f"No hay nada guardado para {telefono_consulta}."
            else:
                # Recorte mas chico cuanto mas mensajes se piden, para no pasarse del
                # limite de WhatsApp (~4096 caracteres).
                recorte = 300 if cantidad <= 10 else 120
                lineas = [f"{'Cliente' if h['role'] == 'user' else 'Lisa'}: {h['content'][:recorte]}" for h in historial]
                respuesta = f"Ultimos {len(historial)} mensajes de {telefono_consulta}:\n\n" + "\n\n".join(lineas)
    elif comando.startswith("aviso"):
        # "/aviso <texto>" carga algo que Lisa tiene que tener en cuenta SOLO hoy (se
        # inyecta en su prompt en cada mensaje, ver brain.py) sin tocar prompts.yaml ni
        # hacer un deploy: "nos quedamos sin rucula", "ofrece primero la promo x3".
        # No se usa _normalizar_comando acá para el texto: mancharia mayusculas y
        # tildes del aviso real.
        resto = texto_comando[len("aviso"):].lstrip(":").strip()
        resto_norm = _normalizar_comando(resto)
        if resto_norm in ("", "ver", "listar"):
            avisos = await obtener_avisos_vigentes()
            respuesta = (
                ("Avisos de hoy:\n\n" + "\n".join(f"- {a}" for a in avisos))
                if avisos
                else "No hay avisos cargados para hoy. Usa: /aviso <texto>"
            )
        elif resto_norm in ("borrar", "limpiar", "quitar"):
            await borrar_avisos()
            respuesta = "Avisos borrados."
        else:
            await agregar_aviso(resto)
            respuesta = f'Listo, Lisa ya sabe: "{resto}". Vale solo por hoy.'
    else:
        respuesta = (
            "No reconozco ese comando. Escribi /pausar, /reanudar, /estado, "
            "/historial <telefono> o /aviso <texto>."
        )

    try:
        await proveedor.enviar_mensaje(msg.telefono, respuesta, msg.contexto)
    except Exception as e:  # noqa: BLE001
        logger.error(f"No se pudo responder el comando del admin: {e}")


async def procesar_mensaje(msg: MensajeEntrante):
    """
    Genera la respuesta y la manda de vuelta. Corre fuera del ciclo del webhook.

    Se toma un candado por telefono: dos mensajes seguidos del mismo cliente se
    atienden en orden, no en paralelo, para que el historial no se mezcle.
    """
    evento_id = msg.contexto.get("evento_id") or msg.mensaje_id

    async with _candados[msg.telefono]:
        try:
            # El historial se lee ANTES de guardar el mensaje actual: brain.py agrega
            # el mensaje nuevo al final, y asi no queda duplicado.
            historial = await obtener_historial(msg.telefono)
            respuesta, es_respuesta_real = await generar_respuesta(msg.texto, historial, telefono=msg.telefono)

            enviado = await proveedor.enviar_mensaje(msg.telefono, respuesta, msg.contexto)

            if not enviado:
                # El evento se marco como procesado ANTES de llegar hasta aca, para que dos
                # entregas simultaneas no se dupliquen. Si el envio fallo, hay que soltarlo:
                # si no, el reintento del proveedor se descartaria por duplicado y el cliente
                # se quedaria sin respuesta para siempre.
                logger.error(f"No se pudo enviar la respuesta a {msg.telefono}; se libera el evento")
                await liberar_evento(evento_id)
                return

            # Solo se guarda en el historial lo que de verdad es conversacion. Los avisos
            # tecnicos ("estoy teniendo problemas") no son un turno del agente: guardarlos
            # los deja contaminando el contexto de todos los mensajes que vengan despues.
            if es_respuesta_real:
                await guardar_mensaje(msg.telefono, "user", msg.texto)
                await guardar_mensaje(msg.telefono, "assistant", respuesta)

            logger.info(f"Respuesta enviada a {msg.telefono}: {respuesta}")

        except Exception as e:  # noqa: BLE001
            logger.exception(f"Error procesando el mensaje de {msg.telefono}: {e}")
            await liberar_evento(evento_id)
            try:
                await proveedor.enviar_mensaje(msg.telefono, obtener_mensaje_error(), msg.contexto)
            except Exception:  # noqa: BLE001
                logger.error("Tampoco se pudo avisarle al cliente del error")
