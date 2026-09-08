# agent/providers/base.py — Clase base para proveedores de WhatsApp
# Generado por AgentKit

"""
Define la interfaz comun que todos los proveedores de WhatsApp implementan.
Gracias a esto, main.py no sabe ni le importa con cual estas conectado.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from fastapi import Request


@dataclass
class MensajeEntrante:
    """Mensaje normalizado: el mismo formato sin importar el proveedor."""

    telefono: str            # Numero del remitente, solo digitos, sin "+"
    texto: str               # Contenido del mensaje
    mensaje_id: str          # Id del mensaje en la plataforma
    es_propio: bool          # True si lo mando el agente (se ignora)
    contexto: dict = field(default_factory=dict)
    # "contexto" lleva lo que cada proveedor necesita para poder responder:
    #   evento_id       -> id unico del evento, para no procesar dos veces lo mismo
    #   conversation_id -> Zernio: en que conversacion hay que responder
    #   account_id      -> Zernio: que cuenta de WhatsApp recibio el mensaje


class ProveedorWhatsApp(ABC):
    """Interfaz que cada proveedor de WhatsApp debe implementar."""

    @abstractmethod
    async def parsear_webhook(self, request: Request) -> list[MensajeEntrante]:
        """Extrae y normaliza los mensajes del payload del webhook."""
        ...

    @abstractmethod
    async def enviar_mensaje(
        self, telefono: str, mensaje: str, contexto: dict | None = None
    ) -> bool:
        """Envia un mensaje de texto. Retorna True si salio bien."""
        ...

    async def verificar_firma(self, request: Request) -> bool:
        """
        Confirma que el webhook viene de verdad del proveedor.
        Por defecto acepta todo; cada proveedor lo implementa segun su esquema.
        """
        return True

    async def validar_webhook(self, request: Request) -> str | None:
        """Verificacion GET del webhook. Solo Meta la usa. Retorna la respuesta o None."""
        return None

    async def verificar_conexion(self) -> tuple[bool, str]:
        """Chequea que las credenciales sirvan. Retorna (ok, mensaje_legible)."""
        return True, "Este proveedor no expone un chequeo de conexion"
