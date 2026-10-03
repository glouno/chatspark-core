import re
from collections import Counter

import trafilatura
from bs4 import BeautifulSoup

from chatspark.documents import DocContent, Section

SECTION_CONTENT_TAGS = ("p", "li", "table", "pre", "blockquote")
OBFUSCATED_EMAIL_RE = re.compile(
    r"\b([A-Za-z0-9._%+-]+)\s*(?:\(|\[)?\s*(?:at|chez)\s*(?:\)|\])?\s*"
    r"([A-Za-z0-9-]+(?:\s*(?:\(|\[)?\s*(?:dot|point)\s*(?:\)|\])?\s*[A-Za-z0-9-]+)+)\b",
    re.IGNORECASE,
)
OBFUSCATED_DOT_RE = re.compile(r"\s*(?:\(|\[)?\s*(?:dot|point)\s*(?:\)|\])?\s*", re.IGNORECASE)


def extract_html(html_content: str, url: str, settings=None) -> DocContent:
    """
    Extracts main content from HTML and converts to Markdown.
    Uses Trafilatura for robust extraction.
    """
    from chatspark.profiles.models import HtmlSettings

    settings = settings or HtmlSettings()
    cleaned_html = _preclean_html(html_content, settings)

    markdown_content = (
        trafilatura.extract(
            cleaned_html,
            output_format="markdown",
            include_tables=settings.include_tables,
            include_comments=False,
            favor_recall=True,
            no_fallback=False,
            url=url,
        )
        or ""
    )
    plain_text = (
        trafilatura.extract(
            cleaned_html,
            output_format="txt",
            include_tables=settings.include_tables,
            include_comments=False,
            favor_recall=True,
            no_fallback=False,
            url=url,
        )
        or ""
    )

    markdown_content = _normalize_text(_deobfuscate_text(markdown_content))
    plain_text = _normalize_text(_deobfuscate_text(plain_text))
    plain_text = _dedupe_lines(plain_text)
    if not plain_text.strip():
        plain_text = _fallback_body_text(cleaned_html)
    if not markdown_content.strip() and plain_text.strip():
        markdown_content = plain_text

    sections = _sections_from_html(cleaned_html)
    if not sections and markdown_content.strip():
        sections = _sections_from_markdown(markdown_content)
    if not sections and plain_text.strip():
        sections = [
            Section(
                heading_path=[],
                content=plain_text.strip(),
                start_char_idx=0,
                end_char_idx=len(plain_text.strip()),
            )
        ]

    for section in sections:
        section.content = _dedupe_lines(_normalize_text(_deobfuscate_text(section.content)))

    return DocContent(
        markdown=markdown_content,
        text=plain_text,
        sections=sections,
    )


class HtmlExtractor:
    def __init__(self, options):
        self.options = options

    def extract(self, html, url):
        return extract_html(html, url, self.options)


def _preclean_html(html_content: str, settings) -> str:
    soup = BeautifulSoup(html_content, "html.parser")
    for selector in settings.remove_selectors:
        for element in list(soup.select(selector)):
            if element.parent is not None:
                element.decompose()
    for rule in settings.attribute_patterns:
        pattern = re.compile(rule.pattern, re.IGNORECASE)
        for element in list(soup.find_all(rule.tags or True, attrs={rule.attribute: pattern})):
            if element.parent is not None:
                element.decompose()
    if not settings.include_tables:
        for table in soup.find_all("table"):
            table.decompose()
    return str(soup)


def _fallback_body_text(html_content: str) -> str:
    soup = BeautifulSoup(html_content, "html.parser")
    root = soup.find("main") or soup.find("article") or soup.body or soup
    parts: list[str] = []
    for el in root.find_all(["h1", "h2", "h3", "p", "li", "table"], recursive=True):
        if el.find_parent(["p", "li", "table"]) and el.name not in {"tr"}:
            continue
        if el.name == "table":
            rows: list[str] = []
            for tr in el.find_all("tr"):
                cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["th", "td"])]
                cells = [cell for cell in cells if cell]
                if cells:
                    rows.append(" | ".join(cells))
            text = "\n\n".join(rows)
        else:
            text = el.get_text(" ", strip=True)
        if text:
            parts.append(text)
    return _dedupe_lines(_normalize_text(_deobfuscate_text("\n".join(parts))))


