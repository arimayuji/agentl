"""GraphQL, Google Sheets, Google Drive and SharePoint / OneDrive.

All HTTP goes through the standard library and the same network guard as the HTTP connectors.
Credentials come from the environment (or `${VAR}` in the YAML), never from the file itself:

- Google: `GOOGLE_ACCESS_TOKEN`, or a service account JSON in `GOOGLE_APPLICATION_CREDENTIALS`
  (needs `agentl-etl[google]` to sign the token request).
- Microsoft Graph: `MS_ACCESS_TOKEN`, or an app registration with `MS_TENANT_ID`, `MS_CLIENT_ID`
  and `MS_CLIENT_SECRET` (client credentials flow, standard library only).

Every connector takes `api_url` (and the auth ones `token_url`) so it can point to a test server
or a sovereign cloud."""

from __future__ import annotations

import fnmatch
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .. import netguard
from ..registry import connector, require
from ._util import single, tmpdir

SHEETS_API = "https://sheets.googleapis.com/v4"
DRIVE_API = "https://www.googleapis.com/drive/v3"
GRAPH_API = "https://graph.microsoft.com/v1.0"
MS_LOGIN = "https://login.microsoftonline.com"


# ---------------------------------------------------------------- http
def _call(
    method: str,
    url: str,
    token: str | None = None,
    body: Any = None,
    data: bytes | None = None,
    headers: dict | None = None,
    timeout: float = 60,
    retries: int = 2,
    raw: bool = False,
) -> Any:
    hdrs = dict(headers or {})
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    if body is not None:
        data = json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    for attempt in range(retries + 1):
        wait = float(2**attempt)
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with netguard.urlopen(req, timeout=timeout) as resp:
                content = resp.read()
                if raw:
                    return content
                return json.loads(content) if content.strip() else {}
        except urllib.error.HTTPError as e:
            if (e.code < 500 and e.code != 429) or attempt == retries:
                detail = e.read().decode(errors="replace")[:500]
                raise RuntimeError(f"{method} {url.split('?')[0]}: HTTP {e.code} {detail}") from e
            after = e.headers.get("Retry-After") if e.headers else None
            if after and after.strip().isdigit():  # Graph throttling says how long to wait
                wait = min(float(after), 120.0)
        except urllib.error.URLError:
            if attempt == retries:
                raise
        time.sleep(wait)
    raise AssertionError("unreachable")


def _q(url: str, params: dict) -> str:
    return f"{url}?{urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})}"


# ---------------------------------------------------------------- auth
_TOKENS: dict[tuple, tuple[str, float]] = {}
_TOKEN_LOCK = threading.Lock()


def _cached(key: tuple, fetch: Any) -> str:
    with _TOKEN_LOCK:
        hit = _TOKENS.get(key)
        if hit and hit[1] > time.time() + 60:
            return hit[0]
        token, expires_in = fetch()
        _TOKENS[key] = (token, time.time() + float(expires_in))
        return token


def google_token(access_token: str | None, credentials: str | None, scopes: list[str]) -> str:
    token = access_token or os.environ.get("GOOGLE_ACCESS_TOKEN")
    if token:
        return token
    path = credentials or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not path:
        raise ValueError(
            "Google needs GOOGLE_ACCESS_TOKEN or a service account file in GOOGLE_APPLICATION_CREDENTIALS"
        )

    def fetch() -> tuple[str, float]:
        sa = require("google.oauth2.service_account", "google")
        transport = require("google.auth.transport.requests", "google")
        creds = sa.Credentials.from_service_account_file(path, scopes=scopes)
        creds.refresh(transport.Request())
        left = (creds.expiry.timestamp() - time.time()) if creds.expiry else 3000
        return creds.token, left

    return _cached(("google", path, tuple(scopes)), fetch)


