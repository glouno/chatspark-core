import hashlib
import json

import pytest

from chatspark.embeddings import prepare


class RecipeResource:
    def __init__(self, payload):
        self.payload = payload

    def joinpath(self, _name):
        return self

    def read_text(self):
        return json.dumps(self.payload)


def configure(monkeypatch, *, content=b"reviewed model data", checksum=None, path="config.json"):
    recipe = {
        "repository": "reviewed/model",
        "revision": "frozen",
        "files": [{"path": path, "sha256": checksum or hashlib.sha256(content).hexdigest()}],
    }
    monkeypatch.setattr(prepare, "files", lambda _package: RecipeResource(recipe))

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def raise_for_status(self):
            pass

        def iter_content(self, _size):
            yield content

    monkeypatch.setattr(prepare.requests, "get", lambda *_args, **_kwargs: Response())
    return recipe


def test_model_preparation_verifies_content_and_writes_ready_marker(monkeypatch, tmp_path):
    recipe = configure(monkeypatch)
    destination = tmp_path / "model"
    assert prepare.prepare_model(destination) == recipe
    assert (destination / "config.json").read_bytes() == b"reviewed model data"
    assert json.loads((destination / "chatspark-model-recipe.json").read_text()) == recipe


def test_bad_hash_leaves_no_model_or_staging_files(monkeypatch, tmp_path):
    configure(monkeypatch, checksum="0" * 64)
    with pytest.raises(ValueError, match="reviewed hash"):
        prepare.prepare_model(tmp_path / "model")
    assert list(tmp_path.iterdir()) == []


def test_existing_directory_is_preserved_without_network_call(monkeypatch, tmp_path):
    configure(monkeypatch)
    monkeypatch.setattr(prepare.requests, "get", lambda *_args, **_kwargs: pytest.fail("network"))
    (tmp_path / "owned.txt").write_text("preserve")
    with pytest.raises(ValueError, match="fresh destination"):
        prepare.prepare_model(tmp_path)
    assert (tmp_path / "owned.txt").read_text() == "preserve"


def test_recipe_path_cannot_escape_destination(monkeypatch, tmp_path):
    configure(monkeypatch, path="../escaped")
    with pytest.raises(ValueError, match="unsafe path"):
        prepare.prepare_model(tmp_path / "model")
    assert list(tmp_path.iterdir()) == []


def test_total_deadline_bounds_streaming_download(monkeypatch, tmp_path):
    configure(monkeypatch)
    clock = iter([0, 1, 1801])
    monkeypatch.setattr(prepare.time, "monotonic", lambda: next(clock))
    with pytest.raises(TimeoutError, match="total deadline"):
        prepare.prepare_model(tmp_path / "model")
    assert list(tmp_path.iterdir()) == []


def test_concurrent_destination_is_not_overwritten(monkeypatch, tmp_path):
    configure(monkeypatch)
    destination = tmp_path / "model"
    original_get = prepare.requests.get

    def race(*args, **kwargs):
        destination.mkdir()
        (destination / "owned.txt").write_text("other caller")
        return original_get(*args, **kwargs)

    monkeypatch.setattr(prepare.requests, "get", race)
    with pytest.raises(FileExistsError):
        prepare.prepare_model(destination)
    assert list(destination.iterdir()) == [destination / "owned.txt"]
