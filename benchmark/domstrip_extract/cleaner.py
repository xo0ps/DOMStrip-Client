"""Pure HTML-to-Markdown extraction and conversion.

Runs inside the supervised parser process (or directly in development). No
network access; no shared mutable converter state. Returns structured data only.

Pipeline: parse -> bound DOM -> remove active/hidden content -> select content
(article/full/auto) -> normalize links -> convert -> normalize Markdown.
"""

from __future__ import annotations

import copy
import re
import secrets
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import html2text
import lxml.etree
import lxml.html
from lxml.html import HtmlElement
from readability import Document

from .errors import AppError, ErrorCode

# Bump when selection heuristics change so results can be attributed.
HEURISTICS_VERSION = "2"

DEFAULT_MAX_NODES = 250_000
DEFAULT_MAX_DEPTH = 512

_ACTIVE_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "template",
        "iframe",
        "frame",
        "frameset",
        "object",
        "embed",
        "form",
        "input",
        "select",
        "textarea",
        "button",
        "canvas",
        "applet",
        "svg",
        "math",
        "link",
        "meta",
        "base",
        "dialog",
        "audio",
        "video",
        "source",
        "track",
        "param",
        "portal",
    }
)
_ALLOWED_LINK_SCHEMES = frozenset({"http", "https", "mailto", "ftp", "tel"})
# Hidden content is a common prompt-injection carrier: drop text a reader
# cannot see (display/visibility, zero opacity, zero font size).
_HIDDEN_STYLE = re.compile(
    r"(?:^|;)\s*(?:"
    r"display\s*:\s*none"
    r"|visibility\s*:\s*hidden"
    r"|opacity\s*:\s*(?:0+(?:\.0*)?|\.0+)%?\s*(?:!important)?\s*(?:;|$)"
    r"|font-size\s*:\s*0+(?:\.0+)?(?:px|pt|em|rem|%)?\s*(?:!important)?\s*(?:;|$)"
    r")",
    re.I,
)
_CHALLENGE_HINTS = (
    "enable javascript",
    "please enable js",
    "javascript is required",
    "verify you are human",
    "captcha",
    "cf-challenge",
    "checking your browser",
    "ddos protection",
    "just a moment",
    "are you a robot",
    "access denied",
)
# Challenge/JS shells are short; a long article that merely mentions CAPTCHAs
# must not be rejected.
_CHALLENGE_MAX_TEXT = 1500
_ARTICLE_MIN_CHARS = 120
_ARTICLE_MIN_RATIO = 0.15
# Placeholders carry a per-conversion nonce so page text that happens to
# contain the literal marker can never splice in a different code block.
_CODE_PLACEHOLDER = "DOMSTRIPCODEBLOCK{nonce}N{index}X"
_LANGUAGE_CLASS = re.compile(r"(?:^|\s)(?:language|lang|highlight-source)-([A-Za-z0-9_+#.-]+)")
_WS = re.compile(r"\s+")
_C0_AND_SPACE = "".join(chr(c) for c in range(0x21))


@dataclass
class CleanResult:
    markdown: str
    title: str | None
    extraction_method: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class _Converted:
    markdown: str
    text: str
    code_blocks: int
    tables: int


def clean(
    html: str,
    mode: str,
    base_url: str | None = None,
    *,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_depth: int = DEFAULT_MAX_DEPTH,
) -> CleanResult:
    """Convert ``html`` into Markdown according to ``mode``.

    Raises :class:`AppError` with ``content_not_extractable`` when no usable
    content remains.
    """
    if mode not in ("auto", "article", "full"):
        raise AppError.make(ErrorCode.INVALID_REQUEST, "Unknown extraction mode.")
    if not html or not html.strip():
        raise AppError.make(ErrorCode.CONTENT_NOT_EXTRACTABLE, "No content to extract.")

    tree = _parse(html)
    _enforce_dom_bounds(tree, max_nodes, max_depth)
    _remove_active_content(tree)
    page_title = _title(tree)
    warnings: list[str] = []

    # Readability operates on the already-sanitized document so its scoring is
    # never influenced by scripts, hidden text, or forms.
    cleaned_html = lxml.html.tostring(tree, encoding="unicode")

    if mode == "full":
        converted = _convert(tree, base_url, warnings)
        method, chosen, title = "full", converted, page_title
    else:
        article_tree, article_title = _extract_article(cleaned_html)
        article = _convert(article_tree, base_url, warnings) if article_tree is not None else None
        title = article_title or page_title
        if mode == "article":
            if article is None or not article.text.strip():
                raise AppError.make(
                    ErrorCode.CONTENT_NOT_EXTRACTABLE,
                    "No article content could be extracted.",
                )
            method, chosen = "article", article
        else:  # auto
            full = _convert(tree, base_url, warnings)
            if article is not None and _article_is_usable(article, full):
                method, chosen = "article", article
            else:
                method, chosen, title = "full", full, page_title
                warnings.append("fallback_used")

    markdown = _normalize_blank_lines(chosen.markdown)
    if not markdown.strip():
        raise AppError.make(
            ErrorCode.CONTENT_NOT_EXTRACTABLE,
            "No usable content remained after extraction.",
        )

    if _looks_like_challenge(chosen.text):
        raise AppError.make(
            ErrorCode.CONTENT_NOT_EXTRACTABLE,
            "The page appears to require JavaScript or a bot challenge.",
        )

    return CleanResult(
        markdown=markdown,
        title=title,
        extraction_method=method,
        warnings=list(dict.fromkeys(warnings)),
    )


