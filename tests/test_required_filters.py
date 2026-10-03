import pytest

from chatspark.runtime.config import settings
from chatspark.runtime.factory import get_retriever
from chatspark.storage.evidence import EvidenceError


def test_operator_filters_cannot_be_replaced(candidate, monkeypatch):
    database, build = candidate
    monkeypatch.setattr(settings, "CHATSPARK_AUTHORIZED_FILTERS", '{"access_class":"internal"}')
    retriever = get_retriever(database=database, chunk_set_id=build.chunk_set_id)
    assert not retriever.retrieve("registration", 3)
    with pytest.raises(EvidenceError, match="widen"):
        retriever.retrieve("registration", 3, {"access_class": "public"})
