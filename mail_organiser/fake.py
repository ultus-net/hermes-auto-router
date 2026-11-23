"""An in-memory ``Mailbox`` for the suite and for a credential-free dry run.

The fake keeps the same contract as a real adapter: it returns refs rather than
bodies from ``list_since``, it carries the cursor on the page, and it can be
told to fail or to answer ``unknown`` for selected messages.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import replace

from .models import (
    ApplyResult,
    Decision,
    Folder,
    Message,
    Outcome,
    Page,
    Ref,
    Scope,
    Verb,
)


class FakeMailbox:
    def __init__(
        self,
        messages: Iterable[Message] = (),
        folders: Iterable[Folder] = (),
        *,
        fail_ids: Iterable[str] = (),
        unknown_ids: Iterable[str] = (),
    ) -> None:
        self._messages = list(messages)
        self._folders = list(folders)
        self.fail_ids = set(fail_ids)
        self.unknown_ids = set(unknown_ids)
        self.applied: list[tuple[Ref, Decision]] = []

    @property
    def messages(self) -> tuple[Message, ...]:
        return tuple(self._messages)

    def list_since(self, cursor: str | None, limit: int, scope: Scope) -> Page:
        start = int(cursor) if cursor else 0
        window = self._messages[start : start + limit]
        end = start + len(window)
        return Page(
            messages=tuple(window),
            scope=scope,
            next_cursor=str(end),
            high_water=str(len(self._messages) - 1) if self._messages else None,
            complete=end >= len(self._messages),
        )

    def apply(self, ref: Ref, action: Decision) -> ApplyResult:
        if ref.message_id in self.fail_ids:
            return ApplyResult(outcome=Outcome.ERROR, error="fake adapter failure")
        if ref.message_id in self.unknown_ids:
            return ApplyResult(outcome=Outcome.UNKNOWN)

        self.applied.append((ref, action))
        if action.verb in (Verb.FILE, Verb.LABEL) and action.target:
            self._move(ref, action.target)
            return ApplyResult(outcome=Outcome.APPLIED, post_ref=ref.message_id)
        if action.verb is Verb.NOOP:
            return ApplyResult(outcome=Outcome.NOOP, post_ref=ref.message_id)
        return ApplyResult(outcome=Outcome.APPLIED, post_ref=ref.message_id)

    def folders(self) -> list[Folder]:
        return list(self._folders)

    def ensure_folder(self, name: str) -> Folder:
        for folder in self._folders:
            if folder.name == name:
                return folder
        folder = Folder(name=name, folder_id=f"label-{len(self._folders) + 1}")
        self._folders.append(folder)
        return folder

    def resync(self, scope: Scope, limit: int) -> Page:
        return self.list_since(None, limit, scope)

    def find_by_stable_id(self, stable_id: str, folders: Sequence[str]) -> Ref | None:
        for item in self._messages:
            if item.ref.stable_id != stable_id:
                continue
            if folders and not any(name in item.folder_ids for name in folders):
                continue
            return item.ref
        return None

    def _move(self, ref: Ref, target: str) -> None:
        for index, item in enumerate(self._messages):
            if item.ref.stable_id == ref.stable_id:
                self._messages[index] = replace(item, folder_ids=(target,))
                return
