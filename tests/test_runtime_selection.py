import pytest

from chatspark.plugins import PluginError
from chatspark.runtime.composition import selected_runtime
from chatspark.runtime.config import settings


def test_runtime_requires_explicit_approved_provider(monkeypatch):
    monkeypatch.setattr(settings, "CHATSPARK_RUNTIME_PROVIDER", "")
    assert selected_runtime() is None
    monkeypatch.setattr(settings, "CHATSPARK_RUNTIME_PROVIDER", "unavailable")
    with pytest.raises(PluginError):
        selected_runtime()


def test_default_workspace_is_hybrid_and_offline_is_explicit(tmp_path):
    from typer.testing import CliRunner

    from chatspark.main import app
    from chatspark.profiles import load_profile

    for offline in (False, True):
        workspace = tmp_path / ("offline" if offline else "hybrid")
        args = ["workspace", "init", "--path", str(workspace)]
        if offline:
            args.append("--offline")
        result = CliRunner().invoke(app, args)
        assert result.exit_code == 0, result.output
        profile = load_profile(str(workspace / "profiles/local/profile.yaml"))
        assert profile.retrieval.enable_dense is (not offline)
        assert profile.retrieval.enable_sparse
    assert load_profile().retrieval.enable_dense


def test_hybrid_without_embedding_configuration_fails_before_model_or_network(
    monkeypatch, tmp_path
):
    from chatspark.profiles import load_profile
    from chatspark.runtime.factory import get_embedder, get_retriever
    from chatspark.runtime.validation import validate_runtime

    profile = load_profile("hybrid")
    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "")
    # Schema/provider validation and corpus construction need no embedding service.
    validate_runtime(profile)
    with pytest.raises(ValueError, match="Configure EMBEDDING_BACKEND"):
        validate_runtime(profile, require_dense=True)
    with pytest.raises(ValueError, match="Configure EMBEDDING_BACKEND"):
        get_retriever(database=tmp_path / "does-not-exist.db", profile=profile)
    with pytest.raises(ValueError, match="Configure EMBEDDING_BACKEND"):
        get_embedder()


def test_explicit_embedding_model_and_optional_dependencies_are_required(monkeypatch):
    from chatspark.profiles import load_profile
    from chatspark.runtime.validation import validate_runtime

    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "openai")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "")
    with pytest.raises(ValueError, match="Configure EMBEDDING_MODEL"):
        validate_runtime(load_profile("hybrid"), require_dense=True)
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "operator-selected-model")
    monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
    with pytest.raises(ValueError, match="qdrant"):
        validate_runtime(load_profile("hybrid"), require_dense=True)
