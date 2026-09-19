"""Provider-neutral value predicates and normalizers shared by job-board adapters."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

# Element content that is markup or styling, not description text.
_DISCARDED_TAGS = frozenset({"script", "style", "noscript", "template"})

# Block elements separate lines; list items also get a bullet so a flattened list stays readable.
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)

_WHITESPACE = re.compile(r"\s+")


class _PlainTextExtractor(HTMLParser):
    """Flatten HTML to text, treating block elements as line breaks and list items as bullets."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._chunks: list[str] = []
        self._discarded_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DISCARDED_TAGS:
            self._discarded_depth += 1
        elif tag == "li":
            self._chunks.append("\n- ")
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _DISCARDED_TAGS:
            self._discarded_depth = max(0, self._discarded_depth - 1)
        elif tag in _BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        if self._discarded_depth == 0:
            # Source line wrapping is not a paragraph break; block tags are the only line breaks.
            self._chunks.append(_WHITESPACE.sub(" ", data))

    def text(self) -> str | None:
        lines = [" ".join(line.split()) for line in "".join(self._chunks).splitlines()]
        return "\n".join(line for line in lines if line) or None


def https_url(value: object, *, field: str) -> str:
    """Return a nonblank absolute HTTPS URL or describe why the provider value is unusable.

    The generic ``ValueError`` deliberately leaves each provider adapter responsible for turning a
    malformed record into its own error type and collection warning.
    """
    url = optional_text(value)
    if url is None:
        raise ValueError(f"missing {field}")
    if not is_absolute_https(url):
        raise ValueError(f"{field} is not an absolute https URL")
    return url


def is_absolute_https(value: str) -> bool:
    """Whether ``value`` parses as an absolute HTTPS URL without raising for malformed input."""
    try:
        parts = urlsplit(value)
    except ValueError:
        # ``urlsplit`` rejects malformed URLs itself, for example an unmatched IPv6 bracket.
        return False
    return parts.scheme == "https" and bool(parts.netloc)


def https_origin(url: str) -> str:
    """The scheme and host of ``url``, without path, query, or fragment.

    A fallback adapter resolves the relative links a page publishes against the origin of the
    audited endpoint, so this is the only base such an adapter may use.
    """
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def resolve_https_url(value: object, *, origin: str, field: str) -> str:
    """Resolve one published URL reference against the audited source origin.

    An absolute HTTPS reference is returned as published. A relative reference is resolved against
    ``origin`` and nothing else, so a payload cannot move a stored link, or a request, onto a host
    it names itself: a protocol-relative reference (``//mirror.example/jobs``) is refused rather
    than resolved, and anything that is not an absolute HTTPS URL afterwards is refused too. The
    generic ``ValueError`` leaves each adapter responsible for turning a bad reference into its own
    error type, exactly like ``https_url()``.
    """
    reference = optional_text(value)
    if reference is None:
        raise ValueError(f"missing {field}")
    if reference.startswith("//"):
        raise ValueError(f"{field} is a protocol-relative URL, not a relative path")
    if urlsplit(reference).scheme:
        if not is_absolute_https(reference):
            raise ValueError(f"{field} is not an absolute https URL")
        return reference
    resolved = urljoin(origin.rstrip("/") + "/", reference)
    if not is_absolute_https(resolved):
        raise ValueError(f"{field} does not resolve to an absolute https URL")
    return resolved


def optional_text(value: object) -> str | None:
    """Normalize provider scalars to nonblank text, excluding boolean values."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    text = str(value).strip()
    return text or None


def html_to_text(value: object) -> str | None:
    """Flatten provider markup into deterministic plain text, or `None` when it holds none.

    Block elements become line breaks and list items become a \n- bullet, so the same document
    always produces the same text no matter how the provider wrapped its source lines. A document
    that carries no tag at all but escaped ones (`&lt;p&gt;`) is rehydrated first, because some
    boards store the description as an escaped HTML document; entities inside the text are decoded
    by the parser, which is what those boards mean.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    document = value
    if "<" not in document and "&lt;" in document:
        document = html.unescape(document)
    extractor = _PlainTextExtractor()
    extractor.feed(document)
    extractor.close()
    return extractor.text()
