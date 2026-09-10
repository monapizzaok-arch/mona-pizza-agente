# agent/providers/evolution.py — Adaptador para Evolution API
# Generado por AgentKit

"""
Evolution API es un gateway de WhatsApp open source y auto-hosteado. A diferencia de
Zernio (servicio administrado sobre la Cloud API oficial de Meta), aca el servidor lo
corres vos: Evolution se conecta a WhatsApp por Baileys (sesion de WhatsApp Web, se
vincula escaneando un QR) o contra la Cloud API oficial, segun como lo configures.

Documentacion: https://docs.evolutionfoundation.com.br/evolution-api

Tres diferencias con Zernio que se notan en este archivo:

1. Los mensajes SALIENTES no tienen un campo tipo "sentVia" que diga quien los mando.
   Llegan por el mismo evento que los entrantes (messages.upsert) con key.fromMe=true,
   sin distinguir si los mando esta API o una persona escribiendo desde el celular. Por
   eso el adaptador se acuerda de los ids que envio el mismo (ver _ids_propios): lo que
   vuelve con un id que no mandamos nosotros es, por descarte, alguien del local
   escribiendo a mano.

2. Evolution NO firma los webhooks (no hay HMAC como el X-Zernio-Signature). Lo unico
   que se puede chequear es el campo "apikey" que el propio payload trae. Ver
   verificar_firma().

3. El formato del body para enviar cambio entre v1 y v2 (textMessage anidado vs text
   plano) y hay documentacion viva de las dos formas. enviar_mensaje() manda el formato
   v2 y, si el servidor lo rechaza por formato, reintenta con el viejo.
"""

import logging
import os
import time

import httpx
from fastapi import Request

from agent.providers.base import MensajeEntrante, ProveedorWhatsApp

logger = logging.getLogger("agentkit")

# Cuanto se recuerda un id de mensaje propio para reconocer su eco. El eco llega en
# segundos; 10 minutos es holgado de sobra y mantiene el diccionario chico.
VENTANA_ECO_SEGUNDOS = 600


