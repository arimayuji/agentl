"""nfe.read on synthetic invoices (fake CNPJs with valid check digits, never real documents)."""

import zipfile

import pytest

from agentl.connectors.nfe import chave_ok
from agentl.registry import get

NS = "http://www.portalfiscal.inf.br/nfe"


def cnpj(base12: str) -> str:
    digits = [int(d) for d in base12]
    for weights in ([5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2], [6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]):
        rest = sum(d * w for d, w in zip(digits, weights, strict=True)) % 11
        digits.append(0 if rest < 2 else 11 - rest)
    return "".join(map(str, digits))


ISSUER = cnpj("112223330001")


def chave(model: str = "55", number: int = 1) -> str:
    body = f"35{2610}{ISSUER}{model}001{number:09d}1{12345678:08d}"
    total = sum(int(d) * (2 + i % 8) for i, d in enumerate(reversed(body)))
    rest = total % 11
    return body + str(0 if rest < 2 else 11 - rest)


def item_xml(n: int, qty: str, price: str, total: str) -> str:
    return f"""<det nItem="{n}"><prod><cProd>P{n}</cProd><xProd>Product {n}</xProd><NCM>84713012</NCM>
<CFOP>5102</CFOP><uCom>UN</uCom><qCom>{qty}</qCom><vUnCom>{price}</vUnCom><vProd>{total}</vProd></prod>
<imposto><ICMS><ICMS00><orig>0</orig><CST>00</CST><vBC>{total}</vBC><pICMS>18.00</pICMS>
<vICMS>1.80</vICMS></ICMS00></ICMS><IPI><cEnq>999</cEnq><IPITrib><CST>50</CST><vBC>{total}</vBC>
<pIPI>5.00</pIPI><vIPI>0.50</vIPI></IPITrib></IPI><PIS><PISAliq><CST>01</CST><vBC>{total}</vBC>
<pPIS>1.65</pPIS><vPIS>0.17</vPIS></PISAliq></PIS><COFINS><COFINSAliq><CST>01</CST>
<vBC>{total}</vBC><pCOFINS>7.60</pCOFINS><vCOFINS>0.76</vCOFINS></COFINSAliq></COFINS></imposto></det>"""


def nfe_xml(key: str, model: str = "55", proc: bool = True, ns: bool = True) -> str:
    dest = (
        "<dest><CPF>123.456.789-09</CPF><xNome>Sample Buyer</xNome><enderDest><UF>RJ</UF></enderDest></dest>"
        if model == "55"
        else ""
    )
    xmlns = f' xmlns="{NS}"' if ns else ""
    nfe = f"""<NFe{xmlns}><infNFe Id="NFe{key}" versao="4.00"><ide><cUF>35</cUF><mod>{model}</mod>
<serie>1</serie><nNF>{int(key[25:34])}</nNF><dhEmi>2026-10-01T10:00:00-03:00</dhEmi></ide>
<emit><CNPJ>{ISSUER[:2]}.{ISSUER[2:5]}.{ISSUER[5:8]}/{ISSUER[8:12]}-{ISSUER[12:]}</CNPJ>
<xNome>Sample Seller Ltd</xNome><enderEmit><UF>SP</UF></enderEmit></emit>{dest}
{item_xml(1, "2.0000", "5.00", "10.00")}{item_xml(2, "1.5000", "4.00", "6.00")}
<total><ICMSTot><vBC>16.00</vBC><vICMS>3.60</vICMS><vProd>16.00</vProd><vFrete>2.00</vFrete>
<vDesc>1.00</vDesc><vIPI>1.00</vIPI><vPIS>0.34</vPIS><vCOFINS>1.52</vCOFINS><vNF>18.00</vNF>
</ICMSTot></total></infNFe></NFe>"""
    if not proc:
        return nfe
    return f"""<?xml version="1.0" encoding="UTF-8"?><nfeProc{xmlns} versao="4.00">{nfe}<protNFe>
<infProt><chNFe>{key}</chNFe><dhRecbto>2026-10-01T10:00:05-03:00</dhRecbto><nProt>135260000000001</nProt>
<cStat>100</cStat></infProt></protNFe></nfeProc>"""


def cancel_xml(key: str) -> str:
    return f"""<procEventoNFe xmlns="{NS}"><evento><infEvento><chNFe>{key}</chNFe>
<dhEvento>2026-10-02T09:00:00-03:00</dhEvento><tpEvento>110111</tpEvento></infEvento></evento>
<retEvento><infEvento><cStat>135</cStat></infEvento></retEvento></procEventoNFe>"""


