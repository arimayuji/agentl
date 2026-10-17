"""Email over IMAP or Microsoft Graph (standard library only): read messages and attachments,
then flag them."""

from __future__ import annotations

import base64
import contextlib
import email
import email.policy
import imaplib
import re
import time
import urllib.parse
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any

from ..checks import parse_duration
from ..registry import connector
from . import saas
from ._util import single, tmpdir

_SAFE = re.compile(r"[^\w.\- ]+")


def _connect(host: str, port: int, user: str, password: str, ssl: bool) -> imaplib.IMAP4:
    conn = imaplib.IMAP4_SSL(host, port) if ssl else imaplib.IMAP4(host, port)
    conn.login(user, password)
    return conn


def _check(typ: str, data: Any, what: str) -> Any:
    if typ != "OK":
        raise RuntimeError(f"IMAP {what} failed: {data}")
    return data


def _safe_name(name: str | None, fallback: str) -> str:
    """Attachment names come from the sender: keep only the base name and safe characters."""
    base = Path((name or "").replace("\\", "/")).name
    base = _SAFE.sub("_", base).strip(" .")
    return base[:150] or fallback


def _body(msg: EmailMessage) -> tuple[str, str]:
    part = msg.get_body(preferencelist=("plain",))
    text = part.get_content() if part is not None else ""
    html_part = msg.get_body(preferencelist=("html",))
    html = html_part.get_content() if html_part is not None else ""
    return text, html


def _addresses(value: str | None) -> list[str]:
    return [addr for _, addr in getaddresses([value or ""]) if addr]


@connector("email.read")
def email_read(
    input: Any = None,
    host: str = "",
    user: str = "",
    password: str = "",
    port: int | None = None,
    ssl: bool = True,
    folder: str = "INBOX",
    search: str = "UNSEEN",
    limit: int | None = None,
    attachments: bool | dict = True,
    explode: bool = False,
    extensions: list[str] | None = None,
    mark_seen: bool = False,
    provider: str = "imap",
    mailbox: str | None = None,
    since: str | None = None,
    filter: str | None = None,
    access_token: str | None = None,
    tenant: str | None = None,
    api_url: str = saas.GRAPH_API,
    token_url: str | None = None,
) -> list[dict]:
    """Read messages matching an IMAP `search` (e.g. 'FROM "billing@x.com" UNSEEN SINCE 01-Oct-2026').

    One record per message: uid, folder, message_id, from, to, cc, subject, date, text, html and
    `attachments` (files saved to a temp dir: path, filename, content_type, size). With
    `explode: true`, one record per attachment instead, with `path` and the message fields, ready
    for `file.read`. Messages are read without changing their flags unless `mark_seen: true`; to
    flag them only after the whole pipeline works, end it with `email.mark`.

    `provider: msgraph` reads a Microsoft 365 `mailbox` through Microsoft Graph instead (token from
    MS_ACCESS_TOKEN or MS_TENANT_ID / MS_CLIENT_ID / MS_CLIENT_SECRET), with `folder` as a path
    such as "Reports/Weekly", `since` ("7d" or an ISO date) and an optional OData `filter`.
    `attachments: {name_regex: '...'}` keeps only attachments whose name matches."""
    wanted = {e.lower().lstrip(".") for e in extensions or []}
    if isinstance(attachments, dict) and set(attachments) - {"name_regex"}:
        raise ValueError(f"attachments: unknown keys {sorted(set(attachments) - {'name_regex'})}")

    name_re = re.compile(attachments.get("name_regex", "")) if isinstance(attachments, dict) else None

    def keep(name: str) -> bool:
        if wanted and Path(name).suffix.lower().lstrip(".") not in wanted:
            return False
        return name_re is None or bool(name_re.search(name))

    if provider == "msgraph":
        return _graph_read(
            mailbox,
            folder,
            since,
            filter,
            limit,
            bool(attachments),
            explode,
            keep,
            mark_seen,
            saas.ms_token(access_token, tenant, token_url),
            api_url,
        )
    if provider != "imap":
        raise ValueError(f"provider '{provider}': use imap or msgraph")
    if since:
        search = f"{search} SINCE {_since(since).strftime('%d-%b-%Y')}".strip()
    conn = _connect(host, port or (993 if ssl else 143), user, password, ssl)
    out: list[dict] = []
    try:
        _check(*conn.select(_quote(folder), readonly=not mark_seen), "select")
        uids = _check(*conn.uid("SEARCH", search), "search")[0].split()
        if limit:
            uids = uids[-limit:]  # newest
        for uid in uids:
            data = _check(*conn.uid("FETCH", uid, "(BODY.PEEK[])"), "fetch")
            raw = next(p[1] for p in data if isinstance(p, tuple))
            msg = email.message_from_bytes(raw, policy=email.policy.default)
            assert isinstance(msg, EmailMessage)
            text, html = _body(msg)
            try:
                date = parsedate_to_datetime(msg["Date"]).isoformat() if msg["Date"] else None
            except (TypeError, ValueError):
                date = None
            rec = {
                "uid": uid.decode(),
                "folder": folder,
                "message_id": msg["Message-ID"],
                "from": _addresses(msg["From"])[0] if _addresses(msg["From"]) else None,
                "to": _addresses(msg["To"]),
                "cc": _addresses(msg["Cc"]),
                "subject": msg["Subject"] or "",
                "date": date,
                "received_at": date,
                "text": text,
                "html": html,
            }
            files = []
            if attachments:
                folder_dir = tmpdir(f"mail-{uid.decode()}-")
                for i, part in enumerate(msg.iter_attachments()):
                    name = _safe_name(part.get_filename(), f"attachment-{i}")
                    if not keep(name):
                        continue
                    payload = part.get_payload(decode=True)
                    payload = payload if isinstance(payload, bytes) else b""
                    path = folder_dir / name
                    if path.exists():
                        path = folder_dir / f"{i}-{name}"
                    path.write_bytes(payload)
                    files.append(
                        {
                            "path": str(path),
                            "filename": name,
                            "content_type": part.get_content_type(),
                            "size": len(payload),
                        }
                    )
            if explode:
                out.extend({**rec, **f, "attachment": f["filename"]} for f in files)
            else:
                out.append({**rec, "attachments": files})
            if mark_seen:
                _check(*conn.uid("STORE", uid, "+FLAGS", "(\\Seen)"), "store")
    finally:
        with contextlib.suppress(Exception):
            conn.logout()
    return out