def ms_token(
    access_token: str | None,
    tenant: str | None,
    token_url: str | None,
    scope: str = "https://graph.microsoft.com/.default",
    client: tuple[str | None, str | None] | None = None,
) -> str:
    """A Microsoft Entra token: a ready one, or the client credentials flow. `client` overrides
    the MS_CLIENT_ID / MS_CLIENT_SECRET pair (Key Vault passes the AZURE_* names)."""
    token = access_token or os.environ.get("MS_ACCESS_TOKEN")
    if token:
        return token
    tenant = tenant or os.environ.get("MS_TENANT_ID")
    client_id, secret = (
        client
        if client and all(client)
        else (
            os.environ.get("MS_CLIENT_ID"),
            os.environ.get("MS_CLIENT_SECRET"),
        )
    )
    if not (tenant and client_id and secret):
        raise ValueError(
            "Microsoft needs an access token, or a tenant id, client id and client secret "
            "(MS_TENANT_ID, MS_CLIENT_ID, MS_CLIENT_SECRET; AZURE_* for Key Vault)"
        )
    url = token_url or f"{MS_LOGIN}/{tenant}/oauth2/v2.0/token"

    def fetch() -> tuple[str, float]:
        form = urllib.parse.urlencode(
            {
                "grant_type": "client_credentials",
                "client_id": client_id,
                "client_secret": secret,
                "scope": scope,
            }
        ).encode()
        res = _call("POST", url, data=form, headers={"Content-Type": "application/x-www-form-urlencoded"})
        return res["access_token"], res.get("expires_in", 3600)

    return _cached(("ms", url, client_id, scope), fetch)


# ---------------------------------------------------------------- GraphQL
def _dig(data: Any, path: str | None) -> Any:
    for part in (path or "").split(".") if path else []:
        data = data.get(part) if isinstance(data, dict) else None
    return data


@connector("graphql.query")
def graphql_query(
    input: Any = None,
    url: str = "",
    query: str = "",
    variables: dict | None = None,
    headers: dict | None = None,
    items: str | None = None,
    page_info: str | None = None,
    cursor_var: str = "after",
    max_pages: int = 1000,
    timeout: float = 30,
    retries: int = 2,
) -> list[dict]:
    """POST a GraphQL query. `items` is the path to the list in `data` (e.g. `repository.issues.nodes`).
    With `page_info` (e.g. `repository.issues.pageInfo`), follows `endCursor` while `hasNextPage`,
    passing it as `$after` (`cursor_var`). With upstream records, runs once per record and fills
    `{field}` placeholders in the variables."""
    if not url or not query:
        raise ValueError("graphql.query needs url and query")

    def fill(v: Any, rec: dict) -> Any:
        if isinstance(v, str):
            return v.format(**rec)
        if isinstance(v, dict):
            return {k: fill(x, rec) for k, x in v.items()}
        if isinstance(v, list):
            return [fill(x, rec) for x in v]
        return v

    def run(rec: dict) -> list[dict]:
        out: list[dict] = []
        vars_ = fill(variables or {}, rec)
        for _ in range(max_pages):
            res = _call(
                "POST",
                url,
                body={"query": query, "variables": vars_},
                headers=headers,
                timeout=timeout,
                retries=retries,
            )
            if res.get("errors"):
                raise RuntimeError("GraphQL: " + "; ".join(e.get("message", str(e)) for e in res["errors"]))
            data = _dig(res.get("data") or {}, items) if items else res.get("data")
            rows = data if isinstance(data, list) else [data] if data else []
            out.extend(r if isinstance(r, dict) else {"value": r} for r in rows)
            info = _dig(res.get("data") or {}, page_info) if page_info else None
            if not info or not info.get("hasNextPage"):
                return out
            vars_ = {**vars_, cursor_var: info.get("endCursor")}
        raise RuntimeError(f"graphql.query: pagination stopped after max_pages={max_pages}")

    upstream = single(input)
    return [r for rec in (upstream or [{}]) for r in run(rec)]


