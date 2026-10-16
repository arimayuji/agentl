"""Secret references in pipeline files, resolved when the file is loaded.

    ${aws-sm:prod/pg_dsn}            AWS Secrets Manager (a JSON secret: ${aws-sm:prod/db#password})
    ${ssm:/prod/pg_dsn}              AWS Systems Manager Parameter Store (decrypted)
    ${gcp-sm:my-project/pg_dsn}      Google Secret Manager, latest version (or .../pg_dsn/versions/3)
    ${azure-kv:my-vault/pg-dsn}      Azure Key Vault

The value lives only in memory: it is not written to the run store, events or logs. Each
reference is fetched once per process. Credentials come from each cloud's usual environment:
AWS (boto3 chain), Google (GOOGLE_ACCESS_TOKEN or GOOGLE_APPLICATION_CREDENTIALS), Azure
(AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, or AZURE_ACCESS_TOKEN)."""

from __future__ import annotations

import base64
import json
import os
import re
import threading
from collections.abc import Callable

SECRET = re.compile(r"\$\{(aws-sm|ssm|gcp-sm|azure-kv):([^}]+)\}")

_CACHE: dict[tuple[str, str], str] = {}
_LOCK = threading.Lock()
# Values of environment variables a pipeline used whose names say they are secrets, so the run
# store's samples can mask them too (see `sensitive`).
_ENV_SECRETS: set[str] = set()
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSW|PASSPHRASE|DSN|AUTH|CREDENTIAL|COOKIE", re.I)


def remember_env(name: str, value: str) -> None:
    if len(value) >= 6 and SECRET_NAME.search(name):
        with _LOCK:
            _ENV_SECRETS.add(value)


def sensitive() -> set[str]:
    """Secret values this process has resolved: cloud secret references, and environment variables
    with secret-looking names that a pipeline used."""
    with _LOCK:
        return {v for v in _CACHE.values() if len(v) >= 4} | set(_ENV_SECRETS)


def mask(text: str, values: set[str]) -> str:
    for v in sorted(values, key=len, reverse=True):
        if v in text:
            text = text.replace(v, "***")
    return text


def _json_key(value: str, key: str | None, ref: str) -> str:
    if key is None:
        return value
    try:
        data = json.loads(value)
    except json.JSONDecodeError as e:
        raise ValueError(f"secret '{ref}' is not JSON, so '#{key}' cannot be read from it") from e
    if key not in data:
        raise ValueError(f"secret '{ref}' has no key '{key}'")
    return str(data[key])


def _aws_sm(ref: str) -> str:
    from .registry import require

    name, _, key = ref.partition("#")
    client = require("boto3", "aws").client("secretsmanager")
    res = client.get_secret_value(SecretId=name)
    value = res.get("SecretString")
    if value is None:
        value = (
            base64.b64decode(res["SecretBinary"]).decode()
            if isinstance(res["SecretBinary"], str)
            else bytes(res["SecretBinary"]).decode()
        )
    return _json_key(value, key or None, ref)


def _ssm(ref: str) -> str:
    from .registry import require

    client = require("boto3", "aws").client("ssm")
    return client.get_parameter(Name=ref, WithDecryption=True)["Parameter"]["Value"]


def _gcp_sm(ref: str) -> str:
    from .connectors.saas import _call, google_token

    name, _, key = ref.partition("#")
    parts = name.strip("/").split("/")
    if len(parts) == 2:  # project/secret
        path = f"projects/{parts[0]}/secrets/{parts[1]}/versions/latest"
    elif len(parts) == 4 and parts[2] == "versions":  # project/secret/versions/N
        path = f"projects/{parts[0]}/secrets/{parts[1]}/versions/{parts[3]}"
    else:
        raise ValueError(f"gcp-sm reference '{ref}': use project/secret or project/secret/versions/N")
    api = os.environ.get("AGENTL_GCP_SECRETS_API", "https://secretmanager.googleapis.com/v1")
    token = google_token(None, None, ["https://www.googleapis.com/auth/cloud-platform"])
    data = _call("GET", f"{api}/{path}:access", token)["payload"]["data"]
    return _json_key(base64.b64decode(data).decode(), key or None, ref)


def _azure_kv(ref: str) -> str:
    from .connectors.saas import _call, ms_token

    name, _, key = ref.partition("#")
    vault, _, secret = name.partition("/")
    if not vault or not secret:
        raise ValueError(f"azure-kv reference '{ref}': use vault-name/secret-name")
    base = os.environ.get("AGENTL_AZURE_KV_URL", "https://{vault}.vault.azure.net").format(vault=vault)
    token = ms_token(
        os.environ.get("AZURE_ACCESS_TOKEN"),
        os.environ.get("AZURE_TENANT_ID"),
        None,
        scope="https://vault.azure.net/.default",
        client=(os.environ.get("AZURE_CLIENT_ID"), os.environ.get("AZURE_CLIENT_SECRET")),
    )
    value = _call("GET", f"{base}/secrets/{secret}?api-version=7.4", token)["value"]
    return _json_key(value, key or None, ref)


RESOLVERS: dict[str, Callable[[str], str]] = {
    "aws-sm": _aws_sm,
    "ssm": _ssm,
    "gcp-sm": _gcp_sm,
    "azure-kv": _azure_kv,
}


def resolve(kind: str, ref: str) -> str:
    with _LOCK:
        if (kind, ref) in _CACHE:
            return _CACHE[(kind, ref)]
    value = RESOLVERS[kind](ref.strip())
    with _LOCK:
        _CACHE[(kind, ref)] = value
    return value


def substitute(text: str) -> str:
    return SECRET.sub(lambda m: resolve(m.group(1), m.group(2)), text)
