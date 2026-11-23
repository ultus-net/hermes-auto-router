"""The Gmail adapter and its protocol-route allowlist.

N7: the allowlist is derived from the protocol, not maintained beside it. Every
``Mailbox`` operation names exactly one route, and a refused family raises
before a URL is built. Google documents ``gmail.modify`` as read, compose, and
send, so "nothing sends" is this table rather than the scope string.

The transport is injected so the suite runs without credentials. The default
transport asks Hermes for the grant, and its import stays inside the call so
the seam loads without Hermes installed.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import quote, urlencode

from .mailbox import CursorExpired
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

BASE_URL = "https://gmail.googleapis.com/gmail/v1/users/me"

#: Gmail's own maximum for ``users.messages.list``.
MAX_PAGE = 500

_ADDRESS = re.compile(r"([^@<>\s]+@[^>\s,]+)")


class GmailError(Exception):
    """A provider answer this adapter cannot use."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class RefusedRoute(GmailError):
    """A protocol family the allowlist deliberately does not carry."""


class UnknownTarget(GmailError):
    """A filing target with no matching label."""


class Operation(StrEnum):
    """Every Gmail route this adapter is allowed to call."""

    PROFILE = "profile"
    LIST_MESSAGES = "list_messages"
    HISTORY = "history"
    GET_MESSAGE = "get_message"
    MODIFY_MESSAGE = "modify_message"
    TRASH_MESSAGE = "trash_message"
    LIST_LABELS = "list_labels"
    CREATE_LABEL = "create_label"


@dataclass(frozen=True)
class Route:
    """The one method and path an operation is allowed to use."""

    method: str
    path: str


ROUTES: dict[Operation, Route] = {
    Operation.PROFILE: Route("GET", "/profile"),
    Operation.LIST_MESSAGES: Route("GET", "/messages"),
    Operation.HISTORY: Route("GET", "/history"),
    Operation.GET_MESSAGE: Route("GET", "/messages/{message_id}"),
    Operation.MODIFY_MESSAGE: Route("POST", "/messages/{message_id}/modify"),
    Operation.TRASH_MESSAGE: Route("POST", "/messages/{message_id}/trash"),
    Operation.LIST_LABELS: Route("GET", "/labels"),
    Operation.CREATE_LABEL: Route("POST", "/labels"),
}

#: Gmail families the grant technically reaches and this seam never calls.
REFUSED_ROUTES: dict[str, str] = {
    "send": "users.messages.send",
    "drafts": "users.drafts",
    "delete": "users.messages.delete",
}


class Response(Protocol):
    """The subset of Hermes' ``ProviderResponse`` the adapter reads."""

    status: int
    text: str

    def json(self) -> dict[str, Any]: ...


class Transport(Protocol):
    """One credentialed request. Hermes owns the token."""

    def __call__(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 30.0,
    ) -> Response: ...


def credentialed_request(
    method: str,
    url: str,
    *,
    json: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> Response:
    """Ask Hermes to attach the ``google`` grant and make one request.

    The import stays here so the seam loads on a host without Hermes, and the
    call stays in this plugin's frame because Hermes resolves the caller's
    manifest from the stack.
    """

    from hermes_cli.plugin_provider_requests import credentialed_provider_request

    return credentialed_provider_request("google", method, url, json=json, headers=headers, timeout=timeout)


def _encode_cursor(kind: str, **fields: Any) -> str:
    return json.dumps({"kind": kind, **fields}, sort_keys=True, separators=(",", ":"))


def _decode_cursor(cursor: str) -> dict[str, Any]:
    try:
        data = json.loads(cursor)
    except json.JSONDecodeError:
        return {"kind": "history", "history_id": cursor}
    if isinstance(data, str | int):
        return {"kind": "history", "history_id": str(data)}
    if not isinstance(data, dict) or "kind" not in data:
        raise GmailError(f"invalid cursor: {cursor!r}")
    return data


def _domain(value: str) -> str:
    match = _ADDRESS.search(value)
    if match is None:
        return ""
    return match.group(1).split("@", 1)[1].casefold().strip(".")


def _decode_body(data: str) -> str:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding).decode("utf-8", "replace")


def _body_text(payload: dict[str, Any], snippet: str = "") -> str:
    chunks: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        if part.get("mimeType") == "text/plain":
            data = (part.get("body") or {}).get("data")
            if data:
                chunks.append(_decode_body(str(data)))
        for child in part.get("parts") or ():
            if isinstance(child, dict):
                walk(child)

    walk(payload)
    return "\n".join(chunk for chunk in chunks if chunk).strip() or snippet


def _received_at(raw: dict[str, Any], fallback: str | None) -> str | None:
    internal = raw.get("internalDate")
    if internal is None:
        return fallback
    try:
        seconds = int(internal) / 1000
    except (TypeError, ValueError):
        return fallback
    return datetime.fromtimestamp(seconds, UTC).isoformat(timespec="seconds")


def _limit(limit: int) -> str:
    return str(max(1, min(int(limit), MAX_PAGE)))


