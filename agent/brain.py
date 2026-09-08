# agent/brain.py — Cerebro del agente: conexion con Claude
# Generado por AgentKit

"""
Logica de IA del agente. Lee el system prompt de config/prompts.yaml y genera las
respuestas con la API de Anthropic.
"""

import json
import logging
import os

import yaml
from anthropic import AsyncAnthropic
from dotenv import load_dotenv

from agent.tools import registrar_pedido

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
            "resumen con el total. No la llames para cotizar o simular un pedido."
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
                        "Cada linea del pedido, con el precio YA CALCULADO por vos "
                        "(aplicando el combo mas economico de empanadas y la regla de "
                        "mitad y mitad si corresponde). 'precio' es el total de esa "
                        "linea, no el precio unitario."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "nombre": {"type": "string"},
                            "detalle": {
                                "type": "string",
                                "description": "Tamanio o variante, ej. 'Entera', 'Media Pepperoni + Media Muzzarella', '1 docena'",
                            },
                            "cantidad": {"type": "integer"},
                            "precio": {
                                "type": "number",
                                "description": "Precio total de la linea, combo/mitad-y-mitad ya aplicado",
                            },
                        },
                        "required": ["nombre", "cantidad", "precio"],
                    },
                },
                "nota": {
                    "type": "string",
                    "description": "Aclaraciones del cliente (sin cebolla, timbre roto, etc.), opcional",
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


async def generar_respuesta(mensaje: str, historial: list[dict], telefono: str = "") -> tuple[str, bool]:
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
