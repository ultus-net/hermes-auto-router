"""The mail organiser seam: journal, rules, policy, and provider adapters.

``register`` attaches the durable store and nothing else yet. Hermes is imported
inside that function, so importing the root plugin stays light and a mail
failure reaches the root guard instead of the import machinery.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

#: The durable store name. It outlives the D11 plugin rename on purpose.
STORE_NAME = "hermes-personal"

_connection: sqlite3.Connection | None = None


def connection() -> sqlite3.Connection | None:
    """The attached store, or ``None`` before ``register`` succeeds."""

    return _connection


def register(ctx) -> None:
    """Attach the durable store. It registers no command or tool yet.

    ``ctx`` is unused here; later phases register their commands and tools
    through it. One load attaches one connection, because Hermes may reload the
    plugin in the same process.
    """

    global _connection
    if _connection is not None:
        return

    from .store import attach_plugin_store

    _connection = attach_plugin_store(STORE_NAME)
    logger.info("mail seam attached store %s", STORE_NAME)
