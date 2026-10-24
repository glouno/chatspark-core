import asyncio
import hashlib

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from chatspark.application import (
    AnswerRequestV1,
    PromptAssetV1,
    ServingPromptBindingsV1,
    ServingSnapshotV1,
    create_answer_runtime,
)
from chatspark.generation.prompts import resolve_messages
from chatspark.profiles import load_profile
from chatspark.profiles.loader import profile_digest
from chatspark.retrieval.pipeline import execution_context
from chatspark.runtime.registry import get_registry
from chatspark.serve.application import create_http_app


def asset(reference, text):
    return PromptAssetV1(
        reference=reference, text=text, sha256=hashlib.sha256(text.encode()).hexdigest()
    )


def test_bound_prompts_match_legacy_after_original_files_are_removed(tmp_path):
    profile = load_profile("example")
    system, user = tmp_path / "system.txt", tmp_path / "user.txt"
    system.write_text("Synthetic corpus-specific instructions.")
    user.write_text("Question: $question\nEvidence: $context")
    profile.generation.system_prompt = str(system)
    profile.generation.user_prompt = str(user)
    expected = resolve_messages("synthetic query", [], profile)
    bindings = ServingPromptBindingsV1(
        profile_sha256=profile_digest(profile),
        system=asset(str(system), system.read_text()),
        user=asset(str(user), user.read_text()),
    )
    system.unlink()
    user.unlink()
    assert resolve_messages("synthetic query", [], profile, prompt_bindings=bindings) == expected
    assert profile_digest(profile) == bindings.profile_sha256
    with pytest.raises(ValidationError):
        bindings.system.text = "foreign text"


def test_unbound_bundled_prompts_keep_existing_behavior():
    profile = load_profile("example")
    binding = ServingPromptBindingsV1(profile_sha256=profile_digest(profile))
    assert resolve_messages("query", [], profile, prompt_bindings=binding) == resolve_messages(
        "query", [], profile
    )


def test_foreign_profile_and_reference_bindings_fail_before_prompt_io(monkeypatch):
    profile = load_profile("example")
    profile.generation.system_prompt = "/old-host/tenant-a/system.txt"
    binding = ServingPromptBindingsV1(
        profile_sha256=profile_digest(profile),
        system=asset("/old-host/tenant-b/system.txt", "Foreign canary"),
    )
    monkeypatch.setattr(
        "pathlib.Path.read_text", lambda *_a, **_kw: pytest.fail("host path opened")
    )
    with pytest.raises(ValueError, match="reference mismatch"):
        resolve_messages("query", [], profile, prompt_bindings=binding)
    changed = profile.model_copy(deep=True)
    changed.retrieval.final_k += 1
    with pytest.raises(ValueError, match="profile mismatch"):
        resolve_messages("query", [], changed, prompt_bindings=binding)
    with pytest.raises(ValueError, match="reference mismatch"):
        ServingPromptBindingsV1(profile_sha256=profile_digest(profile)).validate_profile(profile)


def test_tampered_asset_is_rejected():
    with pytest.raises(ValidationError, match="digest mismatch"):
        PromptAssetV1(reference="synthetic", text="tampered", sha256="0" * 64)


def test_application_and_http_use_bound_prompts_without_old_files(candidate, tmp_path):
    profile = load_profile("example")
    system = tmp_path / "prompt.txt"
    system.write_text("Synthetic independent corpus instructions.")
    profile.generation.system_prompt = str(system)
    binding = ServingPromptBindingsV1(
        profile_sha256=profile_digest(profile),
        system=asset(str(system), system.read_text()),
    )
    context = execution_context(candidate[0])
    snapshot = ServingSnapshotV1(
        build_id="synthetic",
        database=context.database,
        corpus_id=context.corpus_id,
        chunk_set_id=context.chunk_set_id,
        corpus_sha256=context.snapshot_sha256,
        profile_sha256=profile_digest(profile),
    )

    class Model:
        def __init__(self):
            self.inputs = []

        def stream_chat(self, messages, *, max_tokens):
            self.inputs.append(messages)
            yield "ORCHID-42. [C1]"

    model = Model()
    runtime = create_answer_runtime(
        snapshot,
        profile=profile,
        registry=get_registry(allowed=[]),
        llm=model,
        prompt_bindings=binding,
    )
    system.unlink()

    async def direct():
        await runtime.warmup()
        return [
            event
            async for event in runtime.stream_answer(AnswerRequestV1(question="registration code"))
        ]

    events = asyncio.run(direct())
    assert events[-1].event == "done"
    with TestClient(create_http_app(runtime, authorize=lambda _: None)) as client:
        response = client.post("/chat", json={"question": "registration code"})
        assert response.status_code == 200
        assert response.json()["answer"] == events[-1].data["answer"]
    assert len(model.inputs) == 2 and model.inputs[0] == model.inputs[1]
    assert model.inputs[0][0]["content"].startswith(binding.system.text)
