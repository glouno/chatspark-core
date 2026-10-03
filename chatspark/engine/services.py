"""Trusted application composition for existing build integrations.

Customer profiles cannot select these callables. External algorithms should use
provider contracts; this boundary preserves existing applications during migration.
"""

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class BuildServices:
    profile_reader: Callable | None = None
    validator: Callable | None = None
    profile_snapshot: Callable | None = None
    file_iterator: Callable | None = None
    url_resolver: Callable | None = None
    parser: Callable | None = None
    byte_parser: Callable | None = None
    chunker: Callable | None = None
    indexer: Callable | None = None
    artifacts: Callable | None = None
    version_reader: Callable | None = None
    manifest_writer: Callable | None = None