def _since(value: str) -> datetime:
    """'7d' (relative to now) or an ISO date/datetime -> an aware UTC datetime."""
    try:
        return datetime.fromtimestamp(time.time() - parse_duration(value), timezone.utc)
    except ValueError:
        pass
    dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _graph_folder(base: str, folder: str, token: str) -> str:
    """Resolve "Reports/Weekly" one level at a time (well-known names like Inbox work at the top)."""
    parts = [p for p in folder.replace("\\", "/").split("/") if p]
    if not parts:
        return f"{base}/mailFolders/inbox"
    first, rest = parts[0], parts[1:]
    if first.lower() in {"inbox", "archive", "drafts", "sentitems", "deleteditems", "junkemail"}:
        url = f"{base}/mailFolders/{first.lower()}"
    else:
        url = _find_child(f"{base}/mailFolders", first, token, folder)
    for name in rest:
        url = _find_child(f"{url}/childFolders", name, token, folder)
    return url


def _find_child(url: str, name: str, token: str, full: str) -> str:
    esc = name.replace("'", "''")
    res = saas._call("GET", saas._q(url, {"$filter": f"displayName eq '{esc}'", "$top": 50}), token)
    hits = [f for f in res.get("value", []) if f.get("displayName", "").lower() == name.lower()]
    if not hits:
        raise ValueError(f"mail folder '{full}': '{name}' not found")
    return f"{url}/{hits[0]['id']}"


