from __future__ import annotations

import os
import re
from collections.abc import Iterable
from functools import lru_cache
from urllib.parse import urlsplit, urlunsplit

from chatspark.profiles.models import CanonicalURLRewrite


@lru_cache(maxsize=8)
def _profile_rewrites(profile_name: str) -> tuple[CanonicalURLRewrite, ...]:
    from chatspark.profiles.loader import load_profile

    return tuple(load_profile(profile_name).crawl.canonical_url_rewrites)


def active_url_rewrites() -> tuple[CanonicalURLRewrite, ...]:
    """Return URL normalization rules from the selected deployment profile."""
    return _profile_rewrites(os.getenv("CHATSPARK_PROFILE", "hybrid"))


def canonicalize_source_url(
    url: str,
    rewrites: Iterable[CanonicalURLRewrite] | None = None,
) -> str:
    """Apply ordered, profile-owned host and path-prefix rewrites."""
    value = str(url or "").strip()
    if not value:
        return ""
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower()
    path = parsed.path or "/"
    changed = False
    for rule in active_url_rewrites() if rewrites is None else rewrites:
        if rule.from_host and host != rule.from_host.lower():
            continue
        if rule.from_path and path != rule.from_path:
            continue
        if rule.from_path_prefix and not path.startswith(rule.from_path_prefix):
            continue
        if rule.to_host:
            host = rule.to_host.lower()
        if rule.from_path:
            path = rule.to_path or path
        if rule.from_path_prefix:
            path = rule.to_path_prefix + path[len(rule.from_path_prefix) :]
        changed = True
    if not changed:
        return value
    port = f":{parsed.port}" if parsed.port else ""
    userinfo = ""
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo += f":{parsed.password}"
        userinfo += "@"
    return urlunsplit(
        (parsed.scheme, f"{userinfo}{host}{port}", path or "/", parsed.query, parsed.fragment)
    )


_URL_PATTERN = re.compile(r"https?://[^\s<>\]\[()\"']+")


def canonicalize_source_urls_in_text(
    text: str,
    rewrites: Iterable[CanonicalURLRewrite] | None = None,
) -> str:
    rules = tuple(active_url_rewrites() if rewrites is None else rewrites)
    return _URL_PATTERN.sub(
        lambda match: canonicalize_source_url(match.group(0), rules), str(text or "")
    )