read = get("nfe.read")


def test_chave_check_digit():
    key = chave()
    assert chave_ok(key)
    assert not chave_ok(key[:-1] + str((int(key[-1]) + 1) % 10))
    assert not chave_ok("123")


def test_note_level_with_protocol(tmp_path):
    key = chave()
    (tmp_path / "a.xml").write_text(nfe_xml(key), encoding="utf-8")
    [rec] = read(path=str(tmp_path / "*.xml"))
    assert rec["chave"] == key
    assert (rec["number"], rec["series"], rec["model"]) == ("1", "1", "55")
    assert rec["issued_at"] == "2026-10-01T10:00:00-03:00"
    assert rec["issuer_doc"] == ISSUER and rec["issuer_state"] == "SP"
    assert rec["recipient_doc"] == "12345678909" and rec["recipient_state"] == "RJ"
    assert rec["total"] == 18.0 and rec["total_freight"] == 2.0 and rec["total_discount"] == 1.0
    assert rec["protocol_status"] == "100" and rec["protocol_number"] == "135260000000001"
    assert rec["cancelled"] is False


def test_item_level_bare_nfe_without_namespace(tmp_path):
    (tmp_path / "a.xml").write_text(nfe_xml(chave(), proc=False, ns=False), encoding="utf-8")
    items = read(path=str(tmp_path / "*.xml"), level="item")
    assert [i["item"] for i in items] == [1, 2]
    first = items[0]
    assert first["quantity"] == 2.0 and first["unit_value"] == 5.0 and first["item_total"] == 10.0
    assert (first["ncm"], first["cfop"], first["unit"]) == ("84713012", "5102", "UN")
    assert first["icms"] == 1.8 and first["ipi"] == 0.5 and first["pis"] == 0.17 and first["cofins"] == 0.76
    assert first["protocol_status"] is None and first["issuer_doc"] == ISSUER


def test_nfce_zip_broken_file_and_cancellation(tmp_path):
    nfce, nfe = chave("65", 7), chave("55", 2)
    with zipfile.ZipFile(tmp_path / "batch.zip", "w") as z:
        z.writestr("nfce.xml", nfe_xml(nfce, model="65"))
        z.writestr("nfe.xml", nfe_xml(nfe))
        z.writestr("nfe-cancel.xml", cancel_xml(nfe))
        z.writestr("broken.xml", "<nfeProc><NFe>")
        z.writestr("readme.txt", "ignored")
    out = read(path=str(tmp_path / "batch.zip"))
    by_model = {r["model"]: r for r in out}
    assert by_model["65"]["recipient_doc"] is None and by_model["65"]["cancelled"] is False
    assert by_model["55"]["cancelled"] is True
    assert by_model["55"]["cancelled_at"] == "2026-10-02T09:00:00-03:00"
    assert by_model["65"]["_file"] == "batch.zip!nfce.xml"
    [bad] = out.rejected
    assert bad["_file"] == "batch.zip!broken.xml" and "broken.xml" in bad["_error"]


def test_validate_rejects_bad_chave_and_orphan_event(tmp_path):
    good = chave()
    wrong = good[:-1] + str((int(good[-1]) + 1) % 10)
    (tmp_path / "good.xml").write_text(nfe_xml(good), encoding="utf-8")
    (tmp_path / "wrong.xml").write_text(nfe_xml(wrong), encoding="utf-8")
    (tmp_path / "orphan.xml").write_text(cancel_xml(chave("55", 99)), encoding="utf-8")
    (tmp_path / "other.xml").write_text("<root><a>1</a></root>", encoding="utf-8")
    out = read(path=str(tmp_path / "*.xml"), validate=True)
    assert [r["chave"] for r in out] == [good]
    errors = sorted(r["_error"] for r in out.rejected)
    assert len(errors) == 3
    assert any("invalid chave" in e for e in errors)
    assert any("unknown note" in e for e in errors)
    assert any("not an NF-e" in e for e in errors)
    # without validate the bad chave is read and the orphan event ignored
    assert len(read(path=str(tmp_path / "*.xml"))) == 2


def test_bad_level():
    with pytest.raises(ValueError, match="level"):
        read(path="none/*.xml", level="line")
