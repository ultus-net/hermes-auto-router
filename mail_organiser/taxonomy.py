"""Taxonomy bootstrap: sample, propose, force risk, approve.

The classifier may only reach low-risk folders, so risk is decided here rather
than by the proposer. A name or description that names a D14 concept is
sensitive whatever the model said, and a missing risk fails closed.

The sample is bounded and read straight from the adapter, never through the
sweep, so bootstrapping journals no message and moves no cursor.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from . import store
from .models import D14_SENSITIVE_NAMES, Message, Risk, Scope

#: The design's sample window.
SAMPLE_LIMIT = 300

#: Pages are requested in this size; the adapter clamps to its own maximum.
SAMPLE_PAGE = 100

#: How many sender domains the deterministic proposer names.
DETERMINISTIC_LIMIT = 10

_WORDS = re.compile(r"[^0-9a-z]+")


class TaxonomyError(Exception):
    """A proposal or an approval the seam refuses."""


@dataclass(frozen=True)
class FolderProposal:
    """One folder the operator is asked to approve."""

    name: str
    description: str = ""
    risk: Risk = Risk.LOW
    example_senders: tuple[str, ...] = ()
    expected_volume: int = 0


@dataclass(frozen=True)
class TaxonomyProposal:
    """A stored, proposed version and the evidence behind it."""

    version: int
    folders: tuple[FolderProposal, ...]
    sampled: int
    existing: tuple[str, ...]


#: A model-backed proposer, or any deterministic stand-in for one.
Proposer = Callable[[tuple[Message, ...], tuple[str, ...]], Iterable[Mapping[str, Any] | FolderProposal]]


def sample(adapter: Any, limit: int = SAMPLE_LIMIT) -> tuple[Message, ...]:
    """Read at most ``limit`` inbox messages, following the page cursor."""

    collected: list[Message] = []
    cursor: str | None = None
    while len(collected) < limit:
        page = adapter.list_since(cursor, min(SAMPLE_PAGE, limit - len(collected)), Scope.INBOX)
        collected.extend(page.messages)
        if page.complete or not page.next_cursor or page.next_cursor == cursor:
            break
        cursor = page.next_cursor
    return tuple(collected[:limit])


def deterministic_proposals(
    messages: tuple[Message, ...],
    existing: tuple[str, ...],
) -> list[FolderProposal]:
    """Existing labels plus the busiest sender domains, for a keyless bootstrap.

    This is a proposal an operator edits, not a classification. The domain label
    is a readable stand-in; the risk still passes through the D14 check.
    """

    proposals = [FolderProposal(name=name, description="Existing label") for name in existing]
    seen = {store.sensitive_key(proposal.name) for proposal in proposals}

    senders: dict[str, list[str]] = {}
    for message in messages:
        if message.sender_domain:
            senders.setdefault(message.sender_domain, []).append(message.sender)
    counts = Counter({domain: len(found) for domain, found in senders.items()})

    for domain, volume in counts.most_common(DETERMINISTIC_LIMIT):
        parts = [part for part in domain.split(".") if part]
        label = (parts[-2] if len(parts) >= 2 else parts[0]).replace("-", " ").replace("_", " ").title()
        if not label or store.sensitive_key(label) in seen:
            continue
        seen.add(store.sensitive_key(label))
        proposals.append(
            FolderProposal(
                name=label,
                description=f"Proposed from {volume} recent messages from {domain}",
                example_senders=tuple(sorted(set(senders[domain]))[:3]),
                expected_volume=volume,
            )
        )
    return proposals


def bootstrap(
    conn: sqlite3.Connection,
    account: str,
    adapter: Any,
    *,
    proposer: Proposer | None = None,
    sample_limit: int = SAMPLE_LIMIT,
) -> TaxonomyProposal:
    """Sample, propose, enforce risk, and store a ``proposed`` version.

    Nothing is approved here. Filing stays disabled until ``approve`` runs.
    """

    messages = sample(adapter, sample_limit)
    existing = tuple(folder.name for folder in adapter.folders())
    raw = proposer(messages, existing) if proposer is not None else deterministic_proposals(messages, existing)
    folders = tuple(_enforce(conn, account, folder) for folder in _normalise(raw))

    version = store.write_taxonomy(
        conn,
        account,
        [{"name": folder.name, "risk": folder.risk.value, "description": folder.description} for folder in folders],
        state="proposed",
    )
    return TaxonomyProposal(version=version, folders=folders, sampled=len(messages), existing=existing)


def approve(
    conn: sqlite3.Connection,
    account: str,
    adapter: Any,
    *,
    version: int | None = None,
    edits: Iterable[Mapping[str, Any] | FolderProposal] | None = None,
) -> int:
    """Approve a version, or write edited folders as a new approved version.

    The provider labels are ensured first. A failure there leaves the proposal
    unapproved, so filing cannot target a label that was never created.
    """

    if edits is not None:
        folders = tuple(_enforce(conn, account, folder) for folder in _normalise(edits))
        rows = [
            {"name": folder.name, "risk": folder.risk.value, "description": folder.description} for folder in folders
        ]
        _ensure_folders(adapter, rows)
        return store.write_taxonomy(conn, account, rows, state="approved")

    target = version if version is not None else store.latest_taxonomy_version(conn, account, state="proposed")
    if target is None:
        raise TaxonomyError("no proposed taxonomy to approve")
    rows = store.taxonomy_folders(conn, account, target)
    if rows is None:
        raise TaxonomyError(f"taxonomy version {target} does not exist")
    _ensure_folders(adapter, rows)
    if not store.set_taxonomy_state(conn, account, target, "approved"):
        raise TaxonomyError(f"taxonomy version {target} could not be approved")
    return target


def _ensure_folders(adapter: Any, rows: Iterable[Mapping[str, Any]]) -> None:
    for row in rows:
        adapter.ensure_folder(str(row["name"]))


def _coerce(item: Mapping[str, Any] | FolderProposal) -> FolderProposal:
    if isinstance(item, FolderProposal):
        return item
    name = str(item.get("name", "")).strip()
    if not name:
        raise TaxonomyError("a proposal needs a name")
    raw_risk = item.get("risk")
    try:
        risk = Risk(raw_risk)
    except (ValueError, TypeError):
        risk = Risk.SENSITIVE
    try:
        volume = int(item.get("expected_volume", 0) or 0)
    except (TypeError, ValueError):
        volume = 0
    return FolderProposal(
        name=name,
        description=str(item.get("description", "")),
        risk=risk,
        example_senders=tuple(str(sender) for sender in item.get("example_senders") or ()),
        expected_volume=max(volume, 0),
    )


def _normalise(items: Iterable[Mapping[str, Any] | FolderProposal]) -> list[FolderProposal]:
    """One folder per canonical key, first wins, order preserved."""

    seen: set[str] = set()
    folders: list[FolderProposal] = []
    for item in items:
        folder = _coerce(item)
        key = store.sensitive_key(folder.name)
        if key in seen:
            continue
        seen.add(key)
        folders.append(folder)
    return folders


def _enforce(conn: sqlite3.Connection, account: str, folder: FolderProposal) -> FolderProposal:
    """The operator cannot be talked out of a sensitive folder."""

    if folder.risk is Risk.SENSITIVE:
        return folder
    if store.is_sensitive(conn, account, folder.name) or _names_d14(folder.name, folder.description):
        return replace(folder, risk=Risk.SENSITIVE)
    return folder


def _names_d14(name: str, description: str) -> bool:
    tokens = {token for token in _WORDS.split(store.sensitive_key(f"{name} {description}")) if token}
    return any(store.sensitive_key(item) in tokens for item in D14_SENSITIVE_NAMES)
