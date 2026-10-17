"""A small HTML tree and CSS selector engine on the standard library, for `html.select`, `crawl`
and the tables of `html.read`.

Selectors: `tag`, `*`, `#id`, `.class`, `[attr]`, `[attr=v]`, `[attr~=v]`, `[attr^=v]`,
`[attr$=v]`, `[attr*=v]`, `:first-child`, `:last-child`, `:nth-child(n)`, `:nth-of-type(n)`,
the descendant (space) and child (`>`) combinators, and groups (`a, b`). That covers what list and
detail pages need; anything else fails with a clear error instead of matching nothing."""

from __future__ import annotations

import re
from collections.abc import Iterator
from html.parser import HTMLParser
from typing import Any

VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
# Opening one of these closes an open element of the same group, up to the given container.
_AUTOCLOSE = {
    "td": ({"td", "th"}, {"tr", "table"}),
    "th": ({"td", "th"}, {"tr", "table"}),
    "tr": ({"tr", "td", "th"}, {"table", "thead", "tbody", "tfoot"}),
    "thead": ({"thead", "tbody", "tfoot", "tr", "td", "th"}, {"table"}),
    "tbody": ({"thead", "tbody", "tfoot", "tr", "td", "th"}, {"table"}),
    "tfoot": ({"thead", "tbody", "tfoot", "tr", "td", "th"}, {"table"}),
    "li": ({"li"}, {"ul", "ol"}),
    "option": ({"option"}, {"select", "datalist"}),
    "dt": ({"dt", "dd"}, {"dl"}),
    "dd": ({"dt", "dd"}, {"dl"}),
    "p": ({"p"}, {"div", "section", "article", "body", "td", "th", "li"}),
}


class Node:
    __slots__ = ("attrs", "children", "parent", "tag")

    def __init__(self, tag: str, attrs: dict[str, str], parent: Node | None):
        self.tag = tag
        self.attrs = attrs
        self.parent = parent
        self.children: list[Node | str] = []

    def elements(self) -> list[Node]:
        return [c for c in self.children if isinstance(c, Node)]

    def iter(self) -> Iterator[Node]:
        """Descendants in document order (not self)."""
        for c in self.children:
            if isinstance(c, Node):
                yield c
                yield from c.iter()

    def text(self) -> str:
        parts: list[str] = []

        def walk(n: Node) -> None:
            for c in n.children:
                if isinstance(c, str):
                    parts.append(c)
                elif c.tag not in ("script", "style"):
                    if c.tag == "br":
                        parts.append(" ")
                    walk(c)

        walk(self)
        return " ".join("".join(parts).split())

    def html(self) -> str:
        """Inner HTML (re-serialised)."""
        return "".join(_serialise(c) for c in self.children)

    def select(self, selector: str) -> list[Node]:
        return select(self, selector)


