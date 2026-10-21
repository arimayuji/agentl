"""`views:` on agent steps (#214): page cuts, PDF text and tables, compact HTML, per-view cache and
PDF library state isolation."""

import sys

import pytest

from agentl import parse, register_provider, views
from agentl.llm import Result, RowsChunk
from agentl.runner import Runner
from agentl.spec import SpecError
from agentl.store import Store

pytest.importorskip("pypdf")

SEEN: list[dict] = []


class Recording:
    """Records what each call receives and answers with a fixed value."""

    def __init__(self, spec):
        pass

    def structured(self, instruction, record, schema):
        SEEN.append(record)
        return Result(schema(summary="ok"), 10, 2)

    def rows(self, instruction, record, columns, previous):
        SEEN.append(record)
        return RowsChunk('["x", 1]', False, 10, 2)

    def text(self, instruction, content):
        return ""


register_provider("views-fake", Recording)


def make_pdf(pages: list[list[tuple[int, int, str]]]) -> bytes:
    """A minimal PDF with Helvetica text: one list of (x, y, text) per page."""
    objs: list[str | bytes] = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    kids = []
    for items in pages:
        body = "".join(f"BT /F1 12 Tf {x} {y} Td ({t}) Tj ET\n" for x, y, t in items).encode()
        objs.append(f"<< /Length {len(body)} >>\nstream\n".encode() + body + b"endstream")
        objs.append(
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {len(objs)} 0 R >>"
        )
        kids.append(len(objs))
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>"
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + (o.encode() if isinstance(o, str) else o) + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{n:010d} 00000 n \n" for n in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


PRICES = [
    [(72, 720, "Cover page")],
    [
        (72, 720, "Item"),
        (300, 720, "Price"),
        (72, 700, "Apple"),
        (300, 700, "1.50"),
        (72, 680, "Pear"),
        (300, 680, "2.25"),
    ],
    [(72, 720, "Appendix")],
]
DOCS: list[dict] = []


def src(input=None):
    return [dict(d) for d in DOCS]


def pipeline(views_, **step):
    return parse(
        {
            "name": "views",
            "llm": {"default": {"provider": "views-fake", "model": "m"}},
            "steps": [
                {"id": "src", "fn": "tests.test_views:src"},
                {
                    "id": "ask",
                    "input": "src",
                    "agent": "summarise",
                    "output": {"summary": "str"},
                    "views": views_,
                    **step,
                },
            ],
        }
    )


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "prices.pdf"
    p.write_bytes(make_pdf(PRICES))
    SEEN.clear()
    DOCS[:] = [{"id": 1, "path": str(p)}]
    return p


def test_page_numbers():
    assert views.page_numbers([1, "3-4", -1, 1], 5) == [0, 2, 3, 4]
    assert views.page_numbers(["2-"], 4) == [1, 2, 3]
    assert views.page_numbers(["-2--1", 9], 4) == [2, 3]
    assert views.page_numbers(None, 2) == [0, 1]


def test_pages_view_cuts_a_form_feed_text_before_sending(tmp_path):
    SEEN.clear()
    DOCS[:] = [{"id": 1, "text": "p1\fp2\fp3\fp4"}]
    res = Runner(pipeline({"document": {"pages": [2, "4-"]}}), store=Store(tmp_path / "s.db")).run()
    assert res.status == "succeeded", res.error
    assert SEEN == [{"id": 1, "document": "p2\fp4"}]  # the source field is not sent
    v = res.spend["ask"]["views"]
    assert v["pages_total"] == 4 and v["pages_sent"] == 2 and v["view_chars"] < v["source_chars"]


def test_pdf_text_and_tables_views(pdf, tmp_path):
    spec = pipeline(
        {"document": {"use": "pdf.text", "pages": [1]}, "tables": {"use": "pdf.tables", "pages": [2]}}
    )
    res = Runner(spec, store=Store(tmp_path / "s.db")).run()
    assert res.status == "succeeded", res.error
    (sent,) = SEEN
    assert "path" not in sent and sent["document"] == "Cover page"
    assert sent["tables"] == [{"page": 2, "rows": [["Item", "Price"], ["Apple", "1.50"], ["Pear", "2.25"]]}]
    assert res.spend["ask"]["views"]["pages_sent"] == 2 and res.spend["ask"]["views"]["pages_total"] == 6


def test_views_feed_chunk_by_page(pdf, tmp_path):
    spec = pipeline(
        {"text": {"use": "pdf.text", "pages": [1, 3]}},
        output_mode="rows",
        output={"item": "str", "value": "float"},
        chunk={"by": "page", "size": 1},
    )
    res = Runner(spec, store=Store(tmp_path / "s.db")).run()
    assert res.status == "succeeded", res.error
    assert [s["text"] for s in SEEN] == ["Cover page", "Appendix"]


def test_each_view_is_cached_by_the_document_hash(pdf, tmp_path, monkeypatch):
    built: list[str] = []
    real = views._BUILDERS["pdf.text"]
    monkeypatch.setitem(views._BUILDERS, "pdf.text", lambda v, d: built.append("x") or real(v, d))
    store = Store(tmp_path / "s.db")
    spec = pipeline({"document": {"use": "pdf.text", "pages": [1]}})
    Runner(spec, store=store).run()
    res = Runner(spec, store=store).run()
    assert built == ["x"] and res.spend["ask"]["views"]["cache_hits"] == 1
    # another page selection is another view; a changed document is another hash
    Runner(pipeline({"document": {"use": "pdf.text", "pages": [2]}}), store=store).run()
    pdf.write_bytes(make_pdf([[(72, 720, "New cover")]]))
    Runner(spec, store=store).run()
    assert built == ["x", "x", "x"]
    assert SEEN[-1]["document"] == "New cover"


