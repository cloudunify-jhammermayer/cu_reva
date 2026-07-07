"""Well-formedness guard for the HTML the ticket formatter emits.

The Odoo callback writes the analysis into an HTML field; a malformed render
(an unclosed tag, a stray closing tag, or a stray ``<``) breaks the field. This
repairs the tag set the formatter uses with the stdlib parser only — it never
fails and never depends on a third-party library.

``ensure_renderable`` returns the original string byte-for-byte when nothing
needs fixing (so a well-formed render is passed through untouched), and the
repaired string otherwise, alongside a ``was_repaired`` flag the runner uses to
record an ops event.
"""

from __future__ import annotations

from html.parser import HTMLParser

# Tags the formatter emits. Only these participate in balance/nesting repair;
# anything else is left to the passthrough path.
_KNOWN_TAGS = {"p", "h2", "h3", "ul", "li", "strong", "em", "small", "span", "br"}
_VOID_TAGS = {"br"}


class _Rebuilder(HTMLParser):
    """Re-emits the input while tracking open tags, escaping stray ``<``,
    dropping unmatched closers, and closing unclosed tags in stack order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.out: list[str] = []
        self.stack: list[str] = []
        self.repaired = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag not in _VOID_TAGS and tag in _KNOWN_TAGS:
            self.stack.append(tag)
        self.out.append(self._render_start(tag, attrs, void=tag in _VOID_TAGS))

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        # An explicit self-closing tag (<br/>) is emitted as-is, never stacked.
        self.out.append(self._render_start(tag, attrs, void=True))

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            # A closing void tag (</br>) is meaningless — drop it.
            self.repaired = True
            return
        if tag in _KNOWN_TAGS:
            if tag in self.stack:
                # Close down to the matching open, synthesizing closers for any
                # inner tags left open above it (stack order).
                while self.stack:
                    top = self.stack.pop()
                    self.out.append(f"</{top}>")
                    if top == tag:
                        break
                    self.repaired = True
                return
            # Unmatched closer — drop it.
            self.repaired = True
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if "<" in data:
            # A stray '<' the parser handed back as text (e.g. "a < b") — escape
            # it so Odoo doesn't read it as the start of a tag.
            data = data.replace("<", "&lt;")
            self.repaired = True
        self.out.append(data)

    def handle_entityref(self, name: str) -> None:
        self.out.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.out.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        self.out.append(f"<!--{data}-->")

    @staticmethod
    def _render_start(tag: str, attrs: list, void: bool) -> str:
        bits = [tag]
        for name, value in attrs:
            bits.append(name if value is None else f'{name}="{value}"')
        inner = " ".join(bits)
        return f"<{inner}/>" if void else f"<{inner}>"


def ensure_renderable(html: str) -> tuple[str, bool]:
    """Return ``(html_or_repaired, was_repaired)``.

    When ``was_repaired`` is False the original string is returned unchanged.
    """
    parser = _Rebuilder()
    parser.feed(html)
    parser.close()
    # Anything still open at EOF is an unclosed tag — close it in stack order.
    if parser.stack:
        parser.repaired = True
        while parser.stack:
            parser.out.append(f"</{parser.stack.pop()}>")
    if not parser.repaired:
        return html, False
    return "".join(parser.out), True