class ProveedorEvolution(ProveedorWhatsApp):
    """Proveedor de WhatsApp usando Evolution API (auto-hosteado)."""

    def __init__(self):
        self.base_url = (os.getenv("EVOLUTION_API_URL") or "").rstrip("/")
        self.api_key = os.getenv("EVOLUTION_API_KEY", "")
        self.instance = os.getenv("EVOLUTION_INSTANCE", "")

        # Ids de mensajes que mando ESTE proceso, con el momento en que se mandaron.
        # Sirve para reconocer el eco de los propios envios y no confundirlo con
        # alguien del local escribiendo a mano (ver parsear_webhook).
        self._ids_propios: dict[str, float] = {}

        # Se aprende en el primer envio exitoso: True si el servidor acepta el formato
        # plano de v2, False si hubo que caer al anidado de v1.
        self._formato_v2 = True

        # Token propio de la instancia (el que Evolution manda en los webhooks). Se
        # consulta a demanda la primera vez que hace falta. Ver _token_instancia().
        self._token_cache: str | None = None

        if not self.base_url:
            logger.warning("EVOLUTION_API_URL no esta configurada: el agente no va a poder responder")
        if not self.api_key:
            logger.warning("EVOLUTION_API_KEY no esta configurada: el agente no va a poder responder")
        if not self.instance:
            logger.warning("EVOLUTION_INSTANCE no esta configurada: el agente no va a poder responder")

    # ── Recibir ──────────────────────────────────────────────────────────

    async def verificar_firma(self, request: Request) -> bool:
        """
        Evolution API no firma los webhooks: no hay equivalente al HMAC de Zernio.

        Lo unico verificable es el campo "apikey" que el propio payload incluye. No es
        una firma criptografica (no prueba que el cuerpo no fue modificado), pero si
        evita que cualquiera que adivine la URL publica pueda inyectar mensajes falsos
        —y con eso, pedidos falsos en LocalDB—.

        OJO con cual apikey manda: NO es la global (AUTHENTICATION_API_KEY), es el
        token propio de la instancia, que es otro string. Aceptar solo la global hace
        que se rechacen TODOS los mensajes con un 401, que es exactamente lo que paso
        la primera vez que se conecto esto. Por eso valen las dos, y el token de la
        instancia se consulta solo (se cachea) en vez de pedir otra variable de entorno.
        """
        if not self.api_key:
            return True  # sin clave configurada no hay nada contra que comparar

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001 — cuerpo no-JSON: que lo rechace el parseo
            return True

        recibida = payload.get("apikey") if isinstance(payload, dict) else None
        if not recibida:
            logger.warning(
                "El webhook de Evolution no trae el campo 'apikey': no se puede verificar "
                "su origen. Ojo: esta URL acepta mensajes de cualquiera que la conozca."
            )
            return True

        if recibida == self.api_key:
            return True

        token_instancia = await self._token_instancia()
        if token_instancia is None:
            # No se pudo averiguar contra que comparar. Se deja pasar avisando: para un
            # negocio es peor tragarse todos los mensajes de los clientes en silencio
            # que aceptar uno falso en un chequeo que, de movida, es best-effort.
            logger.warning(
                "No se pudo obtener el token de la instancia para verificar el webhook: "
                "se deja pasar sin verificar."
            )
            return True

        if recibida == token_instancia:
            return True

        logger.warning(
            f"Webhook de Evolution con apikey desconocida (empieza con "
            f"'{str(recibida)[:6]}...'): rechazado"
        )
        return False

    async def _token_instancia(self) -> str | None:
        """
        Token propio de la instancia, que es el que Evolution manda en los webhooks.

        Se consulta una sola vez y queda cacheado. Devuelve None si no se pudo obtener.
        """
        if self._token_cache is not None:
            return self._token_cache

        try:
            async with httpx.AsyncClient(timeout=10.0) as cliente:
                r = await cliente.get(
                    f"{self.base_url}/instance/fetchInstances",
                    headers={"apikey": self.api_key},
                )
            instancias = r.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.warning(f"No se pudo consultar el token de la instancia: {e}")
            return None

        for inst in instancias if isinstance(instancias, list) else [instancias]:
            i = inst.get("instance", inst) if isinstance(inst, dict) else {}
            if (i.get("name") or i.get("instanceName")) == self.instance:
                token = i.get("token") or i.get("hash") or i.get("apikey")
                if token:
                    self._token_cache = str(token)
                    return self._token_cache
        return None

    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Normaliza el evento messages.upsert de Evolution API."""
        payload = await request.json()

        # Evolution manda el nombre del evento en varias formas segun version y
        # configuracion: "messages.upsert", "messages-upsert", "MESSAGES_UPSERT".
        evento = str(payload.get("event") or "").lower().replace("_", ".").replace("-", ".")
        if evento != "messages.upsert":
            logger.debug(f"Evento ignorado: {payload.get('event')}")
            return []

        datos = payload.get("data")
        # Segun version, "data" es un mensaje suelto o una lista de mensajes.
        lista = datos if isinstance(datos, list) else [datos]

        mensajes: list[MensajeEntrante] = []
        for dato in lista:
            if not isinstance(dato, dict):
                continue
            msg = self._parsear_mensaje(dato, payload)
            if msg is not None:
                mensajes.append(msg)
        return mensajes

    def _parsear_mensaje(self, dato: dict, payload: dict) -> MensajeEntrante | None:
        """Convierte UN mensaje del payload de Evolution en MensajeEntrante."""
        clave = dato.get("key") or {}
        remote_jid = str(clave.get("remoteJid") or "")

        # Los JID de grupo terminan en "@g.us". Lisa atiende conversaciones 1 a 1: si
        # el numero esta en un grupo, no tiene que ponerse a contestar ahi.
        if remote_jid.endswith("@g.us"):
            logger.debug(f"Mensaje de grupo ignorado: {remote_jid}")
            return None

        # WhatsApp esta migrando al direccionamiento "LID": el remoteJid deja de ser el
        # telefono y pasa a ser un id opaco ("145861417930892@lid"), con el numero real
        # en remoteJidAlt. Si se toma el LID como telefono, TODO se rompe en silencio:
        # la respuesta se manda a un numero que no existe, el chequeo del numero admin
        # no matchea nunca, y la memoria queda guardada bajo un id que no vuelve a
        # aparecer. Por eso remoteJidAlt tiene prioridad cuando viene.
        jid_telefono = str(clave.get("remoteJidAlt") or "") or remote_jid
        if jid_telefono.endswith("@lid"):
            logger.warning(
                f"Mensaje con direccionamiento LID y sin remoteJidAlt ({jid_telefono}): "
                "no hay telefono real, el agente no va a poder responderle."
            )
        telefono = jid_telefono.split("@", 1)[0].split(":", 1)[0].lstrip("+")

        texto = self._extraer_texto(dato.get("message") or {})
        if not texto:
            # Audio, imagen, sticker, etc: por ahora el agente solo maneja texto.
            return None

        mensaje_id = str(clave.get("id") or "")
        es_propio = bool(clave.get("fromMe"))

        # Un saliente que NO salio de esta API es alguien del local escribiendo a mano
        # (desde el celular vinculado o desde el panel de Evolution).
        enviado_por_humano = es_propio and not self._es_eco_propio(mensaje_id)

        return MensajeEntrante(
            telefono=telefono,
            texto=texto,
            mensaje_id=mensaje_id,
            es_propio=es_propio,
            enviado_por_humano=enviado_por_humano,
            contexto={
                # Evolution no manda un id de evento propio: se usa el del mensaje para
                # deduplicar, que es lo que main.py espera igual.
                "evento_id": mensaje_id,
                "instance": payload.get("instance") or self.instance,
            },
        )

    @staticmethod
    def _extraer_texto(message: dict) -> str:
        """
        Saca el texto de un mensaje de WhatsApp.

        Baileys lo guarda en lugares distintos segun el tipo: "conversation" es el texto
        pelado, "extendedTextMessage" es el que tiene contexto (una respuesta citada, un
        link con preview). Tambien se contempla el epigrafe de una imagen o video, que
        es texto que el cliente efectivamente escribio.
        """
        if not isinstance(message, dict):
            return ""
        directo = message.get("conversation")
        if isinstance(directo, str) and directo.strip():
            return directo
        for clave in ("extendedTextMessage", "imageMessage", "videoMessage"):
            sub = message.get(clave)
            if isinstance(sub, dict):
                texto = sub.get("text") or sub.get("caption")
                if isinstance(texto, str) and texto.strip():
                    return texto
        return ""

    def _es_eco_propio(self, mensaje_id: str) -> bool:
        """True si ESTE proceso mando ese mensaje (es el eco de su propio envio)."""
        self._limpiar_ids_viejos()
        return mensaje_id in self._ids_propios

    def _limpiar_ids_viejos(self):
        limite = time.time() - VENTANA_ECO_SEGUNDOS
        for mid in [m for m, t in self._ids_propios.items() if t < limite]:
            self._ids_propios.pop(mid, None)

    # ── Enviar ───────────────────────────────────────────────────────────

    async def enviar_mensaje(
        self, telefono: str, mensaje: str, contexto: dict | None = None
    ) -> bool:
        """Manda un mensaje de texto por Evolution API."""
        contexto = contexto or {}
        instance = contexto.get("instance") or self.instance

        if not self.base_url or not self.api_key or not instance:
            logger.error("No se puede enviar: faltan EVOLUTION_API_URL, EVOLUTION_API_KEY o EVOLUTION_INSTANCE")
            return False

        url = f"{self.base_url}/message/sendText/{instance}"
        headers = {"apikey": self.api_key, "Content-Type": "application/json"}

        # v2 usa el body plano; v1 anidaba el texto en "textMessage". Se intenta el
        # formato aprendido y, si el servidor lo rechaza POR el formato, se prueba el
        # otro y se recuerda cual funciono.
        for intento, usar_v2 in enumerate(([self._formato_v2, not self._formato_v2])):
            cuerpo = (
                {"number": telefono, "text": mensaje}
                if usar_v2
                else {"number": telefono, "textMessage": {"text": mensaje}}
            )
            try:
                async with httpx.AsyncClient(timeout=30.0) as cliente:
                    r = await cliente.post(url, json=cuerpo, headers=headers)
            except httpx.HTTPError as e:
                logger.error(f"Error de red hablando con Evolution API: {e}")
                return False

            if r.status_code in (200, 201):
                if usar_v2 is not self._formato_v2:
                    logger.info(
                        f"Evolution acepto el formato {'v2 (text plano)' if usar_v2 else 'v1 (textMessage)'}; "
                        "se usa ese de ahora en mas."
                    )
                    self._formato_v2 = usar_v2
                self._registrar_envio_propio(r)
                return True

            # 400 puede ser "formato invalido" (probamos el otro) o el mensaje en si
            # esta mal. Solo se reintenta una vez, con el formato alternativo.
            if r.status_code == 400 and intento == 0:
                logger.warning(f"Evolution rechazo el formato del envio [400]: {r.text[:300]}. Se reintenta con el otro formato.")
                continue

            logger.error(f"Evolution rechazo el envio [{r.status_code}]: {r.text[:500]}")
            return False

        return False

    def _registrar_envio_propio(self, respuesta: httpx.Response):
        """
        Guarda el id que Evolution le asigno al mensaje recien enviado.

        Es lo que despues permite reconocer el eco de este envio cuando vuelva por el
        webhook con fromMe=true, y no confundirlo con alguien escribiendo a mano.
        """
        try:
            cuerpo = respuesta.json()
        except ValueError:
            logger.warning("Evolution acepto el envio pero no devolvio JSON: no se pudo registrar el id")
            return

        mensaje_id = ((cuerpo or {}).get("key") or {}).get("id")
        if mensaje_id:
            self._ids_propios[str(mensaje_id)] = time.time()
        else:
            logger.warning(
                "La respuesta de Evolution no trae key.id: el eco de este mensaje se "
                "podria confundir con un mensaje escrito a mano."
            )

    # ── Diagnostico ──────────────────────────────────────────────────────

    async def verificar_conexion(self) -> tuple[bool, str]:
        """Pregunta a Evolution si la instancia esta conectada a WhatsApp."""
        if not self.base_url or not self.api_key or not self.instance:
            return False, "Faltan EVOLUTION_API_URL, EVOLUTION_API_KEY o EVOLUTION_INSTANCE"

        try:
            async with httpx.AsyncClient(timeout=15.0) as cliente:
                r = await cliente.get(
                    f"{self.base_url}/instance/connectionState/{self.instance}",
                    headers={"apikey": self.api_key},
                )
        except httpx.HTTPError as e:
            return False, f"No se pudo contactar a Evolution API: {e}"

        if r.status_code != 200:
            return False, f"Evolution respondio {r.status_code}: {r.text[:200]}"

        try:
            estado = (r.json().get("instance") or {}).get("state") or r.json().get("state")
        except ValueError:
            return False, "Evolution devolvio una respuesta no-JSON en connectionState"

        # "open" es conectado; "connecting"/"close" es que hay que escanear el QR.
        if estado == "open":
            return True, f"Instancia '{self.instance}' conectada"
        return False, f"Instancia '{self.instance}' NO conectada (estado: {estado}). Escanea el QR."