# ---------------------------------------------------------------- Google Sheets
_SHEETS_SCOPE = ["https://www.googleapis.com/auth/spreadsheets"]


def _cell(v: Any) -> Any:
    if v is None:
        return ""
    if isinstance(v, (str, int, float, bool)):
        return v
    return json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (list, dict)) else str(v)


@connector("gsheets.read")
def gsheets_read(
    input: Any = None,
    spreadsheet_id: str = "",
    range: str = "Sheet1",
    access_token: str | None = None,
    credentials: str | None = None,
    api_url: str = SHEETS_API,
) -> list[dict]:
    """A sheet range as records; the first row is the header. Empty trailing cells become ''."""
    token = google_token(access_token, credentials, _SHEETS_SCOPE)
    rng = urllib.parse.quote(range, safe="")
    res = _call(
        "GET",
        _q(
            f"{api_url}/spreadsheets/{spreadsheet_id}/values/{rng}",
            {"valueRenderOption": "UNFORMATTED_VALUE", "dateTimeRenderOption": "FORMATTED_STRING"},
        ),
        token,
    )
    values = res.get("values") or []
    if not values:
        return []
    header = [str(h) or f"col_{i}" for i, h in enumerate(values[0])]
    return [{h: (row[i] if i < len(row) else "") for i, h in enumerate(header)} for row in values[1:]]


@connector("gsheets.write")
def gsheets_write(
    input: Any,
    spreadsheet_id: str = "",
    range: str = "Sheet1",
    mode: str = "replace",
    access_token: str | None = None,
    credentials: str | None = None,
    api_url: str = SHEETS_API,
) -> list[dict]:
    """`mode: replace` clears the range and writes a header plus the rows; `append` adds rows under
    the existing data (no header). Values are entered as a user would type them."""
    if mode not in ("replace", "append"):
        raise ValueError("gsheets.write mode must be 'replace' or 'append'")
    rows = single(input)
    cols: list[str] = []
    for r in rows:
        cols.extend(k for k in r if k not in cols)
    body_rows = [[_cell(r.get(c)) for c in cols] for r in rows]
    token = google_token(access_token, credentials, _SHEETS_SCOPE)
    base = f"{api_url}/spreadsheets/{spreadsheet_id}/values/{urllib.parse.quote(range, safe='')}"
    if mode == "replace":
        _call("POST", f"{base}:clear", token, body={})
        res = _call(
            "PUT",
            _q(base, {"valueInputOption": "USER_ENTERED"}),
            token,
            body={"range": range, "values": [cols, *body_rows]},
        )
    else:
        res = _call(
            "POST",
            _q(f"{base}:append", {"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"}),
            token,
            body={"values": body_rows},
        )
    updated = res.get("updatedRange") or (res.get("updates") or {}).get("updatedRange")
    return [{"written": len(rows), "range": updated or range}]


# ---------------------------------------------------------------- Google Drive
_DRIVE_SCOPE = ["https://www.googleapis.com/auth/drive"]
_EXPORT = {  # Google-native files have no bytes of their own: export them
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
}


def _safe_name(name: str) -> str:
    return name.replace("/", "_").replace("\\", "_") or "file"


