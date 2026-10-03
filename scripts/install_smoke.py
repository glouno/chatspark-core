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
assert "chatspark_private" not in sys.modules
print("Installed package resources, migration, build and retrieval passed")
