"""Explicit download of a reviewed, hash-pinned data-only model recipe."""

import hashlib
import json
import shutil
import time
from importlib.resources import files
from pathlib import Path

import requests

from chatspark.runtime.temporary import temporary_directory


def prepare_model(destination):
    destination = Path(destination).expanduser().resolve()
    if destination.exists():
        raise ValueError("Model preparation requires a fresh destination")
    recipe = json.loads(
        files("chatspark.embeddings").joinpath("multilingual-e5-small.json").read_text()
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 1800
    with temporary_directory(prefix="chatspark-model-", dir=destination.parent) as directory:
        staging = Path(directory)
        for artifact in recipe["files"]:
            name, expected = artifact["path"], artifact["sha256"]
            if Path(name).is_absolute() or ".." in Path(name).parts:
                raise ValueError("Model recipe contains an unsafe path")
            if time.monotonic() >= deadline:
                raise TimeoutError("Model preparation exceeded its total deadline")
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            url = (
                f"https://huggingface.co/{recipe['repository']}/resolve/{recipe['revision']}/{name}"
            )
            digest = hashlib.sha256()
            size = 0
            with requests.get(url, stream=True, timeout=(10, 60)) as response:
                response.raise_for_status()
                with target.open("xb") as output:
                    for chunk in response.iter_content(1024 * 1024):
                        if time.monotonic() >= deadline:
                            raise TimeoutError("Model preparation exceeded its total deadline")
                        size += len(chunk)
                        if size > 1024 * 1024 * 1024:
                            raise ValueError("Model artifact exceeds recipe size limit")
                        digest.update(chunk)
                        output.write(chunk)
            if digest.hexdigest() != expected:
                raise ValueError("Model artifact does not match the reviewed hash")
        # Exclusive creation prevents a concurrent caller's destination from
        # being overwritten. Publish the verified recipe last as a ready marker.
        destination.mkdir()
        try:
            for target in staging.iterdir():
                shutil.move(str(target), str(destination / target.name))
            (destination / "chatspark-model-recipe.json").write_text(
                json.dumps(recipe, indent=2) + "\n"
            )
        except BaseException:
            shutil.rmtree(destination)
            raise
    return recipe
