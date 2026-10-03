"""Explicit, bounded HTML/PDF acquisition into a new local source snapshot."""

import hashlib
import ipaddress
import json
import re
import socket
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import requests
import urllib3
from bs4 import BeautifulSoup

from chatspark.extraction.files import parse_bytes
from chatspark.runtime.validation import validate_runtime


def acquire(profile, output, policy, *, allow_loopback=False):
    validate_runtime(profile)
    policy = json.loads(Path(policy).read_text())
    if policy.get("acquisition_enabled") is not True or not policy.get("authorization_reference"):
        raise ValueError("Acquisition requires an enabled policy and authorization reference")
    output = Path(output).expanduser().resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("Acquisition destination must be empty")
    config = profile.crawl
    if not config.allowed_domains or not config.start_urls:
        raise ValueError("Acquisition requires explicit start URLs and allowed domains")
    domains = {d.lower() for d in config.allowed_domains}

    def validate(url):
        parsed = urlsplit(url)
        if parsed.scheme not in ("https", "http") or parsed.username or parsed.password:
            raise ValueError("Unsupported acquisition URL")
        if (parsed.hostname or "").lower() not in domains or (
            parsed.hostname or ""
        ).lower() in config.deny_domains:
            raise ValueError("Acquisition URL is outside allowed domains")
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(
                parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)
            )
        }
        for value in addresses:
            address = ipaddress.ip_address(value)
            if not address.is_global and not (allow_loopback and address.is_loopback):
                raise ValueError("Non-public acquisition address requires explicit local test mode")
        return parsed, sorted(addresses)

    throttle_lock = Lock()
    next_request = {}
    adaptive_delay = {}
    robots_delay = {}

    def fetch(url, maximum):
        deadline = time.monotonic() + 120
        for _ in range(6):
            parsed, addresses = validate(url)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Acquisition request deadline exceeded")
            # Connect to a validated address directly. TLS still verifies the
            # original hostname, preventing a second DNS lookup/rebinding window.
            options = {
                "host": addresses[0],
                "port": parsed.port or (443 if parsed.scheme == "https" else 80),
                "timeout": urllib3.Timeout(connect=min(10, remaining), read=min(30, remaining)),
            }
            pool = (
                urllib3.HTTPSConnectionPool(
                    **options, server_hostname=parsed.hostname, assert_hostname=parsed.hostname
                )
                if parsed.scheme == "https"
                else urllib3.HTTPConnectionPool(**options)
            )
            origin = f"{parsed.scheme}://{parsed.netloc}"
            with throttle_lock:
                now = time.monotonic()
                scheduled = max(now, next_request.get(origin, now))
                delay = max(
                    config.download_delay_seconds,
                    robots_delay.get(origin, 0),
                    adaptive_delay.get(origin, 0),
                )
                next_request[origin] = scheduled + delay
            if scheduled > now:
                if scheduled >= deadline:
                    raise TimeoutError("Acquisition throttle exceeds deadline")
                time.sleep(scheduled - now)
            started = time.monotonic()
            response = None
            try:
                target = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
                response = pool.urlopen(
                    "GET",
                    target,
                    headers={"User-Agent": config.user_agent, "Host": parsed.netloc},
                    redirect=False,
                    retries=False,
                    preload_content=False,
                )
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        raise ValueError("Acquisition redirect lacks a location")
                    url = urljoin(url, location)
                    continue
                if response.status >= 400:
                    error_response = requests.Response()
                    error_response.status_code = response.status
                    raise requests.HTTPError("Acquisition HTTP failure", response=error_response)
                chunks, size = [], 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Acquisition request deadline exceeded")
                    connection = response.connection
                    if connection is not None and connection.sock is not None:
                        connection.sock.settimeout(min(30, remaining))
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > maximum:
                        raise ValueError("Acquisition input exceeds byte budget")
                    chunks.append(chunk)
                return (
                    url,
                    b"".join(chunks),
                    response.headers.get("Content-Type", "").split(";", 1)[0],
                )
            finally:
                with throttle_lock:
                    adaptive_delay[origin] = min(
                        30, (time.monotonic() - started) / config.autothrottle_target_concurrency
                    )
                if response is not None:
                    response.close()
                pool.close()
        raise ValueError("Too many acquisition redirects")

    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    queue, visited, robots, records = deque(config.start_urls), set(), {}, []
    with ThreadPoolExecutor(max_workers=config.concurrent_requests_per_domain) as pool:
        while queue and len(records) < config.max_pages and len(visited) < 10000:
            pending = []
            while (
                queue
                and len(pending)
                < min(config.concurrent_requests_per_domain, config.max_pages - len(records))
                and len(visited) < 10000
            ):
                url = urldefrag(queue.popleft())[0]
                if url in visited:
                    continue
                visited.add(url)
                parsed, _ = validate(url)
                if config.allow_patterns and not any(
                    re.search(p, url) for p in config.allow_patterns
                ):
                    continue
                if any(re.search(p, url) for p in config.deny_patterns):
                    continue
                origin = f"{parsed.scheme}://{parsed.netloc}"
                if origin not in robots:
                    parser = RobotFileParser()
                    try:
                        _, body, _ = fetch(origin + "/robots.txt", 1024 * 1024)
                        parser.parse(body.decode("utf-8", errors="replace").splitlines())
                    except requests.HTTPError as error:
                        if error.response.status_code != 404:
                            raise ValueError("Robots policy unavailable") from None
                        parser.parse([])
                    robots[origin] = parser
                    robots_delay[origin] = parser.crawl_delay(config.user_agent) or 0
                if not robots[origin].can_fetch(config.user_agent, url):
                    continue
                pending.append(pool.submit(fetch, url, profile.extraction.pdf.max_bytes))
            for future in pending:
                final_url, raw, mime = future.result()
                suffix = (
                    ".pdf"
                    if mime == "application/pdf"
                    else ".html"
                    if mime in ("text/html", "application/xhtml+xml")
                    else None
                )
                if suffix is None or mime in config.blocked_content_types:
                    continue
                ignored = (
                    config.html_ignored_extensions
                    if suffix == ".html"
                    else config.files_ignored_extensions
                )
                if any(urlsplit(final_url).path.lower().endswith(ext.lower()) for ext in ignored):
                    continue
                if (
                    suffix == ".pdf"
                    and config.file_link_allow_patterns
                    and not any(
                        re.search(pattern, final_url) for pattern in config.file_link_allow_patterns
                    )
                ):
                    continue
                if suffix == ".pdf" and profile.extraction.content_scope == "html-only":
                    continue
                normalized = parse_bytes(
                    raw,
                    name="source" + suffix,
                    source_url=final_url,
                    mime_type=mime,
                    pdf_config=profile.extraction.pdf,
                    ocr_config=profile.extraction.ocr,
                    html_config=profile.extraction.html,
                )
                if (
                    not normalized.content.text.strip()
                    or len(normalized.content.text.strip())
                    < profile.extraction.min_document_text_chars
                ):
                    continue
                # Domain/path structure preserves PDF identities; HTML includes its canonical origin.
                path = (
                    output
                    / urlsplit(final_url).netloc
                    / (hashlib.sha256(final_url.encode()).hexdigest() + suffix)
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                stored = raw + (
                    f"\n<!-- Mirrored from {final_url} -->".encode() if suffix == ".html" else b""
                )
                path.write_bytes(stored)
                records.append(
                    {
                        "url": final_url,
                        "path": path.relative_to(output).as_posix(),
                        "input_sha256": hashlib.sha256(raw).hexdigest(),
                        "extraction": normalized.diagnostics,
                    }
                )
                if suffix == ".html":
                    for link in BeautifulSoup(raw, "html.parser").select("a[href]"):
                        target = urldefrag(urljoin(final_url, link["href"]))[0]
                        if (urlsplit(target).hostname or "").lower() in domains and len(
                            queue
                        ) < 10000:
                            queue.append(target)
    (output / "acquisition-manifest.json").write_text(
        json.dumps({"schema_version": 1, "records": records}, indent=2) + "\n"
    )
    return {"documents": len(records)}
