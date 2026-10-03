"""Private temporary paths with cleanup that never resets external permissions."""

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def temporary_directory(*, prefix, dir=None):
    directory = Path(tempfile.mkdtemp(prefix=prefix, dir=dir))
    try:
        yield directory
    finally:
        shutil.rmtree(directory)
