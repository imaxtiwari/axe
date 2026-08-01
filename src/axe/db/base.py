"""SQLAlchemy declarative base for AXE."""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Base class for all AXE SQLAlchemy models.

    Models default to ``isolation_scope = "pm"`` and are automatically
    filtered by the request context's ``pm_id`` (or ``fund_entity_id`` when no
    ``pm_id`` column exists). Mark a model as ``isolation_scope = "global"``
    to opt out of automatic row-level filtering (e.g., master reference tables).
    """

    isolation_scope: str = "pm"
