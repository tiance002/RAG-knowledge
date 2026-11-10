"""Stored settings row shared by schema creation and runtime settings."""
from sqlmodel import Field, SQLModel


class PersistedSystemSettings(SQLModel, table=True):
    __tablename__ = 'system_settings'
    id: str = Field(default='system', primary_key=True)
    data: str = '{}'
