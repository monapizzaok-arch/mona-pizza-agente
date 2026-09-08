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
# guardarPedido, estadoApertura y menu son acciones publicas, no requieren login ni API key.
LOCALDB_API_URL = os.getenv("LOCALDB_API_URL") or "https://monapizza.com.ar/api/api.php"

# Fallback si por algun motivo el catalogo en vivo no trae costo_envio (no deberia pasar,
# pero mejor tener un numero razonable que romper el calculo del pedido).
COSTO_ENVIO_FALLBACK = 1000.0


def cargar_info_negocio() -> dict:
    """Carga la informacion del negocio desde config/business.yaml."""
    try:
        with open("config/business.yaml", "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        logger.error("config/business.yaml no encontrado")
        return {}


def obtener_horario() -> dict:
    """Retorna el horario de atencion del negocio (texto estatico, ver tambien consultar_estado_negocio)."""
    info = cargar_info_negocio()
    return {
        "horario": info.get("negocio", {}).get("horario", "No disponible"),
        "esta_abierto": True,  # TODO: calcular segun la hora actual y el horario
    }


async def consultar_estado_negocio() -> dict:
    """
    Consulta EN VIVO si el local esta abierto ahora mismo, segun el horario que carga
    el mostrador en admin.html (tabla `horarios` de LocalDB) — la misma consulta
    (accion publica estadoApertura) que hace la web de pedidos antes de dejar
    completar un pedido. brain.py la llama en cada mensaje para que Lisa sepa si puede
    tomar pedidos ahora mismo, sin depender de que el propio modelo calcule la hora.

    Devuelve {"abierto": bool, "mensaje": str, "retiro": bool, "delivery": bool}.
    Si la consulta falla (red caida, etc.), "abierto" es None: quien la use debe caer
    al horario estatico del prompt en vez de asumir que esta abierto o cerrado.
    """
    try:
        async with httpx.AsyncClient(timeout=8.0) as cliente:
            r = await cliente.get(
                LOCALDB_API_URL,
                params={"accion": "estadoApertura", "negocio": "monapizza"},
            )
        estado = r.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning(f"No se pudo consultar el estado de apertura en vivo: {e}")
        return {"abierto": None, "mensaje": "", "retiro": True, "delivery": True}

    if estado.get("pausado"):
        mensaje = estado.get("mensaje") or "Por el momento no estamos tomando pedidos por la web."
    elif estado.get("abierto"):
        cierra = (estado.get("cierra") or "")[:5]
        mensaje = f"Abierto ahora mismo{f', cerramos a las {cierra}' if cierra else ''}."
    else:
        apertura = estado.get("apertura")
        if apertura:
            cuando = estado.get("apertura_cuando")
            cuando_txt = "" if cuando in (None, "hoy") else f"{cuando} "
            mensaje = f"Cerrado en este momento. Abrimos {cuando_txt}a las {apertura[:5]}."
        else:
            mensaje = "Cerrado por el momento, todavia no hay horario de reapertura cargado."

    return {
        "abierto": bool(estado.get("abierto")) and not estado.get("pausado"),
        "mensaje": mensaje,
        "retiro": estado.get("retiro", True),
        "delivery": estado.get("delivery", True),
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
# Catalogo en vivo (productos, precios, stock, config publica)
# ════════════════════════════════════════════════════════════
#
# Mismo endpoint publico (accion=menu) que usa la web de pedidos para pintar el menu.
# brain.py lo consulta en cada mensaje para mostrarle a Lisa el catalogo REAL (con los
# ids que hay que usar en registrar_pedido) y que precios/stock esten siempre al dia,
# aunque el texto estatico de prompts.yaml haya quedado viejo.


async def obtener_menu(negocio: str = "monapizza") -> dict | None:
    """Trae categorias/items/precios/stock y la config publica (envio, alias, etc.)."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as cliente:
            r = await cliente.get(LOCALDB_API_URL, params={"accion": "menu", "negocio": negocio})
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        logger.warning(f"No se pudo consultar el catalogo en vivo: {e}")
        return None


def _fmt_precio(valor) -> str:
    try:
        return f"{float(valor):,.0f}".replace(",", ".")
    except (TypeError, ValueError):
        return str(valor)


def formatear_catalogo(menu: dict) -> str:
    """
    Arma el bloque compacto que brain.py inyecta en el prompt: un renglon por producto
    con su id real, cada variante con su precio, y si tiene stock. Ese id y el indice de
    la variante son justo lo que despues recibe registrar_pedido — nada de inventar
    nombres de producto nuevos.
    """
    lineas = []
    for cat in menu.get("categorias", []):
        etiquetas = cat.get("etiquetas", [])
        items = cat.get("items", [])
        if not items:
            continue
        lineas.append(f"[{cat.get('nombre', '')}]")
        for it in items:
            precios = it.get("precios", [])
            partes = []
            for i, p in enumerate(precios):
                etiqueta = etiquetas[i] if i < len(etiquetas) else f"v{i}"
                partes.append(f"{etiqueta}(idx {i})=${_fmt_precio(p)}")
            marca = "" if it.get("stock") == "ok" else "  [SIN STOCK]"
            lineas.append(f"  id={it.get('id')}  {it.get('nombre', '')}: {' / '.join(partes)}{marca}")
    return "\n".join(lineas)


def _indexar_catalogo(menu: dict) -> dict:
    """{producto_id: {..., "_etiquetas": [...]}} para resolver items rapido."""
    indice = {}
    for cat in menu.get("categorias", []):
        etiquetas = cat.get("etiquetas", [])
        for it in cat.get("items", []):
            indice[str(it.get("id"))] = {**it, "_etiquetas": etiquetas}
    return indice


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
# A diferencia de la version anterior, esta funcion NO confia en un precio que le pase
# Claude: cada item llega como referencia a un producto REAL del catalogo (producto_id +
# variante_index, los mismos que ve en el bloque "Catalogo en vivo" del prompt) y aca se
# vuelve a consultar el catalogo para sacar el precio y chequear el stock. Si algo no
# existe o esta sin stock, se rechaza TODO el pedido con el motivo — no se registra nada
# ni a medias.


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
        items: lista de productos, CADA UNO referenciando el catalogo real:
               {
                 "producto_id": "P1",              # id tal cual aparece en el catalogo en vivo
                 "variante_index": 1,              # 0 = primera variante de ese producto, 1 = segunda, etc.
                 "cantidad": 2,
                 "mitad_y_mitad_con": "Muzzarella", # opcional, solo para pizza mitad y mitad
               }
               Cada mitad de una pizza combinada es SU PROPIO producto real (su propio
               producto_id, variante_index apuntando a "Media"), no una linea combinada
               inventada tipo "Hawaiana+Muzzarela": van como dos items separados en la
               lista, cada uno con "mitad_y_mitad_con" apuntando al nombre del otro sabor
               (asi el ticket de cocina deja claro que las dos mitades son la MISMA pizza).
               Igual para packs de empanadas (docena/media docena/unidad): cada tamanio va
               como una linea separada del MISMO producto_id con distinto variante_index —
               no se inventa un producto "8 empanadas".
        domicilio: direccion de entrega, obligatoria si entrega es "Delivery"
        pago: "Efectivo" o "Transferencia" (por WhatsApp no se ofrece Tarjeta)
        nota: aclaraciones del cliente (sin cebolla, timbre roto, etc.)

    Returns:
        {"ok": True, "id": <id del pedido>, "seguimiento": <link>} si se registro bien.
        {"ok": False, "error": <motivo>} si algun producto no existe, no tiene stock, o
        el sistema lo rechazo (ej. local cerrado) — no se registra nada en ese caso.
    """
    if not items:
        return {"ok": False, "error": "El pedido no tiene productos."}

    menu = await obtener_menu("monapizza")
    if menu is None:
        return {"ok": False, "error": "No pude consultar el catálogo para confirmar precios y stock. Probá de nuevo en un minuto."}

    catalogo = _indexar_catalogo(menu)
    lineas = []

    for item in items:
        pid = str(item.get("producto_id", ""))
        try:
            vidx = int(item.get("variante_index", 0))
        except (TypeError, ValueError):
            vidx = 0
        try:
            cantidad = max(1, int(item.get("cantidad", 1)))
        except (TypeError, ValueError):
            cantidad = 1
        combo_con = (item.get("mitad_y_mitad_con") or "").strip()

        prod = catalogo.get(pid)
        if not prod:
            return {"ok": False, "error": f"No encontré el producto \"{pid}\" en el catálogo actual. Puede haber cambiado — fijate en el catálogo en vivo."}
        if prod.get("stock") != "ok":
            return {"ok": False, "error": f"\"{prod.get('nombre')}\" no tiene stock en este momento, no lo puedo agregar al pedido."}
        precios = prod.get("precios", [])
        if vidx < 0 or vidx >= len(precios):
            return {"ok": False, "error": f"\"{prod.get('nombre')}\" no tiene esa variante."}

        precio_unit = float(precios[vidx])
        etiquetas = prod.get("_etiquetas", [])
        variante_txt = etiquetas[vidx] if vidx < len(etiquetas) else ""
        # El combo_con es solo texto para el ticket de cocina — la mitad ya es un
        # producto real e independiente (su propio producto_id), asi que sus estadisticas
        # de venta y costo se cuentan como corresponde, no pisadas por la otra mitad.
        detalle = f"{variante_txt} (mitad y mitad con {combo_con})" if combo_con else (variante_txt or None)

        lineas.append(
            {
                "nombre": prod.get("nombre", ""),
                "detalle": detalle,
                "cantidad": cantidad,
                "precio": round(precio_unit * cantidad, 2),
                "producto_id": pid,
                "variante_index": vidx,
            }
        )

    subtotal = round(sum(l["precio"] for l in lineas), 2)
    config = menu.get("config", {})
    try:
        costo_envio_config = float(config.get("costo_envio") or COSTO_ENVIO_FALLBACK)
    except (TypeError, ValueError):
        costo_envio_config = COSTO_ENVIO_FALLBACK
    envio = costo_envio_config if "delivery" in entrega.lower() else 0.0
    total = subtotal + envio

    payload = {
        "nombre": nombre,
        "telefono": telefono,
        "entrega": entrega,
        "domicilio": domicilio,
        "pago": pago,
        "items": lineas,
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
