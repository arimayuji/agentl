"""Brazilian electronic invoices: NF-e (model 55) and NFC-e (model 65) XML as records.

Reads authorised documents (`nfeProc`, with the SEFAZ protocol) and bare `NFe` documents, plus
cancellation events (`procEventoNFe`) that mark their note as cancelled. Nothing here talks to
SEFAZ, signs or issues invoices.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from ..registry import Rejects, connector
from ._util import origin, paths
from .files import MAX_EXTRACT_BYTES, _expand

Records = list[dict]

_NS = "{http://www.portalfiscal.inf.br/nfe}"
_CANCEL = {"110111", "110112"}  # cancellation, cancellation by substitution
_NON_DIGIT = re.compile(r"\D")


def chave_ok(chave: str) -> bool:
    """True when `chave` has 44 digits and a valid modulo-11 check digit."""
    if len(chave) != 44 or not chave.isdigit():
        return False
    total = sum(int(d) * (2 + i % 8) for i, d in enumerate(reversed(chave[:43])))
    rest = total % 11
    return int(chave[43]) == (0 if rest < 2 else 11 - rest)


def _find(node: ET.Element | None, path: str) -> ET.Element | None:
    if node is None:
        return None
    found = node.find("/".join(_NS + part for part in path.split("/")))
    if found is None:  # documents without the namespace
        found = node.find(path)
    return found


def _text(node: ET.Element | None, path: str) -> str | None:
    e = _find(node, path)
    if e is None or e.text is None:
        return None
    return e.text.strip()


def _num(node: ET.Element | None, path: str) -> float | None:
    t = _text(node, path)
    return float(t) if t not in (None, "") else None


def _doc(node: ET.Element | None) -> str | None:
    t = _text(node, "CNPJ") or _text(node, "CPF") or _text(node, "idEstrangeiro")
    return _NON_DIGIT.sub("", t) if t else None


def _first_child(node: ET.Element | None) -> ET.Element | None:
    """The single group inside ICMS, PIS, ... (ICMS00, ICMSSN102, PISAliq, ...)."""
    return next(iter(node), None) if node is not None else None


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _note(root: ET.Element) -> tuple[dict, list[ET.Element]]:
    nfe = root if _local(root.tag) == "NFe" else _find(root, "NFe")
    inf = _find(nfe, "infNFe")
    if inf is None:
        raise ValueError("no infNFe element")
    chave = (inf.get("Id") or "").removeprefix("NFe")
    ide, emit, dest = _find(inf, "ide"), _find(inf, "emit"), _find(inf, "dest")
    tot = _find(inf, "total/ICMSTot")
    prot = _find(root, "protNFe/infProt")
    rec: dict[str, Any] = {
        "chave": chave,
        "number": _text(ide, "nNF"),
        "series": _text(ide, "serie"),
        "model": _text(ide, "mod"),
        "issued_at": _text(ide, "dhEmi") or _text(ide, "dEmi"),
        "issuer_doc": _doc(emit),
        "issuer_name": _text(emit, "xNome"),
        "issuer_state": _text(emit, "enderEmit/UF"),
        "recipient_doc": _doc(dest),
        "recipient_name": _text(dest, "xNome"),
        "recipient_state": _text(dest, "enderDest/UF"),
        "total_products": _num(tot, "vProd"),
        "total_icms": _num(tot, "vICMS"),
        "total_ipi": _num(tot, "vIPI"),
        "total_pis": _num(tot, "vPIS"),
        "total_cofins": _num(tot, "vCOFINS"),
        "total_freight": _num(tot, "vFrete"),
        "total_discount": _num(tot, "vDesc"),
        "total": _num(tot, "vNF"),
        "protocol_status": _text(prot, "cStat"),
        "protocol_number": _text(prot, "nProt"),
        "protocol_at": _text(prot, "dhRecbto"),
        "cancelled": False,
    }
    dets = [e for e in inf if _local(e.tag) == "det"]
    return rec, dets


def _item(det: ET.Element) -> dict:
    prod, tax = _find(det, "prod"), _find(det, "imposto")
    icms = _first_child(_find(tax, "ICMS"))
    ipi = _find(tax, "IPI/IPITrib")
    pis = _first_child(_find(tax, "PIS"))
    cofins = _first_child(_find(tax, "COFINS"))
    return {
        "item": int(det.get("nItem") or 0) or None,
        "code": _text(prod, "cProd"),
        "description": _text(prod, "xProd"),
        "ncm": _text(prod, "NCM"),
        "cfop": _text(prod, "CFOP"),
        "unit": _text(prod, "uCom"),
        "quantity": _num(prod, "qCom"),
        "unit_value": _num(prod, "vUnCom"),
        "item_total": _num(prod, "vProd"),
        "icms_base": _num(icms, "vBC"),
        "icms": _num(icms, "vICMS"),
        "ipi_base": _num(ipi, "vBC"),
        "ipi": _num(ipi, "vIPI"),
        "pis_base": _num(pis, "vBC"),
        "pis": _num(pis, "vPIS"),
        "cofins_base": _num(cofins, "vBC"),
        "cofins": _num(cofins, "vCOFINS"),
    }


def _event(root: ET.Element) -> dict:
    inf = _find(root, "evento/infEvento") if _local(root.tag) != "evento" else _find(root, "infEvento")
    if inf is None:
        raise ValueError("no infEvento element")
    ret = _find(root, "retEvento/infEvento")
    return {
        "chave": _text(inf, "chNFe") or "",
        "type": _text(inf, "tpEvento"),
        "status": _text(ret, "cStat"),
        "at": _text(inf, "dhEvento"),
    }


def _parse(p: Path) -> ET.Element:
    data = p.read_bytes()
    if b"<!ENTITY" in data:  # no entity expansion ("billion laughs")
        raise ValueError("XML with entity declarations (<!ENTITY) is not read")
    return ET.fromstring(data)


@connector("nfe.read")
def nfe_read(
    input: Any = None,
    path: str | None = None,
    url: str | None = None,
    headers: dict | None = None,
    field: str = "path",
    level: str = "note",
    validate: bool = False,
    max_extract_bytes: int = MAX_EXTRACT_BYTES,
    storage_options: dict | None = None,
) -> Records:
    """NF-e and NFC-e XML (paths, globs, remote storage, zip) to records.

    `level: note` gives one record per invoice; `level: item` one per item with the note fields.
    Money and quantities are numbers, CNPJ/CPF keep only digits, dates stay ISO as in the XML.
    Cancellation event files mark their note `cancelled: true`. A file that cannot be read goes
    to the rejected records with `_error` and `_file`; `validate: true` also rejects notes with a
    bad `chave` check digit and events whose note is not among the files.
    """
    if level not in ("note", "item"):
        raise ValueError("nfe.read: level must be 'note' or 'item'")
    notes: list[tuple[dict, list[ET.Element]]] = []
    events: list[dict] = []
    bad: Records = []
    for p in paths(input, path, field, url, headers, storage_options):
        for f, archive in _expand(p, max_extract_bytes):
            shown = f"{archive.name}!{f.name}" if archive else origin(f)
            if f.suffix.lower() != ".xml":
                continue
            try:
                root = _parse(f)
                kind = _local(root.tag)
                if kind in ("procEventoNFe", "evento"):
                    events.append({**_event(root), "_file": shown})
                    continue
                if kind not in ("nfeProc", "NFe"):
                    raise ValueError(f"not an NF-e document (root <{kind}>)")
                rec, dets = _note(root)
            except Exception as e:  # a broken file never stops the run
                bad.append({"_file": shown, "_error": f"{shown}: {e}"})
                continue
            if validate and not chave_ok(rec["chave"]):
                bad.append({**rec, "_file": shown, "_error": f"{shown}: invalid chave {rec['chave']!r}"})
                continue
            rec["_file"] = shown
            notes.append((rec, dets))
    by_key = {rec["chave"]: rec for rec, _ in notes}
    for ev in events:
        note = by_key.get(ev["chave"])
        if note is None:
            if validate:
                bad.append({**ev, "_error": f"{ev['_file']}: event for unknown note {ev['chave']!r}"})
            continue
        if ev["type"] in _CANCEL and ev["status"] in (None, "135", "136", "155"):
            note["cancelled"] = True
            note["cancelled_at"] = ev["at"]
    out: Records = []
    for rec, dets in notes:
        if level == "note":
            out.append(rec)
        else:
            out.extend({**rec, **_item(d)} for d in dets)
    return Rejects(out, bad)