def _parse(html: str) -> HtmlElement:
    # Always use the HTML parser (never XML) so external entity loading is not a
    # concern and XHTML input is treated as HTML.
    try:
        tree = lxml.html.document_fromstring(html)
    except (lxml.etree.ParserError, ValueError) as exc:
        raise AppError.make(
            ErrorCode.CONTENT_NOT_EXTRACTABLE, "The document could not be parsed."
        ) from exc
    if tree is None:
        raise AppError.make(ErrorCode.CONTENT_NOT_EXTRACTABLE, "The document could not be parsed.")
    return tree


def _enforce_dom_bounds(tree: HtmlElement, max_nodes: int, max_depth: int) -> None:
    nodes = 0
    stack: list[tuple[HtmlElement, int]] = [(tree, 1)]
    while stack:
        el, depth = stack.pop()
        nodes += 1
        if nodes > max_nodes:
            raise AppError.make(
                ErrorCode.CONTENT_NOT_EXTRACTABLE, "The document has too many elements."
            )
        if depth > max_depth:
            raise AppError.make(
                ErrorCode.CONTENT_NOT_EXTRACTABLE, "The document is nested too deeply."
            )
        stack.extend((child, depth + 1) for child in el)


def _remove_element(el: HtmlElement) -> None:
    """Remove ``el`` but keep its tail text attached to the document."""
    parent = el.getparent()
    if parent is None:
        return
    tail = el.tail
    if tail:
        previous = el.getprevious()
        if previous is not None:
            previous.tail = (previous.tail or "") + tail
        else:
            parent.text = (parent.text or "") + tail
    parent.remove(el)


def _is_hidden(el: HtmlElement) -> bool:
    if "hidden" in el.attrib:
        return True
    if el.attrib.get("aria-hidden", "").strip().lower() == "true":
        return True
    style = el.attrib.get("style")
    return bool(style and _HIDDEN_STYLE.search(style))


def _remove_active_content(tree: HtmlElement) -> None:
    to_remove: list[HtmlElement] = []
    for el in tree.iter():
        if not isinstance(el.tag, str):
            # Comments and processing instructions carry no visible content.
            to_remove.append(el)
            continue
        tag = el.tag.lower()
        if tag in _ACTIVE_TAGS or (tag not in ("html", "body") and _is_hidden(el)):
            to_remove.append(el)
            continue
        for attr in list(el.attrib):
            if attr.lower().startswith("on") or attr.lower() in ("style", "srcdoc"):
                del el.attrib[attr]
    for el in to_remove:
        _remove_element(el)


def _visible_text(tree: HtmlElement) -> str:
    return _WS.sub(" ", " ".join(tree.itertext())).strip()


def _extract_article(cleaned_html: str) -> tuple[HtmlElement | None, str | None]:
    try:
        doc = Document(cleaned_html)
        summary = doc.summary(html_partial=True)
        title = doc.short_title() or None
    except Exception:  # readability can raise on pathological input
        return None, None
    if title and title.strip() in ("", "[no-title]"):
        title = None
    if not summary or not summary.strip():
        return None, title
    try:
        fragment = lxml.html.fragment_fromstring(summary, create_parent="div")
    except (lxml.etree.ParserError, ValueError):
        return None, title
    _remove_active_content(fragment)
    return fragment, title


def _article_is_usable(article: _Converted, full: _Converted) -> bool:
    """Deterministic article-vs-full choice (heuristics version ``2``).

    Prefer the article candidate only when it retains enough text and does
    not drop most of the page's code blocks or tables.
    """
    a = len(article.text)
    f = len(full.text)
    if a < _ARTICLE_MIN_CHARS:
        return False
    if f and a < _ARTICLE_MIN_RATIO * f:
        return False
    for kept, total in ((article.code_blocks, full.code_blocks), (article.tables, full.tables)):
        if total and kept * 2 < total:
            return False
    return True