def _serialise(n: Node | str) -> str:
    if isinstance(n, str):
        return n.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    attrs = "".join(f' {k}="{v.replace(chr(34), "&quot;")}"' for k, v in n.attrs.items())
    if n.tag in VOID:
        return f"<{n.tag}{attrs}>"
    return f"<{n.tag}{attrs}>{n.html()}</{n.tag}>"


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document", {}, None)
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        rule = _AUTOCLOSE.get(tag)
        if rule:
            closes, until = rule
            for i in range(len(self.stack) - 1, 0, -1):
                t = self.stack[i].tag
                if t in until:
                    break
                if t in closes:
                    del self.stack[i:]
                    break
        node = Node(tag, {k: v or "" for k, v in attrs}, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in VOID and self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return  # an end tag with nothing open is ignored, as browsers do

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def parse(html: str) -> Node:
    b = _Builder()
    b.feed(html)
    b.close()
    return b.root


# ---------------------------------------------------------------- CSS selectors
class SelectorError(ValueError):
    pass


_TOKEN = re.compile(
    r"""\s*(?:
      (?P<comb>[>+~,])
    | (?P<tag>[a-zA-Z][\w-]*|\*)
    | \#(?P<id>[\w-]+)
    | \.(?P<cls>[\w-]+)
    | \[\s*(?P<attr>[\w:-]+)\s*
        (?:(?P<op>[~^$*|]?=)\s*(?:"(?P<dq>[^"]*)"|'(?P<sq>[^']*)'|(?P<bare>[^\]\s]+))\s*)?\]
    | :(?P<pseudo>[\w-]+)(?:\(\s*(?P<arg>[^)]*?)\s*\))?
    )""",
    re.X,
)

Compound = list[tuple[str, Any]]


def _compile(selector: str) -> list[list[tuple[str, Compound]]]:
    """'div.card > a, h2' -> groups of [(combinator, compound)] read left to right."""
    groups: list[list[tuple[str, Compound]]] = [[]]
    comb = " "
    current: Compound | None = None
    pos = 0
    s = selector.strip()
    if not s:
        raise SelectorError("empty selector")
    while pos < len(s):
        ws = pos < len(s) and s[pos].isspace()
        m = _TOKEN.match(s, pos)
        if not m or m.end() == pos:
            raise SelectorError(f"cannot read the selector {selector!r} at {s[pos:]!r}")
        pos = m.end()
        if m.group("comb"):
            c = m.group("comb")
            if c in "+~":
                raise SelectorError(f"selector {selector!r}: the {c!r} combinator is not supported")
            if c == ",":
                groups.append([])
                comb, current = " ", None
            else:
                comb, current = ">", None
            continue
        if ws and current is not None:
            current = None  # whitespace between simple selectors: descendant
            comb = " "
        if current is None:
            current = []
            groups[-1].append((comb, current))
            comb = " "
        if m.group("tag"):
            if m.group("tag") != "*":
                current.append(("tag", m.group("tag").lower()))
        elif m.group("id"):
            current.append(("id", m.group("id")))
        elif m.group("cls"):
            current.append(("cls", m.group("cls")))
        elif m.group("attr"):
            value = next((v for v in (m.group("dq"), m.group("sq"), m.group("bare")) if v is not None), None)
            current.append(("attr", (m.group("attr").lower(), m.group("op"), value)))
        else:
            name, arg = m.group("pseudo"), m.group("arg")
            if name in ("first-child", "last-child"):
                current.append((name, None))
            elif name in ("nth-child", "nth-of-type") and arg and arg.isdigit():
                current.append((name, int(arg)))
            else:
                raise SelectorError(f"selector {selector!r}: :{name} is not supported")
    if any(not g for g in groups):
        raise SelectorError(f"cannot read the selector {selector!r}")
    return groups


def _siblings(n: Node) -> list[Node]:
    return n.parent.elements() if n.parent else [n]


def _matches(n: Node, compound: Compound) -> bool:
    for kind, arg in compound:
        if kind == "tag":
            if n.tag != arg:
                return False
        elif kind == "id":
            if n.attrs.get("id") != arg:
                return False
        elif kind == "cls":
            if arg not in n.attrs.get("class", "").split():
                return False
        elif kind == "attr":
            name, op, value = arg
            if name not in n.attrs:
                return False
            have = n.attrs[name]
            if op == "=" and have != value:
                return False
            if op == "~=" and value not in have.split():
                return False
            if op == "^=" and not have.startswith(value):
                return False
            if op == "$=" and not have.endswith(value):
                return False
            if op == "*=" and value not in have:
                return False
            if op == "|=" and have != value and not have.startswith(value + "-"):
                return False
        elif kind == "first-child":
            if _siblings(n)[0] is not n:
                return False
        elif kind == "last-child":
            if _siblings(n)[-1] is not n:
                return False
        elif kind == "nth-child":
            sib = _siblings(n)
            if len(sib) < arg or sib[arg - 1] is not n:
                return False
        elif kind == "nth-of-type":
            sib = [s for s in _siblings(n) if s.tag == n.tag]
            if len(sib) < arg or sib[arg - 1] is not n:
                return False
    return True


def _match_chain(n: Node, chain: list[tuple[str, Compound]], scope: Node) -> bool:
    comb, compound = chain[-1]
    if not _matches(n, compound):
        return False
    if len(chain) == 1:
        return True
    rest = chain[:-1]
    parent = n.parent
    if comb == ">":
        return parent is not None and parent is not scope and _match_chain(parent, rest, scope)
    while parent is not None and parent is not scope:
        if _match_chain(parent, rest, scope):
            return True
        parent = parent.parent
    return False


def select(scope: Node, selector: str) -> list[Node]:
    """Elements under `scope` matching `selector`, in document order."""
    groups = _compile(selector)
    return [n for n in scope.iter() if any(_match_chain(n, g, scope) for g in groups)]


# ---------------------------------------------------------------- tables
def _rows(table: Node) -> list[Node]:
    out: list[Node] = []

    def walk(n: Node) -> None:
        for c in n.elements():
            if c.tag == "tr":
                out.append(c)
            elif c.tag != "table":  # nested tables are tables of their own
                walk(c)

    walk(table)
    return out


def _span(n: Node, name: str) -> int:
    try:
        return max(1, min(int(n.attrs.get(name, "1") or 1), 1000))
    except ValueError:
        return 1


def grid(table: Node) -> list[list[str]]:
    """The cells of a table with colspan and rowspan repeated into every slot they cover."""
    out: list[list[str]] = []
    carry: dict[tuple[int, int], str] = {}
    for r, tr in enumerate(_rows(table)):
        cells = [c for c in tr.elements() if c.tag in ("td", "th")]
        if not cells and not any(k[0] == r for k in carry):
            continue
        row: list[str] = []
        col = 0
        for cell in cells:
            while (r, col) in carry:
                row.append(carry.pop((r, col)))
                col += 1
            text = cell.text()
            cs, rs = _span(cell, "colspan"), _span(cell, "rowspan")
            for i in range(cs):
                row.append(text)
                for k in range(1, rs):
                    carry[(r + k, col + i)] = text
            col += cs
        while (r, col) in carry:
            row.append(carry.pop((r, col)))
            col += 1
        out.append(row)
    return out


def header_names(rows: list[list[str]], width: int) -> list[str]:
    """One name per column from several header rows: 'Sales' over 'Q1' -> 'Sales Q1'."""
    names: list[str] = []
    for j in range(width):
        parts: list[str] = []
        for row in rows:
            v = row[j] if j < len(row) else ""
            if v and (not parts or parts[-1] != v):
                parts.append(v)
        names.append(" ".join(parts) or f"col_{j}")
    seen: dict[str, int] = {}
    for j, name in enumerate(names):
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            names[j] = f"{name}_{seen[name]}"
    return names
