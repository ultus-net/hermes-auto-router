"""The provider contract every mail adapter satisfies.

A page returns its own cursor. ``resync`` that did not return one would either
stall or silently reset the frontier, and neither is recoverable from the
journal.

``find_by_stable_id`` exists for the crash after a move but before the
confirmation commit: the adapter locates the message by a stable provider field
rather than by the id it no longer has.
"""

from __future__ import annotations

from typing import Protocol

from .models import (
    ApplyResult,
    Decision,
    Folder,
    Page,
    Ref,
    Scope,
)


class Mailbox(Protocol):
    """What the sweep needs, and nothing a provider happens to offer."""

    def list_since(self, cursor: str | None, limit: int, scope: Scope) -> Page: ...

    def apply(self, ref: Ref, action: Decision) -> ApplyResult: ...

    def folders(self) -> list[Folder]: ...

    def ensure_folder(self, name: str) -> Folder: ...

    def resync(self, scope: Scope, limit: int) -> Page: ...

    def find_by_stable_id(self, stable_id: str, folders: list[str]) -> Ref | None: ...
