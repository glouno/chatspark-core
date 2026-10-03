import hashlib
import re
import unicodedata


def normalize_content_for_hash(text: str) -> str:
    """Conservatively normalize extracted text before assigning content identity."""
    normalized = unicodedata.normalize("NFC", str(text or ""))
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n"))
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def compute_content_hash(text: str) -> str:
    """Computes SHA256 hash of the normalized text content."""
    return hashlib.sha256(normalize_content_for_hash(text).encode("utf-8")).hexdigest()


def compute_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
