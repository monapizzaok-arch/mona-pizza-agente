# agent/memory.py — Memoria de conversaciones
# Generado por AgentKit

"""
Guarda el historial de cada conversacion por numero de telefono, y lleva registro de
que eventos de webhook ya se atendieron.

SQLite en local, PostgreSQL en produccion.
"""

import logging
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from sqlalchemy import DateTime, Integer, String, Text, delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

load_dotenv()
logger = logging.getLogger("agentkit")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./agentkit.db")

# Railway entrega la URL de PostgreSQL con el esquema "postgresql://" (o "postgres://").
# SQLAlchemy en modo asincrono necesita que el driver sea explicito.
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)

# En produccion, SQLite vive dentro del contenedor y el disco del contenedor es efimero:
# cada redespliegue borra el historial de todas las conversaciones. Avisarlo fuerte, porque
# el agente arranca igual y el problema recien se nota cuando un cliente vuelve a escribir.
if DATABASE_URL.startswith("sqlite") and os.getenv("ENVIRONMENT") == "production":
    logger.warning(
        "Estas en produccion con SQLite. El historial se va a borrar en cada redespliegue. "
        "Agrega PostgreSQL y configura DATABASE_URL para que el agente recuerde a sus clientes."
    )

# SQLite solo deja UN escritor a la vez en todo el archivo (no por fila, por tabla).
# Sin "timeout", una segunda escritura que llega mientras otra sigue abierta explota
# al toque con "database is locked" en vez de esperar. Con varias conversaciones
# entrando a la vez (o el guardado de un mensaje manual del inbox corriendo casi
# junto con el de un mensaje normal), esto pasa de verdad. El timeout hace que
# espere hasta 15s a que se libere en vez de fallar de una. No aplica a Postgres
# (produccion real), que maneja escrituras concurrentes sin este problema.
_connect_args = {"timeout": 15} if DATABASE_URL.startswith("sqlite") else {}

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True, connect_args=_connect_args)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def ahora() -> datetime:
    """Hora actual en UTC, con zona horaria."""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Mensaje(Base):
    """Un mensaje del historial de conversacion."""

    __tablename__ = "mensajes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    role: Mapped[str] = mapped_column(String(20))  # "user" o "assistant"
    content: Mapped[str] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


class ConversacionTelefono(Base):
    """
    A que telefono corresponde cada conversation_id de Zernio.

    Se completa cada vez que un cliente escribe (ese momento es el unico en que main.py
    tiene los dos datos juntos). Sirve para resolver el telefono cuando llega un mensaje
    SALIENTE escrito a mano desde el inbox: ese evento trae el conversation_id, pero no
    el telefono del cliente (el "sender" ahi es el lado del negocio, no el cliente).
    """

    __tablename__ = "conversaciones_telefono"

    conversation_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    telefono: Mapped[str] = mapped_column(String(50), index=True)
    actualizado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


class EstadoAgente(Base):
    """
    Un unico renglon (id=1) con el estado operativo del agente. Hoy solo guarda si
    esta pausado; lo prende/apaga el comando que manda ADMIN_WHATSAPP_NUMBER (ver
    main.py). Va en la base, no en una variable en memoria, para que sobreviva a un
    reinicio o redespliegue del servidor.
    """

    __tablename__ = "estado_agente"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pausado: Mapped[bool] = mapped_column(default=False)
    actualizado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


class AvisoDelDia(Base):
    """
    Avisos que carga el local por WhatsApp (comando /aviso) para que Lisa los tenga en
    cuenta al tomar pedidos -- "nos quedamos sin rucula hoy", "ofrece primero la promo
    x3" -- sin tocar config/prompts.yaml ni hacer un deploy. Valen SOLO el dia en que se
    cargaron (obtener_avisos_vigentes filtra por fecha), asi que si el local se olvida
    de borrarlos no arrastran para el dia siguiente.
    """

    __tablename__ = "avisos_del_dia"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    texto: Mapped[str] = mapped_column(Text)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora)


class EventoProcesado(Base):
    """
    Eventos de webhook que ya se atendieron.

    Los proveedores entregan "al menos una vez": el mismo evento puede llegar dos veces.
    Sin esta tabla, el cliente recibiria la misma respuesta repetida.
    """

    __tablename__ = "eventos_procesados"

    evento_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    creado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=ahora, index=True)


