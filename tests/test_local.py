# tests/test_local.py — Simulador de chat en terminal
# Generado por AgentKit

"""
Prueba tu agente sin necesitar WhatsApp.
Simula una conversacion en la terminal.
"""

import asyncio
import os
import sys

# Agregar el directorio raiz al path para poder importar "agent"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Las respuestas del agente pueden traer emojis (WhatsApp es asi). La consola de
# Windows por default usa cp1252, que no los sabe imprimir y tira UnicodeEncodeError.
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from agent.brain import generar_respuesta  # noqa: E402
from agent.memory import (  # noqa: E402
    guardar_mensaje,
    inicializar_db,
    limpiar_historial,
    obtener_historial,
)

TELEFONO_TEST = "test-local-001"


async def main():
    """Loop principal del chat de prueba."""
    await inicializar_db()

    print()
    print("=" * 55)
    print("   AgentKit — Test Local")
    print("=" * 55)
    print()
    print("  ADVERTENCIA: si confirmas un pedido completo (productos, entrega,")
    print("  forma de pago), Lisa lo va a registrar DE VERDAD en el sistema de")
    print("  Mona Pizza — aparece en cocina y en el TPV como cualquier otro")
    print("  pedido real. No hay ambiente de pruebas separado.")
    print()
    print("  Escribe mensajes como si fueras un cliente.")
    print("  Comandos especiales:")
    print("    'limpiar'  — borra el historial")
    print("    'salir'    — termina el test")
    print()
    print("-" * 55)
    print()

    while True:
        try:
            mensaje = input("Tu: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n\nTest finalizado.")
            break

        if not mensaje:
            continue

        if mensaje.lower() == "salir":
            print("\nTest finalizado.")
            break

        if mensaje.lower() == "limpiar":
            await limpiar_historial(TELEFONO_TEST)
            print("[Historial borrado]\n")
            continue

        # El historial se lee ANTES de guardar (brain.py agrega el mensaje actual)
        historial = await obtener_historial(TELEFONO_TEST)

        print("\nAgente: ", end="", flush=True)
        respuesta, es_respuesta_real = await generar_respuesta(mensaje, historial, telefono=TELEFONO_TEST)
        print(respuesta)
        print()

        # Igual que en produccion: los avisos tecnicos no entran al historial
        if es_respuesta_real:
            await guardar_mensaje(TELEFONO_TEST, "user", mensaje)
            await guardar_mensaje(TELEFONO_TEST, "assistant", respuesta)


if __name__ == "__main__":
    asyncio.run(main())