def _is_high_link_density(element) -> bool:
    text = element.get_text(" ", strip=True)
    if not text:
        return False
    links = element.find_all("a")
    if len(links) < 3:
        return False
    link_text = " ".join(a.get_text(" ", strip=True) for a in links)
    text_len = len(text)
    link_len = len(link_text)
    if text_len == 0:
        return False
    density = link_len / text_len
    return density > 0.7 and text_len < 600


def _sections_from_markdown(markdown: str) -> list[Section]:
    sections: list[Section] = []
    heading_path: list[str] = []
    buffer: list[str] = []
    cursor = 0

    heading_re = re.compile(r"^(#{1,6})\s+(.*)$")

    def flush():
        nonlocal cursor, buffer
        content = "\n".join(buffer).strip()
        if content:
            start = cursor
            end = cursor + len(content)
            sections.append(
                Section(
                    heading_path=heading_path.copy(),
                    content=content,
                    start_char_idx=start,
                    end_char_idx=end,
                )
            )
            cursor = end + 2
        buffer = []

    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        match = heading_re.match(line)
        if match:
            flush()
            level = len(match.group(1))
            title = match.group(2).strip()
            heading_path[:] = heading_path[: level - 1]
            if title:
                heading_path.append(title)
            continue
        buffer.append(raw_line)

    flush()
    return sections


def _sections_from_html(html_content: str) -> list[Section]:
    soup = BeautifulSoup(html_content, "html.parser")
    root = soup.body or soup
    sections: list[Section] = []
    heading_path: list[str] = []
    buffer: list[str] = []
    cursor = 0

    def flush():
        nonlocal cursor, buffer
        content = "\n".join(buffer).strip()
        if content:
            start = cursor
            end = cursor + len(content)
            sections.append(
                Section(
                    heading_path=heading_path.copy(),
                    content=content,
                    start_char_idx=start,
                    end_char_idx=end,
                )
            )
            cursor = end + 2
        buffer = []

    for el in root.find_all(
        ["h1", "h2", "h3", "p", "li", "table", "pre", "blockquote"], recursive=True
    ):
        if el.name in {"h1", "h2", "h3"}:
            flush()
            level = int(el.name[1])
            title = el.get_text(" ", strip=True)
            heading_path[:] = heading_path[: level - 1]
            if title:
                heading_path.append(title)
            continue
        if el.name != "li" and el.find_parent(SECTION_CONTENT_TAGS):
            continue
        if el.name == "li":
            text = _list_item_text(el)
            if text:
                buffer.append(f"- {text}")
            continue
        if el.name == "table":
            from chatspark.extraction.files import _markdown_table

            text = _markdown_table(
                [
                    [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
                    for row in el.find_all("tr")
                ]
            )
        else:
            text = el.get_text(" ", strip=True)
        if text:
            buffer.append(text)

    flush()
    return sections


def _deobfuscate_text(text: str) -> str:
    if not text:
        return text
    text = _deobfuscate_email_like_tokens(text)
    text = _normalize_phone_numbers(text)
    return text


def _deobfuscate_email_like_tokens(text: str) -> str:
    previous = None

    def repl(match: re.Match) -> str:
        domain = OBFUSCATED_DOT_RE.sub(".", match.group(2))
        return f"{match.group(1)}@{domain}"

    while previous != text:
        previous = text
        text = OBFUSCATED_EMAIL_RE.sub(repl, text)
    return text


def _list_item_text(element) -> str:
    clone = BeautifulSoup(str(element), "html.parser")
    for nested_list in clone.find_all(["ul", "ol"]):
        nested_list.decompose()
    return clone.get_text(" ", strip=True)


def _normalize_phone_numbers(text: str) -> str:
    def repl(match: re.Match) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) == 10:
            return " ".join(digits[i : i + 2] for i in range(0, 10, 2))
        return match.group(0)

    return re.sub(r"(?:\+33|0)\s?(?:[0-9][\s().-]?){8,}", repl, text)


def _normalize_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\x00", "")
    text = "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _dedupe_lines(text: str) -> str:
    if not text:
        return ""
    lines = [line.strip() for line in text.splitlines()]
    normalized = [re.sub(r"\s+", " ", line).lower() for line in lines if line]
    counts = Counter(normalized)

    filtered: list[str] = []
    for line in lines:
        if not line:
            continue
        norm = re.sub(r"\s+", " ", line).lower()
        if counts.get(norm, 0) >= 3 and len(norm) <= 80:
            continue
        if norm in {"show menu", "hide menu"}:
            continue
        filtered.append(line)

    return "\n".join(filtered)
