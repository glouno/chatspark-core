"""Content-free failure summaries for machine-readable runtime results."""

# Only fixed engine validation messages may be returned verbatim. Third-party
# exceptions can include source text, credentials, URLs and local file paths.
_SAFE_MESSAGES = (
    "output_root must be absent or empty",
    "source_root is not a directory",
    "source_root contains no supported documents",
    "canonical database digest does not match the build result",
    "build result has no canonical database digest",
    "evaluation requires a successful build with a chunk set",
    "evaluation dataset is empty",
    "Provider attempted to widen authorization",
    "Canonical snapshot changed during retrieval",
    "Provider returned unknown, stale or unauthorized evidence",
)


def safe_error(error, operation):
    from chatspark.extraction.pdf import PdfExtractionError

    if isinstance(error, PdfExtractionError) and error.status in {
        "encrypted",
        "unsupported",
        "invalid",
        "timeout",
        "failed",
        "output_limit",
        "oversized",
    }:
        return f"{operation} failed: PDF extraction {error.status}"
    message = str(error)
    for safe in _SAFE_MESSAGES:
        if message == safe or message.startswith(safe + ":"):
            return safe
    return f"{operation} failed; inspect inputs and operator configuration"