@connector("gdrive.files")
def gdrive_files(
    input: Any = None,
    folder_id: str | None = None,
    pattern: str = "*",
    query: str | None = None,
    download: bool = True,
    access_token: str | None = None,
    credentials: str | None = None,
    api_url: str = DRIVE_API,
) -> list[dict]:
    """Files in a Drive folder (or matching a Drive `query`), filtered by name `pattern`, downloaded
    to a temp folder: one record per file with `path` for `file.read`, plus `name`, `id`,
    `modified`. Google Sheets/Docs are exported as .xlsx/.docx, Slides as .pdf."""
    token = google_token(access_token, credentials, _DRIVE_SCOPE)
    q = query or (f"'{folder_id}' in parents" if folder_id else None)
    if not q:
        raise ValueError("gdrive.files needs folder_id or query")
    q += " and trashed = false and mimeType != 'application/vnd.google-apps.folder'"
    files: list[dict] = []
    page = None
    while True:
        res = _call(
            "GET",
            _q(
                f"{api_url}/files",
                {
                    "q": q,
                    "pageToken": page,
                    "pageSize": 1000,
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                    "fields": "nextPageToken,files(id,name,mimeType,modifiedTime,size)",
                },
            ),
            token,
        )
        files += res.get("files", [])
        page = res.get("nextPageToken")
        if not page:
            break
    out = []
    folder = tmpdir("gdrive-")
    for f in files:
        name = f["name"]
        export = _EXPORT.get(f.get("mimeType", ""))
        if export and not name.endswith(export[1]):
            name += export[1]
        if not fnmatch.fnmatch(name, pattern):
            continue
        rec = {"name": name, "id": f["id"], "modified": f.get("modifiedTime"), "mime_type": f.get("mimeType")}
        if download:
            if export:
                url = _q(f"{api_url}/files/{f['id']}/export", {"mimeType": export[0]})
            else:
                url = _q(f"{api_url}/files/{f['id']}", {"alt": "media", "supportsAllDrives": "true"})
            path = folder / _safe_name(name)
            path.write_bytes(_call("GET", url, token, raw=True))
            rec["path"] = str(path)
        out.append(rec)
    return out