def test_html_compact_view(tmp_path):
    SEEN.clear()
    html = (
        '<div class="x" id="y"><script>var a = 1;</script><style>p{}</style>'
        '<table border="1"><tr><td colspan="2" style="c">A  &amp;   B</td></tr></table></div>'
    )
    DOCS[:] = [{"html": html}]
    spec = pipeline(
        {
            "page": {"use": "html.compact", "keep_attrs": ["colspan", "rowspan"]},
            "raw": {"use": "html.compact", "from": "html", "drop": [], "keep_source": True},
        }
    )
    res = Runner(spec, store=Store(tmp_path / "s.db")).run()
    assert res.status == "succeeded", res.error
    (sent,) = SEEN
    assert sent["page"] == '<div><table><tr><td colspan="2">A &amp; B</td></tr></table></div>'
    assert "html" in sent and "var a" in sent["raw"]  # keep_source sends the original too


def test_a_record_whose_view_fails_is_rejected(tmp_path):
    SEEN.clear()
    DOCS[:] = [{"id": 1, "path": str(tmp_path / "missing.pdf")}, {"id": 2}]
    res = Runner(pipeline({"d": {"use": "pdf.text"}}), store=Store(tmp_path / "s.db")).run()
    assert SEEN == []
    errors = [r["_error"] for r in res.rejected["ask"]]
    assert "No such file" in errors[0] and "no field 'path'" in errors[1]


def test_pdf_settings_are_isolated_between_steps(pdf):
    import pypdf._page as page

    original = page.MERGE_CROP_BOX
    seen: list[str] = []
    real = views._reader

    def spy(data):
        seen.append(page.MERGE_CROP_BOX)
        return real(data)

    try:
        with views.isolated("pypdf"):  # captures the import-time baseline
            pass
        page.MERGE_CROP_BOX = "changed-by-another-step"
        views._reader = spy
        out, _, _ = views._pdf_text(views.ViewSpec(use="pdf.text"), pdf.read_bytes())
        assert "Cover page" in out
        assert seen == [original]  # the view read with the import-time setting
        assert page.MERGE_CROP_BOX == "changed-by-another-step"  # and gave the step its own back
    finally:
        views._reader = real
        page.MERGE_CROP_BOX = original
    assert "pypdf" in sys.modules


def test_spec_errors():
    with pytest.raises(SpecError, match="engine: ocr is not supported"):
        pipeline({"t": {"use": "pdf.tables", "engine": "ocr"}})
    with pytest.raises(SpecError, match="not a 1-based page"):
        pipeline({"t": {"pages": [0]}})
    with pytest.raises(SpecError, match="pages belongs"):
        pipeline({"t": {"use": "html.compact", "pages": [1]}})
    with pytest.raises(SpecError, match="views belongs to agent steps"):
        parse({"name": "x", "steps": [{"id": "a", "fn": "tests.test_views:src", "views": {"t": {}}}]})


def test_downstream_steps_see_the_original_record_not_the_views(tmp_path):
    SEEN.clear()
    DOCS[:] = [{"id": 1, "text": "p1\fp2\fp3"}]
    spec = parse(
        {
            "name": "views",
            "llm": {"default": {"provider": "views-fake", "model": "m"}},
            "steps": [
                {"id": "src", "fn": "tests.test_views:src"},
                {
                    "id": "ask",
                    "input": "src",
                    "agent": "summarise",
                    "output": {"summary": "str"},
                    "views": {"document": {"pages": [2]}},
                },
                {"id": "after", "input": "ask", "fn": "tests.test_views:passthrough"},
            ],
        }
    )
    res = Runner(spec, store=Store(tmp_path / "s.db")).run()
    assert res.status == "succeeded", res.error
    assert SEEN == [{"id": 1, "document": "p2"}]
    assert AFTER == [{"id": 1, "text": "p1\fp2\fp3", "summary": "ok"}]


AFTER: list[dict] = []


def passthrough(input):
    AFTER[:] = [dict(r) for r in input]
    return input


def test_plan_counts_cache_hits_on_the_viewed_records(pdf, tmp_path):
    import json

    from agentl.preflight import plan

    data = tmp_path / "docs.json"
    data.write_text(json.dumps(DOCS))
    store = Store(tmp_path / "s.db")
    spec = parse(
        {
            "name": "views",
            "llm": {"default": {"provider": "views-fake", "model": "m"}},
            "steps": [
                {"id": "src", "use": "json.read", "with": {"path": str(data)}},
                {
                    "id": "ask",
                    "input": "src",
                    "agent": "summarise",
                    "output": {"summary": "str"},
                    "views": {"document": {"use": "pdf.text", "pages": [1]}},
                },
            ],
        }
    )
    (before,) = [s for s in plan(spec, store).steps if s.step == "ask"]
    Runner(spec, store=store).run()
    (after,) = [s for s in plan(spec, store).steps if s.step == "ask"]
    assert (before.cache_hits, after.cache_hits) == (0, 1)
