# agent/tools.py — Herramientas del agente
# Generado por AgentKit

"""
Herramientas especificas del negocio de Mona Pizza.

OJO: estas funciones NO se ejecutan solas todavia. La informacion del negocio (menu,
precios, horarios) le llega al agente por el system prompt (config/prompts.yaml), asi
que para CONTESTAR preguntas no hace falta nada de aca. Este archivo es el lugar para
las ACCIONES: registrar pedidos. Conectarlas al ciclo de tool use de Claude es un paso
aparte, todavia no implementado.
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

import yaml

logger = logging.getLogger("agentkit")

CARPETA_KNOWLEDGE = Path("knowledge")
ARCHIVO_PEDIDOS = Path("pedidos.jsonl")


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
# Mona Pizza usa el agente para tomar pedidos. Estas funciones registran el pedido
# en un archivo local (pedidos.jsonl): el negocio revisa y confirma manualmente,
# el agente NO cobra ni confirma que el pedido ya esta en cocina.


def registrar_pedido(telefono: str, resumen: str, total: float | None, tipo_entrega: str) -> dict:
    """
    Guarda un pedido nuevo para que el local lo revise.

    Args:
        telefono: numero del cliente que hizo el pedido
        resumen: descripcion de lo pedido (productos, tamanios, cantidades)
        total: monto total del pedido en pesos, si se pudo calcular
        tipo_entrega: "delivery" o "retiro"

    Returns:
        El pedido guardado, con su id.
    """
    pedido = {
        "id": f"MP-{int(datetime.now(timezone.utc).timestamp())}",
        "telefono": telefono,
        "resumen": resumen,
        "total": total,
        "tipo_entrega": tipo_entrega,
        "estado": "pendiente_confirmacion",
        "creado_en": datetime.now(timezone.utc).isoformat(),
    }

    with open(ARCHIVO_PEDIDOS, "a", encoding="utf-8") as f:
        f.write(json.dumps(pedido, ensure_ascii=False) + "\n")

    logger.info(f"Pedido registrado: {pedido['id']} — {telefono} — {resumen}")
    return pedido


def listar_pedidos_de(telefono: str) -> list[dict]:
    """Devuelve los pedidos previos de un cliente, para que el agente pueda repetir uno."""
    if not ARCHIVO_PEDIDOS.exists():
        return []

    pedidos = []
    with open(ARCHIVO_PEDIDOS, "r", encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if not linea:
                continue
            pedido = json.loads(linea)
            if pedido.get("telefono") == telefono:
                pedidos.append(pedido)
    return pedidos
