"""Email over IMAP (standard library only): read messages and attachments, then flag them."""

from __future__ import annotations

import contextlib
import email
import email.policy
import imaplib
import re
from email.message import EmailMessage
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any

from ..registry import connector
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
    attachments: bool = True,
    explode: bool = False,
    extensions: list[str] | None = None,
    mark_seen: bool = False,
) -> list[dict]:
    """Read messages matching an IMAP `search` (e.g. 'FROM "billing@x.com" UNSEEN SINCE 01-Oct-2026').

    One record per message: uid, folder, message_id, from, to, cc, subject, date, text, html and
    `attachments` (files saved to a temp dir: path, filename, content_type, size). With
    `explode: true`, one record per attachment instead, with `path` and the message fields, ready
    for `file.read`. Messages are read without changing their flags unless `mark_seen: true`; to
    flag them only after the whole pipeline works, end it with `email.mark`."""
    wanted = {e.lower().lstrip(".") for e in extensions or []}
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
                "text": text,
                "html": html,
            }
            files = []
            if attachments:
                folder_dir = tmpdir(f"mail-{uid.decode()}-")
                for i, part in enumerate(msg.iter_attachments()):
                    name = _safe_name(part.get_filename(), f"attachment-{i}")
                    if wanted and Path(name).suffix.lower().lstrip(".") not in wanted:
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
