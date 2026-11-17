"""A small, tolerant HTML tree built on the standard library's html.parser.

Invoices exported as HTML are usually simple: a few tables, a definition
list, some paragraphs. This gives just enough of a DOM to query them by tag
and class, and to render them as text with table rows kept on one line.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Iterator

VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
     "meta", "source", "track", "wbr"}
)

BLOCK_TAGS = frozenset(
    {"address", "article", "aside", "blockquote", "body", "caption", "dd",
     "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form",
     "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "html", "li", "main",
     "nav", "ol", "p", "pre", "section", "table", "tbody", "tfoot", "thead",
     "tr", "ul", "br"}
)

SKIP_TAGS = frozenset({"head", "script", "style", "template", "title", "noscript"})

CELL_TAGS = ("td", "th")

# Opening one of these closes an unclosed sibling, as browsers do.
_IMPLIED_CLOSE = {
    "li": {"li"},
    "p": {"p"},
    "dt": {"dt", "dd"},
    "dd": {"dt", "dd"},
    "tr": {"tr", "td", "th"},
    "td": {"td", "th"},
    "th": {"td", "th"},
    "thead": {"thead", "tbody", "tfoot", "tr", "td", "th"},
    "tbody": {"thead", "tbody", "tfoot", "tr", "td", "th"},
    "tfoot": {"thead", "tbody", "tfoot", "tr", "td", "th"},
}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag: str, attrs=None, parent: "Node | None" = None):
        self.tag = tag
        self.attrs = {k: (v or "") for k, v in (attrs or [])}
        self.children: list["Node | str"] = []
        self.parent = parent

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Node {self.tag} {self.attrs}>"

    @property
    def classes(self) -> list[str]:
        return self.attrs.get("class", "").split()

    def elements(self) -> list["Node"]:
        """Direct child elements, skipping text."""
        return [c for c in self.children if isinstance(c, Node)]

    def iter(self) -> Iterator["Node"]:
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.iter()

    def find_all(self, tag: str | None = None, class_: str | None = None) -> list["Node"]:
        """Descendants matching tag and a class substring (e.g. "item" matches "line-items")."""
        out = []
        for node in self.iter():
            if node is self:
                continue
            if tag is not None and node.tag != tag:
                continue
            if class_ is not None and not any(class_ in c for c in node.classes):
                continue
            out.append(node)
        return out

    def find(self, tag: str | None = None, class_: str | None = None) -> "Node | None":
        found = self.find_all(tag, class_)
        return found[0] if found else None

    def text(self) -> str:
        parts: list[str] = []
        _collect_text(self, parts)
        return " ".join("".join(parts).split())

    def has_ancestor(self, tag: str) -> bool:
        node = self.parent
        while node is not None:
            if node.tag == tag:
                return True
            node = node.parent
        return False


def _collect_text(node: Node, parts: list[str]) -> None:
    if node.tag in SKIP_TAGS:
        return
    boundary = node.tag in BLOCK_TAGS or node.tag in CELL_TAGS
    if boundary:
        parts.append(" ")
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        else:
            _collect_text(child, parts)
    if boundary:
        parts.append(" ")


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.current = self.root

    def handle_starttag(self, tag, attrs):
        closes = _IMPLIED_CLOSE.get(tag)
        if closes:
            # Close an open sibling, but never climb out of a table or list.
            node = self.current
            while node is not self.root and node.tag not in ("table", "ul", "ol", "dl"):
                if node.tag in closes:
                    self.current = node.parent
                    break
                node = node.parent
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in VOID_TAGS:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.current.children.append(Node(tag, attrs, self.current))

    def handle_endtag(self, tag):
        node = self.current
        while node is not self.root:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent
        # A stray end tag with no matching open tag is ignored.

    def handle_data(self, data):
        if data:
            self.current.children.append(data)


def parse_html(source: str) -> Node:
    builder = _TreeBuilder()
    builder.feed(source)
    builder.close()
    return builder.root


def row_cells(tr: Node) -> list[str]:
    return [cell.text() for cell in tr.elements() if cell.tag in CELL_TAGS]


def to_text(root: Node) -> str:
    """Render the tree as lines of text; table cells are joined with " | "."""
    parts: list[str] = []

    def walk(node: Node) -> None:
        if node.tag in SKIP_TAGS:
            return
        if node.tag in CELL_TAGS:
            siblings = [c for c in node.parent.elements() if c.tag in CELL_TAGS] if node.parent else []
            if siblings and siblings[0] is not node:
                parts.append(" | ")
        elif node.tag in BLOCK_TAGS:
            parts.append("\n")
        for child in node.children:
            if isinstance(child, str):
                parts.append(child.replace("\n", " "))
            else:
                walk(child)
        if node.tag in BLOCK_TAGS:
            parts.append("\n")

    walk(root)
    lines = (" ".join(line.split()) for line in "".join(parts).split("\n"))
    return "\n".join(line for line in lines if line)
