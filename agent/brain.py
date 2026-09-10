# agent/brain.py — Cerebro del agente: conexion con Claude
# Generado por AgentKit

"""
Logica de IA del agente. Lee el system prompt de config/prompts.yaml y genera las
respuestas con la API de Anthropic.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import yaml
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from agent.memory import obtener_avisos_vigentes, obtener_datos
from agent.tools import (
    consultar_estado_negocio,
    formatear_catalogo,
    obtener_horarios_semanales,
    obtener_menu,
    registrar_pedido,
)

load_dotenv()
logger = logging.getLogger("agentkit")

client = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# Cuantas veces como maximo se le permite a Claude encadenar llamadas a herramientas
# en un solo mensaje del cliente. Sin este limite, un loop de tool use que nunca
# termina en texto dejaria al webhook procesando para siempre.
MAX_ITERACIONES_TOOL = 3

# La unica herramienta real: registrar el pedido en el sistema de Mona Pizza. Cada
# llamada carga un pedido DE VERDAD (aparece en cocina y TPV al instante) — por eso la
# descripcion insiste en que Claude la use solo tras la confirmacion final del cliente,
# nunca para "probar" o cotizar.
TOOLS = [
    {
        "name": "registrar_pedido",
        "description": (
            "Registra el pedido del cliente directamente en el sistema de Mona Pizza. "
            "El pedido aparece al instante en la pantalla de cocina y se imprime — no hay "
            "revision manual despues. Llamala SOLO una vez, y solo cuando el cliente ya "
            "confirmo en firme: los productos con sus tamanios/cantidades, si es delivery "
            "o retiro (con la direccion si es delivery), la forma de pago, y viste el "
            "resumen con el total. No la llames para cotizar o simular un pedido. Cada "
            "item tiene que referenciar un producto REAL del bloque 'Catalogo en vivo' "
            "del prompt (por su id) — nunca inventes un producto ni un precio."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre": {"type": "string", "description": "Nombre del cliente"},
                "entrega": {
                    "type": "string",
                    "enum": ["Delivery", "Retiro en local"],
                },
                "domicilio": {
                    "type": "string",
                    "description": "Direccion de entrega. Obligatoria si entrega es Delivery; vacia si es Retiro en local.",
                },
                "pago": {
                    "type": "string",
                    "enum": ["Efectivo", "Transferencia"],
                    "description": "Por WhatsApp no se ofrece pago con Tarjeta.",
                },
                "items": {
                    "type": "array",
                    "description": (
                        "Cada linea referencia UN producto real del catalogo en vivo por su "
                        "id (ej. 'P1', 'e1', 'c3') — nunca un producto inventado, y sin "
                        "agregarle texto: se carga igual que lo haria la web. Para un pack "
                        "de empanadas (docena / media docena / unidad), agregá una linea "
                        "por cada tamaño, todas con el mismo producto_id y el "
                        "variante_index que corresponda (nunca un producto tipo '8 "
                        "empanadas'). Para una pizza mitad y mitad, cada mitad es SU PROPIA "
                        "linea (su propio producto_id, variante_index en 'Media') — NUNCA "
                        "una linea combinada tipo 'Hawaiana+Muzzarela'. Una mitad y mitad "
                        "no es nada fuera de lo comun, no hace falta aclarar nada."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "producto_id": {
                                "type": "string",
                                "description": "Id del producto tal cual figura en el catalogo en vivo (ej. 'P1')",
                            },
                            "variante_index": {
                                "type": "integer",
                                "description": "Indice de la variante elegida dentro de ese producto (0 = primera variante listada, 1 = segunda, etc.)",
                            },
                            "cantidad": {"type": "integer", "description": "Cuantas unidades de ESTA linea (producto+variante)"},
                        },
                        "required": ["producto_id", "variante_index", "cantidad"],
                    },
                },
                "nota": {
                    "type": "string",
                    "description": (
                        "SOLO para algo realmente fuera de lo comun (sin cebolla, timbre "
                        "roto, etc.). No la uses para describir combos normales como mitad "
                        "y mitad o los packs de empanadas — esos ya quedan claros con los "
                        "items en si, sin aclaracion."
                    ),
                },
            },
            "required": ["nombre", "entrega", "pago", "items"],
        },
    }
]

# El modelo se cambia desde .env, sin tocar el codigo.
#   claude-opus-5     el mas capaz             $5 / $25 por millon de tokens
#   claude-sonnet-5   el balanceado (default)  $3 / $15
#   claude-haiku-4-5  el mas barato y rapido   $1 / $5
# El "or" y no el default de os.getenv: una variable declarada vacia en el .env
# devuelve "" y dejaria al agente sin modelo.
MODELO = os.getenv("ANTHROPIC_MODEL") or "claude-sonnet-5"

# Es un bot de respuestas cortas: con esfuerzo bajo contesta mas rapido y mas barato.
# Dejalo vacio en el .env para no mandar el parametro.
ESFUERZO = os.getenv("ANTHROPIC_EFFORT", "low").strip()

# WhatsApp son mensajes cortos, pero este tope NO es solo la respuesta: en los modelos
# actuales el razonamiento interno tambien cuenta contra el. Con el margen justo, una
# pregunta que exija pensar un poco deja al agente sin espacio para contestar.
MAX_TOKENS = int(os.getenv("ANTHROPIC_MAX_TOKENS") or "4096")

# Los modelos mas viejos no aceptan output_config. Si la primera llamada falla por eso,
# se reintenta sin el parametro y se recuerda para las siguientes.
_soporta_esfuerzo = True


def cargar_config_prompts() -> dict:
    """Lee toda la configuracion desde config/prompts.yaml."""
    try:
        with open("config/prompts.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/prompts.yaml no encontrado")
        return {}


def cargar_system_prompt() -> str:
    """El system prompt: quien es el agente y que sabe del negocio."""
    return cargar_config_prompts().get(
        "system_prompt", "Eres un asistente util. Responde siempre en espanol."
    )


def obtener_mensaje_error() -> str:
    """Que decirle al cliente cuando algo falla de nuestro lado."""
    return cargar_config_prompts().get(
        "error_message",
        "Lo siento, estoy teniendo problemas tecnicos. Por favor intenta de nuevo en unos minutos.",
    )


def obtener_mensaje_fallback() -> str:
    """Que decirle al cliente cuando no se entendio el mensaje."""
    return cargar_config_prompts().get(
        "fallback_message", "Disculpa, no entendi tu mensaje. Podrias reformularlo?"
    )


def _antiguedad_historial(historial: list[dict]) -> str | None:
    """
    Hace cuanto fue el ultimo mensaje del historial, en texto ("2 dias", "5 horas").

    None si el historial esta vacio o es reciente: si la conversacion viene corrida no
    hace falta aclarar nada, y aclararlo de mas solo gasta tokens y confunde.
    """
    if not historial:
        return None
    ultimo = historial[-1].get("timestamp")
    if ultimo is None:
        return None

    # SQLite puede devolver el timestamp sin zona horaria; se asume UTC, que es como se
    # guarda (ver ahora() en memory.py).
    if ultimo.tzinfo is None:
        ultimo = ultimo.replace(tzinfo=timezone.utc)

    minutos = (datetime.now(timezone.utc) - ultimo).total_seconds() / 60
    if minutos < 180:  # menos de 3 horas: es la misma charla, no hay nada que aclarar
        return None
    if minutos < 1440:
        return f"{int(minutos // 60)} horas"
    dias = int(minutos // 1440)
    return "1 día" if dias == 1 else f"{dias} días"


def _extraer_texto(respuesta) -> str:
    """
    Junta el texto de la respuesta de Claude.

    Ojo: NO se puede hacer respuesta.content[0].text. La respuesta es una lista de
    bloques y el primero no siempre es texto (los modelos que razonan devuelven
    primero un bloque de pensamiento). Hay que filtrar por tipo.
    """
    partes = [bloque.text for bloque in respuesta.content if bloque.type == "text"]
    return "\n".join(p for p in partes if p).strip()


async def _ejecutar_tool(nombre: str, argumentos: dict, telefono: str) -> dict:
    """
    Corre la herramienta que pidio Claude y devuelve el resultado como dict.

    El telefono lo pone esta funcion, no Claude: es el numero real de WhatsApp que
    mando el mensaje (main.py lo conoce desde el webhook), no algo que el modelo
    tenga que adivinar o pueda inventar mal.
    """
    if nombre == "registrar_pedido":
        return await registrar_pedido(
            telefono=telefono,
            nombre=argumentos.get("nombre", ""),
            entrega=argumentos.get("entrega", ""),
            items=argumentos.get("items") or [],
            domicilio=argumentos.get("domicilio", ""),
            pago=argumentos.get("pago", "Efectivo"),
            nota=argumentos.get("nota", ""),
        )
    logger.error(f"Claude pidio una herramienta desconocida: {nombre}")
    return {"ok": False, "error": f"Herramienta desconocida: {nombre}"}


def _es_error_de_esfuerzo(error: Exception) -> bool:
    """
    True solo si el modelo rechazo la llamada POR el parametro output_config/effort.

    Se exige que sea un 400 de peticion invalida y no cualquier error que mencione la
    palabra: un 529 de sobrecarga que la nombre de paso no debe apagar el parametro
    para todo el proceso.
    """
    if getattr(error, "status_code", None) != 400:
        return False
    texto = str(error).lower()
    return "output_config" in texto or "effort" in texto


async def generar_respuesta(
    mensaje: str, historial: list[dict], telefono: str = "", estado_negocio: dict | None = None
) -> tuple[str, bool]:
    """
    Genera una respuesta con Claude. Si Claude decide registrar un pedido, ejecuta la
    herramienta y le devuelve el resultado antes de pedirle la respuesta final.

    Args:
        mensaje: el mensaje nuevo del cliente
        historial: los mensajes anteriores, [{"role": "user"|"assistant", "content": "..."}]
        telefono: numero de WhatsApp del cliente, para registrar_pedido (ver _ejecutar_tool)

    Returns:
        (texto, es_respuesta_real)

        "es_respuesta_real" es False cuando lo que se devuelve es un aviso tecnico
        (error o fallback) y no una respuesta del agente. main.py lo usa para no
        guardar esos avisos en el historial: si se guardaran, quedarian contaminando
        el contexto de todos los mensajes siguientes.
    """
    global _soporta_esfuerzo

    if not mensaje or len(mensaje.strip()) < 2:
        return obtener_mensaje_fallback(), False

    mensajes = [{"role": m["role"], "content": m["content"]} for m in historial]
    mensajes.append({"role": "user", "content": mensaje})

    system_prompt = cargar_system_prompt()

    # Estado del local, catalogo y avisos del dia, todo EN VIVO (mismo horario/stock/
    # precios que admin.html, mas lo que haya cargado el local por /aviso), no lo que
    # diga el texto estatico del prompt de mas arriba. Las consultas son independientes
    # asi que van en paralelo. Si quien llama ya consulto el estado del local (main.py
    # lo necesita antes, para decidir si contestar), lo pasa y no se vuelve a pedir.
    if estado_negocio is None:
        estado_negocio, menu, avisos, datos, horarios = await asyncio.gather(
            consultar_estado_negocio(),
            obtener_menu("monapizza"),
            obtener_avisos_vigentes(),
            obtener_datos(),
            obtener_horarios_semanales(),
        )
    else:
        menu, avisos, datos, horarios = await asyncio.gather(
            obtener_menu("monapizza"),
            obtener_avisos_vigentes(),
            obtener_datos(),
            obtener_horarios_semanales(),
        )

    if horarios:
        system_prompt += (
            "\n\n## Horario de atención de la semana (en vivo, lo que está cargado en el sistema)\n"
            f"{horarios}\n"
            "Usá ESTO para cualquier pregunta sobre qué días o a qué hora abren, y no el "
            "horario escrito más arriba: este sale del sistema y está siempre al día."
        )

    if datos:
        system_prompt += (
            "\n\n## Lo que el local te fue enseñando (vale siempre, hasta que lo saquen)\n"
            + "\n".join(f"- {t}" for _, t in datos)
            + "\nSi algo de acá contradice el menú o la descripción del negocio de más "
            "arriba, mandá esto: lo cargó el local después, sabiendo lo que decía el prompt."
        )

    if avisos:
        system_prompt += (
            "\n\n## Avisos de hoy (cargados por el local, valen SOLO por hoy — tienen "
            "prioridad sobre el catálogo y el menú de más arriba si se contradicen)\n"
            + "\n".join(f"- {a}" for a in avisos)
        )

    if menu is not None:
        config = menu.get("config", {})
        alias = config.get("cbu_alias") or config.get("cbu") or config.get("alias") or ""
        try:
            tiempo_prep = int(float(config.get("tiempo_preparacion_default_monapizza") or 0))
        except (TypeError, ValueError):
            tiempo_prep = 0
        try:
            tiempo_envio = int(float(config.get("tiempo_envio_default_monapizza") or 0))
        except (TypeError, ValueError):
            tiempo_envio = 0
        system_prompt += (
            "\n\n## Catálogo en vivo (fuente real de precios, stock e ids — el menú de más "
            "arriba en este prompt es solo referencia de ingredientes/sabores y puede tener "
            "precios viejos)\n"
            f"{formatear_catalogo(menu)}\n\n"
            f"Alias/CBU para transferencias: {alias or '(no hay uno cargado — si el cliente pide pagar por transferencia, avisale que le vas a confirmar el dato)'}\n\n"
            f"Tiempo estimado (según el sistema): preparación ~{tiempo_prep} min. "
            f"Para retiro en local, el pedido está listo en ~{tiempo_prep} min. "
            f"Para delivery, sumale el envío (~{tiempo_envio} min): total estimado ~{tiempo_prep + tiempo_envio} min. "
            "Mencionalo cuando confirmes un pedido, o si el cliente pregunta cuánto tarda.\n\n"
            "Para armar cada item de registrar_pedido usá el id real de acá (producto_id) y "
            "la posición de la variante elegida (variante_index: 0 = la primera de la lista "
            "para ese producto, 1 = la segunda, etc.). Un producto marcado SIN STOCK no se "
            "ofrece ni se agrega a ningún pedido — decile al cliente que por ahora no hay."
        )

    # Si la conversacion venia de antes, decirlo: el historial no trae ninguna marca de
    # tiempo, asi que un mensaje de hace tres dias le llega al modelo igual que uno de
    # hace treinta segundos. Eso hacia que un "hacen envios?" se leyera como la
    # continuacion de una charla en curso, arrastrando el pedido y el estado de aquel
    # momento.
    antiguedad = _antiguedad_historial(historial)
    if antiguedad:
        system_prompt += (
            "\n\n## OJO: esta conversación viene de antes\n"
            f"Los mensajes anteriores son de hace {antiguedad}. NO es una charla en curso: "
            "el cliente volvió a escribir recién ahora.\n"
            "Todo lo que aparezca ahí —un pedido a medio armar, precios, si estaban abiertos, "
            "qué había en stock— es de aquel momento y puede no valer más. No lo des por "
            "vigente: confirmá contra los datos de este prompt, que son los de ahora."
        )

    # El estado del local va ULTIMO, pegado a la conversacion, y no antes del catalogo:
    # es el dato que mas se contradice con el historial (que puede tener mensajes de
    # hace horas, de cuando el local SI estaba abierto) y el que peor sale si el modelo
    # lo pasa por alto. Con un modelo chico (Haiku), ponerlo lejos del final hacia que
    # la primera respuesta omitiera que estaban cerrados y siguiera la inercia del
    # historial, hasta que el cliente lo corregia.
    if estado_negocio.get("abierto") is not None:
        abierto = estado_negocio["abierto"]
        system_prompt += (
            "\n\n## ESTADO DEL LOCAL AHORA MISMO — dato en vivo, es la verdad\n"
            f"{'ABIERTO' if abierto else 'CERRADO'}. {estado_negocio['mensaje']}\n"
            f"Retiro disponible: {'si' if estado_negocio['retiro'] else 'no'}. "
            f"Delivery disponible: {'si' if estado_negocio['delivery'] else 'no'}.\n"
            "Este bloque le gana a TODO lo de arriba y también al historial de la "
            "conversación: los mensajes anteriores pueden ser de hace horas, de cuando el "
            "local estaba abierto, y no dicen nada de cómo está ahora.\n"
        )
        if not abierto:
            system_prompt += (
                "Como está CERRADO: decilo vos en tu PRIMERA respuesta, aunque el cliente "
                "no haya preguntado por el horario y aunque venga hablando de un pedido. "
                "No tomes ni registres ningún pedido. Podés seguir respondiendo dudas del "
                "menú con normalidad y dejar todo listo para cuando abran.\n"
                "NUNCA digas que están abiertos, ni que la web se equivoca o 'tarda en "
                "actualizarse': la web y vos leen exactamente el mismo dato, este."
            )

    extras = {"output_config": {"effort": ESFUERZO}} if (_soporta_esfuerzo and ESFUERZO) else {}

    async def _llamar(parametros_extra: dict):
        return await client.messages.create(
            model=MODELO,
            max_tokens=MAX_TOKENS,
            system=system_prompt,
            messages=mensajes,
            tools=TOOLS,
            **parametros_extra,
        )

    try:
        respuesta = await _llamar(extras)
    except Exception as e:  # noqa: BLE001
        if extras and _es_error_de_esfuerzo(e):
            logger.warning(
                f"El modelo {MODELO} no acepta output_config.effort; se reintenta sin ese parametro."
            )
            _soporta_esfuerzo = False
            try:
                respuesta = await _llamar({})
            except Exception as e2:  # noqa: BLE001
                logger.error(f"Error llamando a Claude: {e2}")
                return obtener_mensaje_error(), False
        else:
            logger.error(f"Error llamando a Claude: {e}")
            return obtener_mensaje_error(), False

    # Loop de tool use: Claude puede pedir registrar_pedido, recibir el resultado
    # (ok+id o el error del sistema) y despues contestarle al cliente con eso. El
    # limite de iteraciones evita quedar encadenando llamadas si Claude no converge
    # nunca a una respuesta de texto.
    iteraciones = 0
    while getattr(respuesta, "stop_reason", None) == "tool_use" and iteraciones < MAX_ITERACIONES_TOOL:
        iteraciones += 1
        mensajes.append({"role": "assistant", "content": respuesta.content})

        resultados = []
        for bloque in respuesta.content:
            if bloque.type != "tool_use":
                continue
            logger.info(f"Claude pidio la herramienta '{bloque.name}' para {telefono}: {bloque.input}")
            resultado = await _ejecutar_tool(bloque.name, bloque.input, telefono)
            resultados.append(
                {
                    "type": "tool_result",
                    "tool_use_id": bloque.id,
                    "content": json.dumps(resultado, ensure_ascii=False),
                }
            )
        mensajes.append({"role": "user", "content": resultados})

        try:
            respuesta = await _llamar(extras if (_soporta_esfuerzo and ESFUERZO) else {})
        except Exception as e:  # noqa: BLE001
            logger.error(f"Error llamando a Claude despues de ejecutar una herramienta: {e}")
            return obtener_mensaje_error(), False

    if getattr(respuesta, "stop_reason", None) == "tool_use":
        # Se agoto MAX_ITERACIONES_TOOL sin que Claude cerrara en texto: no debería
        # pasar (una sola herramienta, sin motivo para encadenar), pero mejor avisar
        # con el mensaje de error que dejar al cliente sin respuesta.
        logger.error(f"Se agotaron las iteraciones de tool use para {telefono} sin respuesta de texto")
        return obtener_mensaje_error(), False

    if getattr(respuesta, "stop_reason", None) == "max_tokens":
        logger.warning(
            f"La respuesta se corto por llegar al tope de {MAX_TOKENS} tokens. "
            "Si pasa seguido, sube ANTHROPIC_MAX_TOKENS o acorta el system prompt."
        )

    texto = _extraer_texto(respuesta)
    if not texto:
        logger.warning("Claude devolvio una respuesta sin texto")
        return obtener_mensaje_fallback(), False

    logger.info(
        f"Respuesta generada con {MODELO} "
        f"({respuesta.usage.input_tokens} in / {respuesta.usage.output_tokens} out)"
    )
    return texto, True
