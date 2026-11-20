"""Backward-compatible import path for the former MySQL-only data source."""

from .database_source import (
    DatabaseConnectionExpired,
    DatabaseSourceError,
    clear_connections,
    connect_and_discover,
    fetch_table_csv,
    get_connection_metadata,
    get_connection_tables,
)

__all__ = [
    "DatabaseConnectionExpired",
    "DatabaseSourceError",
    "clear_connections",
    "connect_and_discover",
    "fetch_table_csv",
    "get_connection_metadata",
    "get_connection_tables",
]
