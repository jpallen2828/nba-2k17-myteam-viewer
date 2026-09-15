from __future__ import annotations

from html.parser import HTMLParser
from typing import Callable, Iterable, Iterator


class Element:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(
        self,
        tag: str,
        attrs: Iterable[tuple[str, str | None]] = (),
        parent: "Element | None" = None,
    ) -> None:
        self.tag = tag.casefold()
        self.attrs = {key.casefold(): value or "" for key, value in attrs}
        self.children: list[Element | str] = []
        self.parent = parent

    def get(self, name: str, default: str | None = None) -> str | None:
        return self.attrs.get(name.casefold(), default)

    @property
    def classes(self) -> frozenset[str]:
        return frozenset((self.get("class") or "").casefold().split())

    def has_class(self, name: str) -> bool:
        return name.casefold() in self.classes

    def text_content(self, *, separator: str = " ") -> str:
        pieces: list[str] = []

        def walk(node: Element) -> None:
            for child in node.children:
                if isinstance(child, str):
                    text = " ".join(child.split())
                    if text:
                        pieces.append(text)
                else:
                    walk(child)

        walk(self)
        return separator.join(pieces).strip()

    def iter_elements(self, *, include_self: bool = False) -> Iterator["Element"]:
        if include_self:
            yield self
        for child in self.children:
            if isinstance(child, Element):
                yield child
                yield from child.iter_elements()

    def find_all(
        self,
        tag: str | None = None,
        *,
        predicate: Callable[["Element"], bool] | None = None,
        **attrs: str,
    ) -> list["Element"]:
        wanted_tag = tag.casefold() if tag else None
        result: list[Element] = []
        for element in self.iter_elements(include_self=True):
            if wanted_tag is not None and element.tag != wanted_tag:
                continue
            if any((element.get(key) or "") != value for key, value in attrs.items()):
                continue
            if predicate is not None and not predicate(element):
                continue
            result.append(element)
        return result

    def find_first(
        self,
        tag: str | None = None,
        *,
        predicate: Callable[["Element"], bool] | None = None,
        **attrs: str,
    ) -> "Element | None":
        matches = self.find_all(tag, predicate=predicate, **attrs)
        return matches[0] if matches else None

    def descendants_with_class_fragment(self, fragment: str) -> list["Element"]:
        needle = fragment.casefold()
        return self.find_all(
            predicate=lambda element: needle in (element.get("class") or "").casefold()
            or needle in (element.get("id") or "").casefold()
        )

    def ancestors(self) -> Iterator["Element"]:
        current = self.parent
        while current is not None:
            yield current
            current = current.parent

    def element_children(self) -> list["Element"]:
        return [child for child in self.children if isinstance(child, Element)]


class _DomParser(HTMLParser):
    VOID_TAGS = frozenset(
        {
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
            "source",
            "track",
            "wbr",
        }
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Element("document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Element(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)
        if tag.casefold() not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Element(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(node)

    def handle_endtag(self, tag: str) -> None:
        wanted = tag.casefold()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == wanted:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        if data:
            self.stack[-1].children.append(data)


def parse_html(html: str) -> Element:
    parser = _DomParser()
    parser.feed(html)
    parser.close()
    return parser.root


def class_or_id_contains(element: Element, *fragments: str) -> bool:
    haystack = f"{element.get('class') or ''} {element.get('id') or ''}".casefold()
    return any(fragment.casefold() in haystack for fragment in fragments)


def child_text(element: Element, *class_fragments: str) -> str | None:
    child = element.find_first(
        predicate=lambda item: class_or_id_contains(item, *class_fragments)
    )
    text = child.text_content() if child else ""
    return text or None
