import pytest

from chatspark.engine.build import execute_build
from chatspark.engine.contracts import EngineBuildRequestV1


@pytest.fixture(autouse=True)
def explicit_test_configuration(monkeypatch):
    from chatspark.runtime.config import settings

    monkeypatch.setattr(settings, "CHATSPARK_PROFILE", "example")
    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "deterministic")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "chatspark-deterministic-hash-v1")


@pytest.fixture
def candidate(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "workshop.md").write_text("# Workshop\n\nThe registration code is ORCHID-42.\n")
    (source / "loan.md").write_text(
        "# Loan kit\n\nReturn the violet loan kit to the training desk at 16:30.\n"
    )
    result = execute_build(
        EngineBuildRequestV1(
            build_id="synthetic",
            source_root=source,
            output_root=tmp_path / "candidate",
            profile="example",
        )
    )
    assert result.status == "succeeded", result.error
    return tmp_path / "candidate/corpus.db", result