class GmailMailbox:
    """The Gmail half of the ``Mailbox`` protocol."""

    def __init__(self, transport: Transport | None = None, *, account: str = "personal") -> None:
        self._transport = transport or credentialed_request
        self.account = account
        self._label_rows: list[dict[str, Any]] | None = None

    # -- Mailbox protocol -------------------------------------------------

    def list_since(self, cursor: str | None, limit: int, scope: Scope) -> Page:
        if cursor is None:
            return self._list(scope, limit, token=None)

        state = _decode_cursor(cursor)
        kind = state["kind"]
        if kind == "page":
            return self._list(scope, limit, token=state.get("next"), history_id=state.get("history"))
        if kind == "history" and scope is Scope.INBOX:
            return self._history(scope, limit, start=str(state["history_id"]), token=None)
        if kind == "history_page":
            return self._history(scope, limit, start=str(state["start"]), token=state.get("next"))
        if kind == "history":
            # The archive window is not incremental; replay it from the top and
            # return a fresh frontier.
            return self._list(scope, limit, token=None)
        raise GmailError(f"unknown cursor kind: {kind!r}")

    def apply(self, ref: Ref, action: Decision) -> ApplyResult:
        if action.verb is Verb.NOOP:
            return ApplyResult(outcome=Outcome.NOOP, post_ref=ref.message_id)

        try:
            if action.verb is Verb.TRASH:
                self._call(Operation.TRASH_MESSAGE, message_id=ref.message_id)
                return ApplyResult(outcome=Outcome.APPLIED)
            if action.verb in (Verb.FILE, Verb.LABEL):
                if not action.target:
                    return ApplyResult(outcome=Outcome.ERROR, error="missing_target")
                label = self._folder_id(action.target)
                remove = ["INBOX"] if action.verb is Verb.FILE else []
                self._modify(ref.message_id, add=[label], remove=remove)
                return ApplyResult(outcome=Outcome.APPLIED)
            if action.verb is Verb.MARK_READ:
                self._modify(ref.message_id, add=[], remove=["UNREAD"])
                return ApplyResult(outcome=Outcome.APPLIED)
            if action.verb in (Verb.STAR, Verb.FLAG):
                self._modify(ref.message_id, add=["STARRED"], remove=[])
                return ApplyResult(outcome=Outcome.APPLIED)
        except GmailError as exc:
            return ApplyResult(outcome=Outcome.ERROR, error=str(exc))

        return ApplyResult(outcome=Outcome.ERROR, error=f"unsupported_verb:{action.verb.value}")

    def folders(self) -> list[Folder]:
        return [
            Folder(name=str(row["name"]), folder_id=str(row["id"]))
            for row in self._labels()
            if row.get("type") != "system" and row.get("name") and row.get("id")
        ]

    def ensure_folder(self, name: str) -> Folder:
        existing = self._folder_named(name)
        if existing is not None:
            return existing

        try:
            raw = self._call(
                Operation.CREATE_LABEL,
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
        except GmailError as exc:
            # A concurrent create wins the race; the label is what matters.
            if exc.status != 409:
                raise
            self._label_rows = None
            existing = self._folder_named(name)
            if existing is None:
                raise
            return existing

        self._label_rows = None
        return Folder(name=str(raw.get("name", name)), folder_id=str(raw["id"]))

    def resync(self, scope: Scope, limit: int) -> Page:
        return self._list(scope, limit, token=None)

    def find_by_stable_id(self, stable_id: str, folders: list[str]) -> Ref | None:
        if not stable_id:
            return None

        try:
            raw = self._call(Operation.GET_MESSAGE, message_id=stable_id, params={"format": "minimal"})
        except GmailError as exc:
            if exc.status != 404:
                raise
            raw = None
        if raw is not None and self._in_folders(raw, folders):
            return self._ref(raw)

        data = self._call(
            Operation.LIST_MESSAGES,
            params={"q": f"rfc822msgid:{stable_id}", "maxResults": "1"},
        )
        for item in data.get("messages") or ():
            if not item.get("id"):
                continue
            found = self._call(Operation.GET_MESSAGE, message_id=str(item["id"]), params={"format": "minimal"})
            if self._in_folders(found, folders):
                return self._ref(found)
        return None

    # -- Refused families -------------------------------------------------

    def send_message(self, *args: Any, **kwargs: Any) -> None:
        self._refuse("send")

    def create_draft(self, *args: Any, **kwargs: Any) -> None:
        self._refuse("drafts")

    def delete_message(self, *args: Any, **kwargs: Any) -> None:
        self._refuse("delete")

    # -- Routes -----------------------------------------------------------

    def _refuse(self, family: str) -> None:
        raise RefusedRoute(f"the Gmail adapter never calls {REFUSED_ROUTES[family]}")

    def _call(
        self,
        operation: Operation,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        message_id: str | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        route = ROUTES[operation]
        path = route.path.format(message_id=quote(message_id, safe="")) if message_id is not None else route.path
        url = BASE_URL + path
        if params:
            url = f"{url}?{urlencode(params)}"

        response = self._transport(route.method, url, json=body)
        status = int(getattr(response, "status", 0))
        if status == 404 and operation is Operation.HISTORY:
            raise CursorExpired(str(cursor))
        if not 200 <= status < 300:
            text = str(getattr(response, "text", ""))
            raise GmailError(f"{operation.value} returned {status}: {text[:200]}", status=status)
        return response.json()

    def _list(self, scope: Scope, limit: int, *, token: str | None, history_id: str | None = None) -> Page:
        params = {"maxResults": _limit(limit)}
        if scope is Scope.INBOX:
            params["labelIds"] = "INBOX"
        else:
            params["q"] = "-in:inbox"
        if token:
            params["pageToken"] = token

        data = self._call(Operation.LIST_MESSAGES, params=params)
        messages = tuple(
            self._message(str(item["id"])) for item in data.get("messages") or () if item.get("id")
        )
        next_token = data.get("nextPageToken")
        high = history_id or self._profile_history()

        if next_token and next_token != token:
            next_cursor = _encode_cursor("page", scope=scope.value, next=next_token, history=high)
        else:
            next_cursor = _encode_cursor("history", history_id=high)

        return Page(
            messages=messages,
            scope=scope,
            next_cursor=next_cursor,
            high_water=high,
            complete=not next_token,
        )

    def _history(self, scope: Scope, limit: int, *, start: str, token: str | None) -> Page:
        params = {
            "startHistoryId": start,
            "historyTypes": "messageAdded",
            "maxResults": _limit(limit),
        }
        if scope is Scope.INBOX:
            params["labelId"] = "INBOX"
        if token:
            params["pageToken"] = token

        data = self._call(Operation.HISTORY, params=params, cursor=start)
        ids: list[str] = []
        for record in data.get("history") or ():
            for added in record.get("messagesAdded") or ():
                message = added.get("message") or {}
                if message.get("id") and str(message["id"]) not in ids:
                    ids.append(str(message["id"]))

        messages = tuple(self._message(message_id) for message_id in ids)
        next_token = data.get("nextPageToken")
        high = str(data.get("historyId") or start)

        if next_token:
            next_cursor = _encode_cursor("history_page", scope=scope.value, start=start, next=next_token)
        else:
            next_cursor = _encode_cursor("history", history_id=high)

        return Page(
            messages=messages,
            scope=scope,
            next_cursor=next_cursor,
            high_water=high,
            complete=not next_token,
        )

    def _profile_history(self) -> str:
        data = self._call(Operation.PROFILE)
        history = data.get("historyId")
        if history is None:
            raise GmailError("profile response has no historyId")
        return str(history)

    # -- Message parsing --------------------------------------------------

    def _message(self, message_id: str) -> Message:
        raw = self._call(Operation.GET_MESSAGE, message_id=message_id, params={"format": "full"})
        return self._to_message(raw)

    def _to_message(self, raw: dict[str, Any]) -> Message:
        payload = raw.get("payload") or {}
        headers = {
            str(header.get("name", "")).casefold(): str(header.get("value", ""))
            for header in payload.get("headers") or ()
            if header.get("name")
        }
        sender = headers.get("from", "")
        message_id = str(raw["id"])
        return Message(
            ref=Ref(mailbox=self.account, message_id=message_id, stable_id=message_id),
            sender=sender,
            sender_domain=_domain(sender),
            subject=headers.get("subject", ""),
            folder_ids=tuple(str(label) for label in raw.get("labelIds") or ()),
            list_id=headers.get("list-id"),
            body_text=_body_text(payload, str(raw.get("snippet", ""))),
            internet_message_id=headers.get("message-id"),
            received_at=_received_at(raw, headers.get("date")),
        )

    def _ref(self, raw: dict[str, Any]) -> Ref:
        message_id = str(raw["id"])
        return Ref(mailbox=self.account, message_id=message_id, stable_id=message_id)

    # -- Labels -----------------------------------------------------------

    def _labels(self) -> list[dict[str, Any]]:
        if self._label_rows is None:
            data = self._call(Operation.LIST_LABELS)
            self._label_rows = [row for row in data.get("labels") or () if isinstance(row, dict)]
        return self._label_rows

    def _folder_named(self, name: str) -> Folder | None:
        folded = name.casefold()
        for folder in self.folders():
            if folder.name.casefold() == folded:
                return folder
        return None

    def _folder_id(self, name: str) -> str:
        folder = self._folder_named(name)
        if folder is None:
            raise UnknownTarget(f"unknown_target:{name}")
        return folder.folder_id

    def _in_folders(self, raw: dict[str, Any], folders: list[str]) -> bool:
        if not folders:
            return True
        wanted = {name.casefold() for name in folders}
        names = {
            str(row["id"]): str(row["name"]).casefold()
            for row in self._labels()
            if row.get("id") and row.get("name")
        }
        return any(names.get(str(label)) in wanted for label in raw.get("labelIds") or ())

    def _modify(self, message_id: str, *, add: list[str], remove: list[str]) -> None:
        body: dict[str, Any] = {}
        if add:
            body["addLabelIds"] = add
        if remove:
            body["removeLabelIds"] = remove
        self._call(Operation.MODIFY_MESSAGE, message_id=message_id, body=body)
