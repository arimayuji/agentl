import imaplib
import os
import re
import time
import uuid
from email.message import EmailMessage
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

import pytest

from agentl.registry import get


def message(subject, sender="billing@vendor.com", attach=None, text="Hello"):
    msg = EmailMessage()
    msg["From"] = f"Vendor <{sender}>"
    msg["To"] = "ana@example.com"
    msg["Subject"] = subject
    msg["Date"] = "Tue, 13 Oct 2026 10:00:00 -0300"
    msg["Message-ID"] = f"<{uuid.uuid4().hex}@vendor.com>"
    msg.set_content(text)
    for name, data, mime in attach or []:
        maintype, subtype = mime.split("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return msg.as_bytes()


class FakeIMAP:
    """Just enough of imaplib.IMAP4 for the connector: UID SEARCH ALL/UNSEEN/FROM, FETCH, STORE, COPY."""

    boxes: ClassVar[dict] = {}

    def __init__(self, host, port):
        self.box = None

    def login(self, user, password):
        return "OK", [b"Logged in"]

    def select(self, folder, readonly=False):
        self.box = self.boxes.setdefault(folder.strip('"'), {})
        self.readonly = readonly
        return "OK", [str(len(self.box)).encode()]

    def uid(self, command, *args):
        if command == "SEARCH":
            crit = args[0]
            uids = [u for u, m in self.box.items()]
            if "UNSEEN" in crit:
                uids = [u for u in uids if "\\Seen" not in self.box[u]["flags"]]
            if m := re.search(r'FROM "([^"]+)"', crit):
                uids = [u for u in uids if m.group(1).encode() in self.box[u]["raw"]]
            return "OK", [" ".join(str(u) for u in uids).encode()]
        if command == "FETCH":
            u = int(args[0])
            return "OK", [(b"1 (UID %d BODY[] {n}" % u, self.box[u]["raw"]), b")"]
        if command == "STORE":
            for u in str(args[0]).split(","):
                self.box[int(u)]["flags"] |= set(args[2].strip("()").split())
            return "OK", [b""]
        if command == "COPY":
            dest = self.boxes.setdefault(args[1].strip('"'), {})
            for u in str(args[0]).split(","):
                dest[len(dest) + 1] = {**self.box[int(u)], "flags": set()}
            return "OK", [b""]
        raise AssertionError(command)

    def expunge(self):
        for u in [u for u, m in self.box.items() if "\\Deleted" in m["flags"]]:
            del self.box[u]

    def logout(self):
        pass


@pytest.fixture
def fake(monkeypatch):
    FakeIMAP.boxes = {
        "INBOX": {
            1: {
                "raw": message("Invoice 1", attach=[("nota.csv", b"id,total\n1,10\n", "text/csv")]),
                "flags": set(),
            },
            2: {"raw": message("Newsletter", sender="news@x.com"), "flags": set()},
            3: {
                "raw": message(
                    "Invoice 2",
                    attach=[
                        ("../../evil.csv", b"id,total\n2,20\n", "text/csv"),
                        ("logo.png", b"\x89PNG", "image/png"),
                    ],
                ),
                "flags": {"\\Seen"},
            },
        }
    }
    monkeypatch.setattr(imaplib, "IMAP4_SSL", FakeIMAP)
    return FakeIMAP


CRED = {"host": "imap.test", "user": "ana", "password": "x"}


def test_read_messages_and_attachments(fake):
    out = get("email.read")(None, **CRED, search='FROM "billing@vendor.com"')
    assert [r["subject"] for r in out] == ["Invoice 1", "Invoice 2"]
    first = out[0]
    assert first["from"] == "billing@vendor.com" and first["to"] == ["ana@example.com"]
    assert first["text"].strip() == "Hello" and first["date"].startswith("2026-10-13T10:00")
    att = first["attachments"][0]
    assert att["filename"] == "nota.csv" and Path(att["path"]).read_bytes() == b"id,total\n1,10\n"
    # sender-controlled names cannot escape the temp folder
    evil = out[1]["attachments"][0]
    assert evil["filename"] == "evil.csv"
    assert Path(evil["path"]).resolve().parent == Path(evil["path"]).parent.resolve()
    assert ".." not in evil["path"]
    assert fake.boxes["INBOX"][1]["flags"] == set()  # read-only by default


def test_explode_filter_and_file_read(fake):
    rows = get("email.read")(None, **CRED, search="ALL", explode=True, extensions=["csv"])
    assert [r["filename"] for r in rows] == ["nota.csv", "evil.csv"]
    data = get("file.read")(rows)
    assert [r["total"] for r in data] == ["10", "20"]


def test_unseen_limit_and_mark(fake):
    rows = get("email.read")(None, **CRED, search="UNSEEN", limit=1)
    assert [r["uid"] for r in rows] == ["2"]
    out = get("email.mark")(rows, **CRED, move_to="Done")
    assert out == [{"folder": "INBOX", "marked": 1, "moved_to": "Done"}]
    assert 2 not in fake.boxes["INBOX"] and len(fake.boxes["Done"]) == 1
    assert [r["uid"] for r in get("email.read")(None, **CRED, search="UNSEEN")] == ["1"]


REAL = os.environ.get("AGENTL_TEST_IMAP")  # imap://user:password@host:port


@pytest.mark.skipif(not REAL, reason="set AGENTL_TEST_IMAP=imap://user:pass@host:port")
def test_real_imap_server():
    u = urlparse(REAL)
    cred = {"host": u.hostname, "port": u.port, "user": u.username, "password": u.password, "ssl": False}
    folder = f"T{uuid.uuid4().hex[:8]}"
    conn = imaplib.IMAP4(u.hostname, u.port)
    conn.login(u.username, u.password)
    conn.create(folder)
    for raw in (
        message("Invoice A", attach=[("a.csv", b"id\n1\n", "text/csv")]),
        message("Other", sender="x@y.com"),
    ):
        conn.append(folder, None, imaplib.Time2Internaldate(time.time()), raw)
    conn.logout()

    rows = get("email.read")(
        None, **cred, folder=folder, search='FROM "billing@vendor.com" UNSEEN', explode=True
    )
    assert [(r["subject"], r["filename"]) for r in rows] == [("Invoice A", "a.csv")]
    assert len(get("email.read")(None, **cred, folder=folder, search="UNSEEN")) == 2  # peek kept them unseen
    get("email.mark")(rows, **cred)
    assert [r["subject"] for r in get("email.read")(None, **cred, folder=folder, search="UNSEEN")] == [
        "Other"
    ]
