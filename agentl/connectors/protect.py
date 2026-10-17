"""Field-level protection before data reaches a destination (#225).

    - id: protect
      use: protect
      input: clean
      with:
        key: ${AGENTL_DATA_KEY}
        fields: { tax_id: encrypt, email: hash, phone: mask, notes: drop }

encrypt  AES-256-GCM (`agentl-etl[crypto]`), reversible with `unprotect` and the same key. The
         value is stored as `enc:v1:<key id>:<base64>`; the field name is bound to it, so a value
         copied into another field does not decrypt.
hash     keyed HMAC-SHA256, hex: the same input and key give the same output, so joins and dedupe
         still work. Not a plain hash: low-entropy values such as tax ids are brute-forced.
mask     keeps the last 4 characters (`{mask: 2}` keeps 2): "*******4321".
drop     removes the field.

`key` is 32 random bytes in base64 (`agentl keygen` prints one). A list of keys rotates: the first
protects, every one of them can unprotect. Encryption and hashing use separate keys derived from it.
Protected data is still personal data under most privacy laws; whoever holds the key can read it."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
from typing import Any

from ..registry import connector, require, rowwise
from ._util import single

PREFIX = "enc:v1:"
ACTIONS = ("encrypt", "hash", "mask", "drop")


class Key:
    def __init__(self, raw: str):
        try:
            secret = base64.urlsafe_b64decode(raw.strip() + "=" * (-len(raw.strip()) % 4))
        except (binascii.Error, ValueError):
            secret = b""
        if len(secret) != 32:
            raise ValueError(
                "protect: key must be 32 random bytes in base64; make one with `agentl keygen` "
                "and pass it through an environment variable or a secret reference"
            )
        self.enc = hmac.new(secret, b"agentl-encrypt", hashlib.sha256).digest()
        self.mac = hmac.new(secret, b"agentl-hash", hashlib.sha256).digest()
        self.id = hmac.new(secret, b"agentl-key-id", hashlib.sha256).hexdigest()[:8]


def new_key() -> str:
    return base64.urlsafe_b64encode(os.urandom(32)).decode()


def _keys(key: str | list[str]) -> list[Key]:
    raw = [key] if isinstance(key, str) else list(key or [])
    if not raw:
        raise ValueError("protect: `key` is required (a list of keys rotates: the first one protects)")
    return [Key(k) for k in raw]


def _aead(k: Key) -> Any:
    aead = require("cryptography.hazmat.primitives.ciphers.aead", "crypto")
    return aead.AESGCM(k.enc)


def encrypt_value(value: Any, field: str, k: Key) -> str:
    nonce = os.urandom(12)
    data = json.dumps(value, ensure_ascii=False, default=str).encode()
    blob = nonce + _aead(k).encrypt(nonce, data, field.encode())
    return f"{PREFIX}{k.id}:{base64.urlsafe_b64encode(blob).decode().rstrip('=')}"


def decrypt_value(text: str, field: str, keys: list[Key]) -> Any:
    if not isinstance(text, str) or not text.startswith(PREFIX):
        raise ValueError(f"unprotect: field '{field}' is not an encrypted value")
    kid, _, b64 = text[len(PREFIX) :].partition(":")
    k = next((k for k in keys if k.id == kid), None)
    if k is None:
        raise ValueError(f"unprotect: field '{field}' was encrypted with key {kid}, which is not in `key`")
    blob = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4))
    exc = require("cryptography.exceptions", "crypto")
    try:
        data = _aead(k).decrypt(blob[:12], blob[12:], field.encode())
    except exc.InvalidTag:
        why = "altered, or moved from another field"
        raise ValueError(f"unprotect: field '{field}' does not decrypt ({why})") from None
    return json.loads(data)


def hash_value(value: Any, k: Key) -> str:
    data = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return hmac.new(k.mac, data.encode(), hashlib.sha256).hexdigest()


def mask_value(value: Any, keep: int = 4) -> str:
    s = str(value)
    shown = s[-keep:] if 0 < keep < len(s) else ""
    return "*" * (len(s) - len(shown)) + shown


def _plan(fields: dict[str, Any]) -> list[tuple[str, str, int]]:
    if not isinstance(fields, dict) or not fields:
        raise ValueError("protect: `fields` maps each field to encrypt, hash, mask or drop")
    plan = []
    for name, spec in fields.items():
        keep = 4
        if isinstance(spec, dict) and list(spec) == ["mask"]:
            action, keep = "mask", int(spec["mask"])
        else:
            action = str(spec)
        if action not in ACTIONS:
            raise ValueError(f"protect: field '{name}': {spec!r} is not one of {', '.join(ACTIONS)}")
        plan.append((name, action, keep))
    return plan


@connector("protect")
def protect(input: Any, fields: dict[str, Any], key: str | list[str] | None = None) -> list[dict]:
    """Encrypt, hash, mask or drop fields before they reach a destination. `fields`: {field:
    encrypt | hash | mask | {mask: N} | drop}. `key`: 32 random bytes in base64 (`agentl keygen`),
    from an environment variable or secret reference; a list rotates (the first one protects).
    Empty values stay empty. Needs `agentl-etl[crypto]` for encrypt."""
    plan = _plan(fields)
    needs_key = any(a in ("encrypt", "hash") for _, a, _ in plan)
    k = _keys(key)[0] if needs_key else None  # type: ignore[arg-type]
    out = []
    for r in single(input):
        row = dict(r)
        for name, action, keep in plan:
            if action == "drop":
                row.pop(name, None)
                continue
            v = row.get(name)
            if v is None or v == "":
                continue
            if action == "encrypt":
                row[name] = encrypt_value(v, name, k)  # type: ignore[arg-type]
            elif action == "hash":
                row[name] = hash_value(v, k)  # type: ignore[arg-type]
            else:
                row[name] = mask_value(v, keep)
        out.append(row)
    return out


@connector("unprotect")
def unprotect(input: Any, fields: str | list[str], key: str | list[str]) -> list[dict]:
    """Decrypt fields that `protect` encrypted. `key`: the key, or a list of keys (any of them may
    have been used). Values stay out of the run store's samples."""
    names = [fields] if isinstance(fields, str) else list(fields)
    keys = _keys(key)
    out = []
    for r in single(input):
        row = dict(r)
        for name in names:
            v = row.get(name)
            if v is not None and v != "":
                row[name] = decrypt_value(v, name, keys)
        out.append(row)
    return out


rowwise("protect")
rowwise("unprotect")
