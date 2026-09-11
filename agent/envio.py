# agent/envio.py — Envio de mensajes con reintentos y limite de concurrencia
# Generado por AgentKit

"""
Todo lo que Lisa manda por WhatsApp pasa por aca, en vez de llamar directo a
proveedor.enviar_mensaje(). Resuelve dos problemas distintos:

1. UN ENVIO QUE FALLA SE PIERDE EN SILENCIO.

   Con Zernio no se notaba: si el webhook no terminaba bien, Zernio reintentaba el
   evento hasta 7 veces y el cliente igual terminaba recibiendo la respuesta. Evolution
   API no hace eso. Desde la migracion, un envio fallido —un corte de red de un segundo,
   el servidor de Evolution reiniciandose, un 429— significa que el cliente no recibe
   nada y nadie se entera. Por eso ahora reintentamos nosotros, con esperas crecientes.

2. NO HAY NINGUN FRENO A LA SALIDA.

   main.py encola una tarea por mensaje entrante: los mensajes de un mismo cliente se
   atienden en orden (candado por telefono), pero clientes distintos van en paralelo,
   sin tope. Con 20 conversaciones simultaneas salen 20 envios al mismo tiempo.

   El semaforo pone un maximo de envios en vuelo. A diferencia de "esperar 1 o 2
   segundos entre cada envio" —la receta tipica de las herramientas no-code—, esto NO
   agrega demora cuando hay poco trafico: solo frena cuando de verdad hay una avalancha.
   Un bot reactivo como Lisa casi nunca lo va a tocar; esta para el dia que si.
"""

import asyncio
import logging
import os

logger = logging.getLogger("agentkit")

# Cuantos mensajes pueden estar saliendo al mismo tiempo. 5 es holgado para un negocio
# chico y sigue estando muy por debajo de cualquier limite de la Cloud API de Meta.
MAX_ENVIOS_CONCURRENTES = int(os.getenv("MAX_ENVIOS_CONCURRENTES") or "5")

# Cuanto se espera entre intento e intento, en segundos. La cantidad de esperas define
# la cantidad de reintentos: con (1, 3, 9) son hasta 4 intentos en unos 13 segundos.
# Crecientes a proposito: si el problema es que estamos yendo muy rapido, insistir al
# mismo ritmo lo empeora.
ESPERAS_REINTENTO = (1.0, 3.0, 9.0)

_semaforo = asyncio.Semaphore(MAX_ENVIOS_CONCURRENTES)

# Contadores para GET /diagnostico: sirven para contestar "¿esto pasa de verdad o lo
# estamos imaginando?" sin tener que leer los logs de Railway.
estadisticas = {
    "enviados": 0,
    "reintentos": 0,
    "fallidos": 0,
    "esperando_lugar": 0,  # veces que un envio tuvo que esperar por el semaforo
}


async def enviar(proveedor, telefono: str, mensaje: str, contexto: dict | None = None, *, intentos: int | None = None) -> bool:
    """
    Manda un mensaje reintentando si falla, y respetando el tope de envios simultaneos.

    Args:
        intentos: cuantas veces intentarlo como maximo. None = 1 + len(ESPERAS_REINTENTO).
                  Se pasa 1 para los avisos de cortesia ("estoy teniendo problemas"):
                  si el canal esta caido, insistir 13 segundos con un mensaje que ya no
                  le sirve a nadie no tiene sentido.

    Retorna True si algun intento salio bien.
    """
    maximo = intentos if intentos is not None else len(ESPERAS_REINTENTO) + 1

    for intento in range(1, maximo + 1):
        # El semaforo se toma SOLO alrededor del envio, no de la espera entre reintentos.
        # Si se mantuviera tomado durante el backoff, cinco mensajes en reintento
        # bloquearian a todos los demas 13 segundos sin estar usando el canal para nada.
        if _semaforo.locked():
            estadisticas["esperando_lugar"] += 1
        async with _semaforo:
            try:
                ok = await proveedor.enviar_mensaje(telefono, mensaje, contexto)
            except Exception as e:  # noqa: BLE001 — un bug del adaptador no debe matar la tarea
                logger.error(f"Excepcion enviando a {telefono} (intento {intento}/{maximo}): {e}")
                ok = False

        if ok:
            estadisticas["enviados"] += 1
            if intento > 1:
                logger.info(f"Envio a {telefono} salio bien en el intento {intento}")
            return True

        if intento >= maximo:
            break

        # No se distingue el tipo de error: los adaptadores devuelven True/False, no
        # el codigo HTTP. Reintentar un rechazo definitivo (un numero invalido, por
        # ejemplo) cuesta unos segundos en una tarea de fondo y termina igual en
        # False; perder un mensaje por un corte de red de un segundo cuesta un
        # cliente. El intercambio esta claro a favor de reintentar.
        espera = ESPERAS_REINTENTO[intento - 1]
        estadisticas["reintentos"] += 1
        logger.warning(f"Fallo el envio a {telefono} (intento {intento}/{maximo}); se reintenta en {espera:g}s")
        await asyncio.sleep(espera)

    estadisticas["fallidos"] += 1
    logger.error(f"No se pudo enviar a {telefono} despues de {maximo} intento(s)")
    return False
