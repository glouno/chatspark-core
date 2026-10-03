import re
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from chatspark.extraction.files import supported_suffixes

SUPPORTED_MIRROR_SUFFIXES = set(supported_suffixes())
_MIRRORED_FROM = re.compile(r'Mirrored\s+from\s+(?P<url>https?://[^\s<>"\']+)', re.I)


def iter_local_mirror_files(root):
    for item in sorted(root.rglob("*")):
        if item.is_file() and item.suffix.lower() in SUPPORTED_MIRROR_SUFFIXES:
            item.resolve().relative_to(root.resolve())
            yield item


def resolve_local_mirror_url(
    root: Path, path: Path, base_url: str, *, content_bytes: bytes | None = None
) -> tuple[str, str]:
    if path.suffix.lower() in {".html", ".htm", ".php"}:
        match = _MIRRORED_FROM.search(
            (content_bytes or path.read_bytes()).decode("utf-8", errors="ignore")
        )
        if match:
            raw = match.group("url").strip()
            raw = raw if raw.startswith(("http://", "https://")) else f"https://{raw}"
            parsed = urlparse(raw)
            if parsed.scheme and parsed.netloc:
                escaped = "/".join(quote(unquote(part), safe="") for part in parsed.path.split("/"))
                return f"{parsed.scheme}://{parsed.netloc}{escaped}" + (
                    f"?{parsed.query}" if parsed.query else ""
                ), "httrack_mirrored_from"
    relative = path.resolve().relative_to(root.resolve())
    parts = relative.parts
    scheme = urlparse(base_url).scheme or "https"
    if (
        len(parts) > 1
        and "." in parts[0]
        and Path(parts[0]).suffix.lower() not in SUPPORTED_MIRROR_SUFFIXES
    ):
        tail = [quote(part, safe="") for part in parts[1:]]
        if path.name.lower() in {"index.html", "index.htm"}:
            tail = tail[:-1]
        return f"{scheme}://{parts[0]}/{'/'.join(tail)}" + (
            "/" if path.name.lower() in {"index.html", "index.htm"} else ""
        ), "explicit_host_path"
    tail = [quote(part, safe="") for part in parts]
    if path.name.lower() in {"index.html", "index.htm"}:
        tail = tail[:-1]
    return f"{base_url.rstrip('/')}/{'/'.join(tail)}" + (
        "/" if path.name.lower() in {"index.html", "index.htm"} else ""
    ), "base_path"
