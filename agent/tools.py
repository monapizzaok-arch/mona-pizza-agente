# agent/tools.py — Herramientas del agente
# Generado por AgentKit

"""
Herramientas especificas del negocio de Mona Pizza.

La informacion del negocio (menu, precios, horarios) le llega al agente por el system
prompt (config/prompts.yaml), asi que para CONTESTAR preguntas no hace falta nada de
aca. Este archivo es el lugar para las ACCIONES: registrar_pedido(), conectada al ciclo
de tool use de Claude en brain.py, carga el pedido directo en el sistema de gestion de
Mona Pizza (LocalDB / api.php) — el mismo que usa la web de pedidos.
"""

import json
import logging
import os
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("agentkit")

CARPETA_KNOWLEDGE = Path("knowledge")

# El backend de Mona Pizza (LocalDB): mismo api.php que usa la web publica de pedidos.
# guardarPedido es una accion publica, no requiere login ni API key.
LOCALDB_API_URL = os.getenv("LOCALDB_API_URL") or "https://monapizza.com.ar/api/api.php"

# Costo de envio fijo. Coincide con lo que dice el prompt (config/prompts.yaml) y con
# el valor que usa la web publica. Si el negocio lo cambia, hay que actualizarlo en los
# dos lugares — no hay forma de consultarlo en vivo sin loguearse como personal
# (configCobro exige sesion de staff, y Lisa no tiene una).
COSTO_ENVIO = 1000.0


def cargar_info_negocio() -> dict:
    """Carga la informacion del negocio desde config/business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/business.yaml no encontrado")
        return {}


def obtener_horario() -> dict:
    """Retorna el horario de atencion del negocio."""
    info = cargar_info_negocio()
    return {
        "horario": info.get("negocio", {}).get("horario", "No disponible"),
        "esta_abierto": True,  # TODO: calcular segun la hora actual y el horario
    }


def buscar_en_knowledge(consulta: str) -> str:
    """
    Busca informacion en los archivos de /knowledge.
    Retorna los fragmentos que coinciden con la consulta.
    """
    if not CARPETA_KNOWLEDGE.is_dir():
        return "No hay archivos de conocimiento disponibles."

    resultados = []
    for ruta in sorted(CARPETA_KNOWLEDGE.iterdir()):
        if ruta.name.startswith(".") or not ruta.is_file():
            continue
        try:
            contenido = ruta.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binarios y archivos ilegibles se saltean
        if consulta.lower() in contenido.lower():
            resultados.append(f"[{ruta.name}]: {contenido[:500]}")

    if resultados:
        return "\n---\n".join(resultados)
    return "No encontre informacion especifica sobre eso en mis archivos."


# ════════════════════════════════════════════════════════════
# Toma de pedidos
# ════════════════════════════════════════════════════════════
#
# registrar_pedido() la llama brain.py cuando el cliente confirma el pedido en firme.
# Carga el pedido DIRECTO en la base de Mona Pizza a traves de la misma accion
# (guardarPedido) que usa la web publica: aparece al toque en comandas-cocina.html,
# se lo imprime el puente, y el TPV lo ve como cualquier otro pedido. No hay paso de
# revision manual — por eso brain.py solo la llama despues de que el cliente confirmo
# productos, entrega, direccion (si aplica) y forma de pago.
#
# Los items ya vienen con el precio de cada linea CALCULADO por Lisa (ella conoce el
# menu completo y las reglas de combos/mitad-y-mitad de config/prompts.yaml). Esta
# funcion no vuelve a mirar precios: solo suma lo que le llega y arma el pedido.


async def registrar_pedido(
    telefono: str,
    nombre: str,
    entrega: str,
    items: list[dict],
    domicilio: str = "",
    pago: str = "Efectivo",
    nota: str = "",
) -> dict:
    """
    Registra el pedido en el sistema de Mona Pizza (LocalDB).

    Args:
        telefono: numero de WhatsApp del cliente (lo agrega brain.py, no lo inventa Claude)
        nombre: nombre del cliente
        entrega: "Delivery" o "Retiro en local"
        items: lista de {"nombre": str, "detalle": str, "cantidad": int, "precio": float}
               — "precio" es el precio YA CALCULADO de esa linea completa (cantidad
               incluida), no el precio unitario.
        domicilio: direccion de entrega, obligatoria si entrega es "Delivery"
        pago: "Efectivo" o "Transferencia" (por WhatsApp no se ofrece Tarjeta)
        nota: aclaraciones del cliente (sin cebolla, timbre roto, etc.)

    Returns:
        {"ok": True, "id": <id del pedido>, "seguimiento": <link>} si se registro bien.
        {"ok": False, "error": <motivo>} si el sistema lo rechazo (ej. local cerrado).
    """
    if not items:
        return {"ok": False, "error": "El pedido no tiene productos."}

    subtotal = round(sum(float(i.get("precio") or 0) for i in items), 2)
    envio = COSTO_ENVIO if "delivery" in entrega.lower() else 0.0
    total = subtotal + envio

    payload = {
        "nombre": nombre,
        "telefono": telefono,
        "entrega": entrega,
        "domicilio": domicilio,
        "pago": pago,
        "items": [
            {
                "nombre": i.get("nombre", ""),
                "detalle": i.get("detalle") or None,
                "cantidad": i.get("cantidad", 1),
                "precio": float(i.get("precio") or 0),
            }
            for i in items
        ],
        "subtotal": subtotal,
        "envio": envio,
        "descuento": 0,
        "recargo": 0,
        "cupon": "",
        "cuponDescuento": 0,
        "total": total,
        "nota": nota,
        "negocio": "monapizza",
        "origen": "web",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as cliente:
            r = await cliente.get(
                LOCALDB_API_URL,
                params={"accion": "guardarPedido", "data": json.dumps(payload, ensure_ascii=False)},
            )
    except httpx.HTTPError as e:
        logger.error(f"Error de red registrando pedido en LocalDB: {e}")
        return {"ok": False, "error": "No pude conectarme al sistema de pedidos. Probá de nuevo en un minuto."}

    try:
        cuerpo = r.json()
    except ValueError:
        logger.error(f"LocalDB devolvio una respuesta no-JSON [{r.status_code}]: {r.text[:300]}")
        return {"ok": False, "error": "El sistema de pedidos no respondio como se esperaba."}

    if not cuerpo.get("ok"):
        error = cuerpo.get("error", "El sistema de pedidos rechazo el pedido.")
        logger.warning(f"LocalDB rechazo el pedido de {telefono}: {error}")
        return {"ok": False, "error": error}

    idPedido = cuerpo["id"]
    logger.info(f"Pedido #{idPedido} registrado en LocalDB para {telefono} — total ${total}")
    return {
        "ok": True,
        "id": idPedido,
        "total": total,
        # Mismo link que arma la web de pedidos (pedir__index.html) al confirmar.
        "seguimiento": f"https://monapizza.com.ar/seguimiento.html?id={idPedido}",
    }
