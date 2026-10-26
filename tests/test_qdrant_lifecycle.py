"""Owned transport cleanup without deleting or crossing collection boundaries."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

pytest.importorskip("qdrant_client")
from chatspark.storage import qdrant as module


class Client:
    def __init__(self):
        self.closed = 0
        self.collections = []
        self.fail_close = False
        self.fail_discovery = False
        self.entered = Event()
        self.release = Event()

    def get_collection(self, name):
        self.collections.append(name)
        if self.fail_discovery:
            raise LookupError("discovery failed")
        return SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(vectors={})))

    def count(self, *, collection_name, exact):
        assert exact
        self.collections.append(collection_name)
        self.entered.set()
        assert self.release.wait(5)
        return SimpleNamespace(count=7)

    def close(self):
        self.closed += 1
        if self.fail_close:
            raise OSError("disposal failed")


@pytest.fixture
def client(monkeypatch):
    client = Client()
    monkeypatch.setattr(module, "QdrantClient", lambda **kwargs: client)
    return client


def test_successful_close_is_idempotent_and_never_mutates_collection(client):
    store = module.QdrantVectorStore(collection_name="selected", url="http://localhost:1")
    store.close()
    store.close()
    assert client.closed == 1
    assert client.collections == ["selected", "selected"]
    assert store.collection_name == "selected"
    for operation in (
        lambda: store.upsert([]),
        lambda: store.query([], 0),
        lambda: store.get([]),
        lambda: store.delete([]),
        store.count,
        store.list_ids,
        store.reset,
    ):
        with pytest.raises(RuntimeError, match="is closed"):
            operation()
    assert client.collections == ["selected", "selected"]


def test_active_operation_rejects_close_and_remains_usable(client):
    store = module.QdrantVectorStore(collection_name="selected")
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(store.count)
        try:
            assert client.entered.wait(5)
            with pytest.raises(RuntimeError, match="Drain"):
                store.close()
            assert client.closed == 0
        finally:
            client.release.set()
        assert pending.result(timeout=5) == 7
    assert store.count() == 7
    store.close()
    assert client.closed == 1


def test_failed_operation_releases_its_ownership(client):
    store = module.QdrantVectorStore(collection_name="selected")
    client.fail_discovery = True
    with pytest.raises(LookupError):
        store.query([1.0], 1)
    store.close()
    assert client.closed == 1


def test_failed_disposal_can_retry_but_cannot_reopen(client):
    store = module.QdrantVectorStore(collection_name="selected")
    client.fail_close = True
    with pytest.raises(OSError):
        store.close()
    with pytest.raises(RuntimeError, match="is closed"):
        store.get([])
    client.fail_close = False
    store.close()
    store.close()
    assert client.closed == 2


@pytest.mark.parametrize("fail_close", [False, True])
def test_failed_constructor_disposes_client_and_preserves_original_error(client, fail_close):
    client.fail_discovery = True
    client.fail_close = fail_close
    with pytest.raises(LookupError, match="discovery failed"):
        module.QdrantVectorStore(collection_name="selected")
    assert client.closed == 1


def test_closing_one_owner_does_not_close_another(monkeypatch):
    clients = [Client(), Client()]
    remaining = iter(clients)
    monkeypatch.setattr(module, "QdrantClient", lambda **kwargs: next(remaining))
    first = module.QdrantVectorStore(collection_name="first")
    second = module.QdrantVectorStore(collection_name="second")
    first.close()
    clients[1].release.set()
    assert second.count() == 7
    assert [client.closed for client in clients] == [1, 0]
    second.close()