@connector("gdrive.upload")
def gdrive_upload(
    input: Any,
    folder_id: str = "",
    field: str = "path",
    access_token: str | None = None,
    credentials: str | None = None,
    api_url: str = DRIVE_API,
) -> list[dict]:
    """Upload the local files named in each record's `path` (e.g. the output of `excel.write`) to a
    Drive folder. A file with the same name in the folder is replaced (new version, same id)."""
    token = google_token(access_token, credentials, _DRIVE_SCOPE)
    upload_api = api_url.replace("/drive/v3", "/upload/drive/v3")
    out = []
    for rec in single(input):
        local = Path(rec[field])
        name = local.name.replace("'", "\\'")
        existing = _call(
            "GET",
            _q(
                f"{api_url}/files",
                {
                    "q": f"'{folder_id}' in parents and name = '{name}' and trashed = false",
                    "fields": "files(id)",
                    "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                },
            ),
            token,
        ).get("files", [])
        content = local.read_bytes()
        if existing:
            res = _call(
                "PATCH",
                _q(
                    f"{upload_api}/files/{existing[0]['id']}",
                    {"uploadType": "media", "supportsAllDrives": "true"},
                ),
                token,
                data=content,
                headers={"Content-Type": "application/octet-stream"},
            )
        else:
            boundary = "agentl-upload-boundary"
            meta = json.dumps({"name": local.name, "parents": [folder_id]}).encode()
            body = (
                f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n".encode()
                + meta
                + f"\r\n--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n".encode()
                + content
                + f"\r\n--{boundary}--".encode()
            )
            res = _call(
                "POST",
                _q(f"{upload_api}/files", {"uploadType": "multipart", "supportsAllDrives": "true"}),
                token,
                data=body,
                headers={"Content-Type": f"multipart/related; boundary={boundary}"},
            )
        out.append(
            {"name": local.name, "id": res.get("id"), "bytes": len(content), "replaced": bool(existing)}
        )
    return out


# ---------------------------------------------------------------- SharePoint / OneDrive
SIMPLE_UPLOAD_MAX = 4 * 1024 * 1024
CHUNK = 320 * 1024 * 16  # 5 MiB, a multiple of 320 KiB as Graph requires


def _drive(api_url: str, drive_id: str | None, site: str | None, user: str | None, token: str) -> str:
    if drive_id:
        return f"{api_url}/drives/{drive_id}"
    if site:  # "contoso.sharepoint.com:/sites/Finance"
        site_id = _call("GET", f"{api_url}/sites/{site}", token)["id"]
        return f"{api_url}/sites/{site_id}/drive"
    if user:
        return f"{api_url}/users/{user}/drive"
    raise ValueError("set drive_id, site (e.g. contoso.sharepoint.com:/sites/Finance) or user")


def _path(folder: str) -> str:
    return urllib.parse.quote("/" + folder.strip("/"))


@connector("sharepoint.files")
def sharepoint_files(
    input: Any = None,
    folder: str = "/",
    pattern: str = "*",
    drive_id: str | None = None,
    site: str | None = None,
    user: str | None = None,
    download: bool = True,
    access_token: str | None = None,
    tenant: str | None = None,
    api_url: str = GRAPH_API,
    token_url: str | None = None,
) -> list[dict]:
    """Files in a SharePoint document library or OneDrive folder, filtered by name `pattern`, downloaded
    to a temp folder: one record per file with `path` for `file.read`, plus `name`, `id`, `modified`."""
    token = ms_token(access_token, tenant, token_url)
    base = _drive(api_url, drive_id, site, user, token)
    url = f"{base}/root/children" if folder.strip("/") == "" else f"{base}/root:{_path(folder)}:/children"
    items: list[dict] = []
    while url:
        res = _call("GET", url, token)
        items += res.get("value", [])
        url = res.get("@odata.nextLink")
    out = []
    dest = tmpdir("sharepoint-")
    for it in items:
        if "file" not in it or not fnmatch.fnmatch(it["name"], pattern):
            continue
        rec = {
            "name": it["name"],
            "id": it["id"],
            "modified": it.get("lastModifiedDateTime"),
            "size": it.get("size"),
        }
        if download:
            path = dest / _safe_name(it["name"])
            direct = it.get("@microsoft.graph.downloadUrl")  # pre-signed: no Authorization header
            path.write_bytes(
                _call("GET", direct, raw=True)
                if direct
                else _call("GET", f"{base}/items/{it['id']}/content", token, raw=True)
            )
            rec["path"] = str(path)
        out.append(rec)
    return out


@connector("sharepoint.upload")
def sharepoint_upload(
    input: Any,
    folder: str = "/",
    field: str = "path",
    drive_id: str | None = None,
    site: str | None = None,
    user: str | None = None,
    access_token: str | None = None,
    tenant: str | None = None,
    api_url: str = GRAPH_API,
    token_url: str | None = None,
) -> list[dict]:
    """Upload the local files named in each record's `path` (e.g. the output of `excel.write`) to a
    SharePoint or OneDrive folder, replacing a file with the same name. Files over 4 MB go through an
    upload session in 5 MiB chunks."""
    token = ms_token(access_token, tenant, token_url)
    base = _drive(api_url, drive_id, site, user, token)
    out = []
    for rec in single(input):
        local = Path(rec[field])
        target = _path(f"{folder.strip('/')}/{local.name}")
        size = local.stat().st_size
        if size <= SIMPLE_UPLOAD_MAX:
            res = _call(
                "PUT",
                f"{base}/root:{target}:/content",
                token,
                data=local.read_bytes(),
                headers={"Content-Type": "application/octet-stream"},
            )
        else:
            session = _call(
                "POST",
                f"{base}/root:{target}:/createUploadSession",
                token,
                body={"item": {"@microsoft.graph.conflictBehavior": "replace"}},
            )
            res = {}
            with local.open("rb") as f:
                start = 0
                while start < size:
                    chunk = f.read(CHUNK)
                    end = start + len(chunk) - 1
                    res = _call(
                        "PUT",
                        session["uploadUrl"],
                        data=chunk,
                        headers={
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {start}-{end}/{size}",
                        },
                    )
                    start = end + 1
        out.append({"name": local.name, "id": res.get("id"), "bytes": size, "web_url": res.get("webUrl")})
    return out