def _graph_read(
    mailbox: str | None,
    folder: str,
    since: str | None,
    odata: str | None,
    limit: int | None,
    attachments: bool,
    explode: bool,
    keep: Any,
    mark_seen: bool,
    token: str,
    api_url: str,
) -> list[dict]:
    if not mailbox:
        raise ValueError("provider msgraph needs `mailbox` (e.g. reports@example.com)")
    base = f"{api_url}/users/{urllib.parse.quote(mailbox)}"
    folder_url = _graph_folder(base, "" if folder == "INBOX" else folder, token)
    clauses = [f"({odata})"] if odata else []
    if since:
        clauses.append(f"receivedDateTime ge {_since(since).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    params: dict[str, Any] = {
        "$select": "id,internetMessageId,from,toRecipients,ccRecipients,subject,receivedDateTime,"
        "body,hasAttachments",
        "$orderby": "receivedDateTime desc",
        "$top": min(limit or 50, 50),
    }
    if clauses:
        params["$filter"] = " and ".join(clauses)
    url: str | None = saas._q(f"{folder_url}/messages", params)
    msgs: list[dict] = []
    while url and not (limit and len(msgs) >= limit):
        res = saas._call("GET", url, token, headers={"Prefer": 'outlook.body-content-type="text"'})
        msgs += res.get("value", [])
        url = res.get("@odata.nextLink")
    msgs = msgs[:limit] if limit else msgs

    def addrs(items: list | None) -> list[str]:
        return [a["emailAddress"]["address"] for a in items or [] if a.get("emailAddress")]

    out: list[dict] = []
    for m in msgs:
        body = m.get("body") or {}
        text = body.get("content", "") if body.get("contentType") != "html" else ""
        rec = {
            "uid": m["id"],
            "folder": folder,
            "message_id": m.get("internetMessageId"),
            "from": next(iter(addrs([m.get("from") or {}])), None),
            "to": addrs(m.get("toRecipients")),
            "cc": addrs(m.get("ccRecipients")),
            "subject": m.get("subject") or "",
            "date": m.get("receivedDateTime"),
            "received_at": m.get("receivedDateTime"),
            "text": text,
            "html": body.get("content", "") if body.get("contentType") == "html" else "",
        }
        files = []
        if attachments and m.get("hasAttachments"):
            dest = tmpdir("mail-")
            aurl: str | None = f"{base}/messages/{m['id']}/attachments"
            i = 0
            while aurl:
                res = saas._call("GET", aurl, token)
                for att in res.get("value", []):
                    if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
                        continue  # item and reference attachments have no bytes here
                    name = _safe_name(att.get("name"), f"attachment-{i}")
                    i += 1
                    if not keep(name):
                        continue
                    payload = base64.b64decode(att.get("contentBytes") or "")
                    path = dest / name
                    if path.exists():
                        path = dest / f"{i}-{name}"
                    path.write_bytes(payload)
                    files.append(
                        {
                            "path": str(path),
                            "filename": name,
                            "content_type": att.get("contentType"),
                            "size": len(payload),
                        }
                    )
                aurl = res.get("@odata.nextLink")
        if explode:
            out.extend({**rec, **f, "attachment": f["filename"], "name": f["filename"]} for f in files)
        else:
            out.append({**rec, "attachments": files})
        if mark_seen:
            saas._call("PATCH", f"{base}/messages/{m['id']}", token, body={"isRead": True})
    return out


def _quote(folder: str) -> str:
    return f'"{folder}"' if " " in folder and not folder.startswith('"') else folder


@connector("email.mark")
def email_mark(
    input: Any,
    host: str = "",
    user: str = "",
    password: str = "",
    port: int | None = None,
    ssl: bool = True,
    seen: bool = True,
    flags: list[str] | None = None,
    move_to: str | None = None,
) -> list[dict]:
    """Flag (default `\\Seen`) or move the messages of upstream records (`uid`, `folder`).
    Put it last, so messages are only marked when every earlier step worked."""
    by_folder: dict[str, set[str]] = {}
    for r in single(input):
        if r.get("uid"):
            by_folder.setdefault(r.get("folder") or "INBOX", set()).add(str(r["uid"]))
    if not by_folder:
        return []
    add = list(flags or []) + (["\\Seen"] if seen else [])
    conn = _connect(host, port or (993 if ssl else 143), user, password, ssl)
    out = []
    try:
        for folder, uids in by_folder.items():
            _check(*conn.select(_quote(folder)), "select")
            ids = ",".join(sorted(uids, key=int))
            if add:
                _check(*conn.uid("STORE", ids, "+FLAGS", f"({' '.join(add)})"), "store")
            if move_to:
                _check(*conn.uid("COPY", ids, _quote(move_to)), "copy")
                _check(*conn.uid("STORE", ids, "+FLAGS", "(\\Deleted)"), "store")
                conn.expunge()
            out.append({"folder": folder, "marked": len(uids), "moved_to": move_to})
    finally:
        with contextlib.suppress(Exception):
            conn.logout()
    return out