async def inicializar_db():
    """Crea las tablas si no existen."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def marcar_evento_procesado(evento_id: str) -> bool:
    """
    Registra un evento. Retorna True si es nuevo, False si ya se habia procesado.

    La unicidad la garantiza la base de datos (clave primaria), no una consulta previa:
    asi dos webhooks que llegan al mismo tiempo no pasan los dos.
    """
    if not evento_id:
        return True  # sin id no podemos deduplicar: se procesa

    async with async_session() as session:
        session.add(EventoProcesado(evento_id=evento_id, creado_en=ahora()))
        try:
            await session.commit()
            return True
        except IntegrityError:
            await session.rollback()
            return False


async def liberar_evento(evento_id: str):
    """
    Borra la marca de un evento para que el reintento del proveedor SI se procese.

    Se usa cuando el mensaje se marco como procesado pero despues fallo el envio de la
    respuesta. Sin esto, el reintento se descartaria por duplicado y el cliente se
    quedaria sin respuesta para siempre.
    """
    if not evento_id:
        return
    async with async_session() as session:
        await session.execute(delete(EventoProcesado).where(EventoProcesado.evento_id == evento_id))
        await session.commit()


async def limpiar_eventos_viejos(dias: int = 7):
    """Borra los eventos de hace mas de N dias para que la tabla no crezca sin fin."""
    limite = ahora() - timedelta(days=dias)
    async with async_session() as session:
        resultado = await session.execute(
            delete(EventoProcesado).where(EventoProcesado.creado_en < limite)
        )
        await session.commit()
    if resultado.rowcount:
        logger.info(f"Se limpiaron {resultado.rowcount} eventos de mas de {dias} dias")


async def guardar_mensaje(telefono: str, role: str, content: str):
    """Guarda un mensaje en el historial de esa conversacion."""
    async with async_session() as session:
        session.add(Mensaje(telefono=telefono, role=role, content=content, timestamp=ahora()))
        await session.commit()


async def obtener_historial(telefono: str, limite: int = 20) -> list[dict]:
    """
    Devuelve los ultimos N mensajes de una conversacion, en orden cronologico.

    Se ordena por id y no por timestamp: dos mensajes guardados en el mismo instante
    tienen el mismo timestamp, y el orden entre ellos quedaria librado al azar.
    """
    async with async_session() as session:
        resultado = await session.execute(
            select(Mensaje)
            .where(Mensaje.telefono == telefono)
            .order_by(Mensaje.id.desc())
            .limit(limite)
        )
        mensajes = list(resultado.scalars().all())

    mensajes.reverse()  # vienen del mas nuevo al mas viejo: los damos vuelta

    # La API de Claude exige que el historial empiece con un mensaje del usuario.
    # Si por un error anterior quedo un "assistant" suelto al principio, lo sacamos.
    while mensajes and mensajes[0].role != "user":
        mensajes.pop(0)

    return [{"role": m.role, "content": m.content} for m in mensajes]


async def limpiar_historial(telefono: str):
    """Borra todo el historial de una conversacion."""
    async with async_session() as session:
        await session.execute(delete(Mensaje).where(Mensaje.telefono == telefono))
        await session.commit()


async def guardar_conversacion(conversation_id: str, telefono: str):
    """Asocia un conversation_id de Zernio con el telefono del cliente que escribio."""
    if not conversation_id or not telefono:
        return
    async with async_session() as session:
        existente = await session.get(ConversacionTelefono, conversation_id)
        if existente:
            existente.telefono = telefono
            existente.actualizado_en = ahora()
        else:
            session.add(
                ConversacionTelefono(conversation_id=conversation_id, telefono=telefono, actualizado_en=ahora())
            )
        await session.commit()


async def obtener_telefono_de_conversacion(conversation_id: str) -> str | None:
    """Resuelve el telefono del cliente a partir del conversation_id de Zernio."""
    if not conversation_id:
        return None
    async with async_session() as session:
        fila = await session.get(ConversacionTelefono, conversation_id)
        return fila.telefono if fila else None


async def esta_pausado() -> bool:
    """True si el admin pauso a Lisa: no responde pedidos, solo guarda lo que llega."""
    async with async_session() as session:
        fila = await session.get(EstadoAgente, 1)
        return bool(fila.pausado) if fila else False


async def set_pausado(valor: bool):
    """Prende o apaga la pausa. Lo dispara el comando de ADMIN_WHATSAPP_NUMBER."""
    async with async_session() as session:
        fila = await session.get(EstadoAgente, 1)
        if fila:
            fila.pausado = valor
            fila.actualizado_en = ahora()
        else:
            session.add(EstadoAgente(id=1, pausado=valor, actualizado_en=ahora()))
        await session.commit()


# Zona horaria del local (America/Argentina/Salta, UTC-3 todo el ano, sin horario de
# verano) -- coincide con la que fija api.php de LocalDB. "Vigente hoy" se decide con
# la fecha ahi, no en UTC: un aviso cargado a las 23:50 hora Argentina ya seria "manana"
# en UTC, y se perderia de dia por una diferencia de huso horario, no de tiempo real.
TZ_NEGOCIO = timezone(timedelta(hours=-3))


async def agregar_aviso(texto: str):
    """Carga un aviso del dia (comando /aviso <texto> de ADMIN_WHATSAPP_NUMBER)."""
    async with async_session() as session:
        session.add(AvisoDelDia(texto=texto, creado_en=ahora()))
        await session.commit()


async def obtener_avisos_vigentes() -> list[str]:
    """Avisos cargados HOY (hora Argentina), en el orden en que se cargaron."""
    hoy = ahora().astimezone(TZ_NEGOCIO).date()
    async with async_session() as session:
        resultado = await session.execute(select(AvisoDelDia).order_by(AvisoDelDia.id))
        avisos = list(resultado.scalars().all())
    return [a.texto for a in avisos if a.creado_en.astimezone(TZ_NEGOCIO).date() == hoy]


async def borrar_avisos():
    """Borra TODOS los avisos guardados (comando /aviso borrar)."""
    async with async_session() as session:
        await session.execute(delete(AvisoDelDia))
        await session.commit()


async def limpiar_avisos_viejos(dias: int = 3):
    """Borra avisos de hace mas de N dias para que la tabla no crezca sin fin."""
    limite = ahora() - timedelta(days=dias)
    async with async_session() as session:
        await session.execute(delete(AvisoDelDia).where(AvisoDelDia.creado_en < limite))
        await session.commit()
