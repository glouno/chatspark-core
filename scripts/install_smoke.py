"""Run against an installed distribution outside its source checkout."""

import importlib
import json
import pkgutil
import sys
import tempfile
from pathlib import Path

import chatspark
from chatspark.engine.build import execute_build
from chatspark.engine.contracts import EngineBuildRequestV1
from chatspark.engine.source_bindings import EngineSourceBindingsV1
from chatspark.profiles import load_profile
from chatspark.prompts import load_prompt
from chatspark.runtime.factory import get_retriever

package_root = Path(chatspark.__file__).resolve()
assert "site-packages" in str(package_root), package_root
assert load_prompt("rag.classic_chat.system.v6").content
for module in pkgutil.walk_packages(chatspark.__path__, "chatspark."):
    if module.name in {"chatspark.__main__", "chatspark.migrations.env"} or module.name.startswith(
        "chatspark.migrations.versions."
    ):
        continue
    if module.name in {
        "chatspark.storage.qdrant",
        "chatspark.serve.api",
        "chatspark.serve.application",
        "chatspark.serve.contracts",
    }:
        continue
    importlib.import_module(module.name)
with tempfile.TemporaryDirectory(prefix="core-installed-smoke-") as directory:
    root = Path(directory)
    source = root / "source"
    source.mkdir()
    (source / "workshop.md").write_text("# Workshop\n\nThe registration code is ORCHID-42.\n")
    result = execute_build(
        EngineBuildRequestV1(
            build_id="installed",
            source_root=source,
            output_root=root / "candidate",
            profile="example",
        )
    )
    assert result.status == "succeeded", result.error
    assert (
        "ORCHID-42"
        in get_retriever(
            database=root / "candidate/corpus.db",
            profile=load_profile("example"),
            chunk_set_id=result.chunk_set_id,
        )
        .retrieve("registration", 3)[0]
        .content
    )
    assert json.loads((root / "candidate/runtime-manifest.json").read_text())["distributions"][
        "chatspark-core"
    ]
    bindings = EngineSourceBindingsV1.from_directory(
        build_id="installed-bound",
        source_root=source,
        source_uris={"workshop.md": "https://docs.example/workshop"},
    )
    bound = execute_build(
        EngineBuildRequestV1(
            build_id="installed-bound",
            source_root=source,
            output_root=root / "bound-candidate",
            profile="example",
        ),
        source_bindings=bindings,
    )
    assert bound.status == "succeeded", bound.error
    assert bound.stages["ingestion"].details["source_bindings_sha256"] == bindings.sha256
    import sqlite3

    with sqlite3.connect(root / "bound-candidate/corpus.db") as connection:
        assert connection.execute("SELECT canonical_uri FROM documents").fetchall() == [
            ("https://docs.example/workshop",)
        ]
    import asyncio

    from chatspark.application import (
        AnswerCapacityV1,
        AnswerRequestV1,
        ServingSnapshotV1,
        create_answer_runtime,
        profile_digest,
    )
    from chatspark.retrieval.pipeline import execution_context
    from chatspark.runtime.registry import get_registry

    profile = load_profile("example")
    context = execution_context(root / "candidate/corpus.db")

    class Model:
        def stream_chat(self, messages, *, max_tokens):
            assert "ORCHID-42" in messages[1]["content"]
            yield "The registration code is ORCHID-42. [C1]"

    runtime = create_answer_runtime(
        ServingSnapshotV1(
            build_id="installed",
            database=context.database,
            corpus_id=context.corpus_id,
            chunk_set_id=context.chunk_set_id,
            corpus_sha256=context.snapshot_sha256,
            profile_sha256=profile_digest(profile),
        ),
        profile=profile,
        registry=get_registry(allowed=[]),
        llm=Model(),
        capacity=(capacity := AnswerCapacityV1(max_streams=1)),
    )

    other = execution_context(root / "bound-candidate/corpus.db")
    second = create_answer_runtime(
        ServingSnapshotV1(
            build_id="installed-bound",
            database=other.database,
            corpus_id=other.corpus_id,
            chunk_set_id=other.chunk_set_id,
            corpus_sha256=other.snapshot_sha256,
            profile_sha256=profile_digest(profile),
        ),
        profile=profile,
        registry=get_registry(allowed=[]),
        llm=Model(),
        capacity=capacity,
    )

    async def answer():
        await runtime.warmup()
        await second.warmup()
        first_stream = runtime.stream_answer(AnswerRequestV1(question="registration"))
        await anext(first_stream)
        busy = [e async for e in second.stream_answer(AnswerRequestV1(question="registration"))]
        assert busy[-1].data == {"code": "capacity_busy"}
        await first_stream.aclose()
        events = [e async for e in runtime.stream_answer(AnswerRequestV1(question="registration"))]
        assert events[-1].event == "done"
        assert events[-1].data["sources"]
        await runtime.shutdown()
        sibling = [e async for e in second.stream_answer(AnswerRequestV1(question="registration"))]
        assert sibling[-1].event == "done"
        await second.shutdown()

    asyncio.run(answer())
assert "chatspark_private" not in sys.modules
print("Installed package resources, migration, build, retrieval and application runtime passed")