def _title(tree: HtmlElement) -> str | None:
    title_el = tree.find(".//title")
    if title_el is not None:
        text = _WS.sub(" ", title_el.text_content()).strip()
        if text:
            return text[:512]
    h1 = tree.find(".//h1")
    if h1 is not None:
        text = _WS.sub(" ", h1.text_content()).strip()
        if text:
            return text[:512]
    return None


def _normalize_links(tree: HtmlElement, base_url: str | None) -> bool:
    """Resolve/strip links in place; return True if relative links remain."""
    unresolved = False
    for el in tree.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in ("href", "src"):
            if attr not in el.attrib:
                continue
            raw = el.attrib[attr].strip(_C0_AND_SPACE)
            if not raw or not _safe_link(raw):
                del el.attrib[attr]
                continue
            if raw.startswith("#"):
                el.attrib[attr] = raw
                continue
            if not urlsplit(raw).scheme:
                if base_url:
                    raw = urljoin(base_url, raw)
                else:
                    unresolved = True
            el.attrib[attr] = raw
    return unresolved


def _safe_link(raw: str) -> bool:
    try:
        scheme = urlsplit(raw).scheme.lower()
    except ValueError:
        return False
    if not scheme:
        return True  # relative or fragment
    return scheme in _ALLOWED_LINK_SCHEMES


def _extract_code_blocks(root: HtmlElement, nonce: str) -> list[tuple[str, str]]:
    """Replace each ``<pre>`` with a placeholder; return ``(language, code)``.

    html2text renders preformatted text as indented ``[code]`` blocks; fenced
    blocks with the original whitespace are friendlier to agents.
    """
    blocks: list[tuple[str, str]] = []
    for pre in list(root.iter("pre")):
        if pre.getparent() is None:
            continue
        language = ""
        for candidate in [pre, *pre.iter("code")]:
            match = _LANGUAGE_CLASS.search(candidate.attrib.get("class", ""))
            if match:
                language = match.group(1)
                break
        code = pre.text_content().strip("\n")
        placeholder = lxml.html.Element("p")
        placeholder.text = _CODE_PLACEHOLDER.format(nonce=nonce, index=len(blocks))
        placeholder.tail = pre.tail
        pre.getparent().replace(pre, placeholder)
        blocks.append((language, code))
    return blocks


def _fence(language: str, code: str) -> str:
    longest = max((len(m) for m in re.findall(r"`{3,}", code)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{code}\n{fence}"


def _convert(tree: HtmlElement, base_url: str | None, warnings: list[str]) -> _Converted:
    body = tree.find(".//body") if tree.tag != "body" else tree
    if body is None:
        body = tree
    # Work on a copy so the article/full candidates never affect each other.
    body = copy.deepcopy(body)
    text = _visible_text(body)
    tables = sum(1 for _ in body.iter("table"))
    if _normalize_links(body, base_url):
        warnings.append("relative_links_unresolved")
    nonce = secrets.token_hex(8)
    code_blocks = _extract_code_blocks(body, nonce)

    converter = html2text.HTML2Text()
    converter.body_width = 0
    converter.ignore_links = False
    converter.ignore_images = False
    converter.images_to_alt = True
    converter.unicode_snob = True
    converter.wrap_links = False
    converter.protect_links = True
    converter.single_line_break = False
    markdown = converter.handle(lxml.html.tostring(body, encoding="unicode"))

    def _restore(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if index >= len(code_blocks):
            return match.group(0)
        return "\n" + _fence(*code_blocks[index]) + "\n"

    placeholder_re = re.compile(rf"DOMSTRIPCODEBLOCK{nonce}N(\d+)X")
    markdown = placeholder_re.sub(_restore, markdown)
    return _Converted(markdown=markdown, text=text, code_blocks=len(code_blocks), tables=tables)


def _looks_like_challenge(text: str) -> bool:
    if len(text) > _CHALLENGE_MAX_TEXT:
        return False
    lowered = text.lower()
    return any(hint in lowered for hint in _CHALLENGE_HINTS)


def _normalize_blank_lines(markdown: str) -> str:
    """Collapse runs of blank lines and trailing spaces outside code fences."""
    out: list[str] = []
    fence: str | None = None
    for line in markdown.split("\n"):
        stripped = line.lstrip()
        if fence is None:
            match = re.match(r"(`{3,})", stripped)
            if match:
                fence = match.group(1)
                out.append(line.rstrip())
                continue
            if not line.strip():
                if out and out[-1] == "":
                    continue
                line = ""
            out.append(line)
        else:
            if stripped.startswith(fence) and stripped.strip("`") == "":
                fence = None
                out.append(line.rstrip())
            else:
                out.append(line)
    return "\n".join(out).strip() + "\n"
