import json
from pathlib import Path

import typer
import yaml

from chatspark.runtime.composition import selected_runtime

app = typer.Typer(help="ChatSpark source-available core engine")
engine = typer.Typer()
profiles = typer.Typer()
workspace = typer.Typer()
prompts = typer.Typer()
corpus = typer.Typer()
models = typer.Typer()
app.add_typer(engine, name="engine")
app.add_typer(profiles, name="profiles")
app.add_typer(workspace, name="workspace")
app.add_typer(prompts, name="prompts")
app.add_typer(corpus, name="corpus")
app.add_typer(models, name="models")


@models.command("prepare-e5")
def prepare_e5(path: Path = typer.Option(...)):
    """Explicitly download the reviewed multilingual E5 weights outside the engine."""
    from chatspark.embeddings.prepare import prepare_model

    recipe = prepare_model(path)
    print(json.dumps({"path": str(path.resolve()), "revision": recipe["revision"]}))


@app.callback()
def configure(runtime: Path | None = typer.Option(None, "--runtime")):
    if runtime:
        # Explicit runtime selection overrides the file selected by environment;
        # explicit environment values retain precedence over operator file values.
        import os

        from chatspark.runtime.config import Settings, settings
        from chatspark.runtime.operator import load_operator_config

        previous = os.environ.get("CHATSPARK_RUNTIME_CONFIG")
        os.environ["CHATSPARK_RUNTIME_CONFIG"] = str(runtime)
        try:
            effective = Settings()
        finally:
            if previous is None:
                os.environ.pop("CHATSPARK_RUNTIME_CONFIG", None)
            else:
                os.environ["CHATSPARK_RUNTIME_CONFIG"] = previous
        for field in Settings.model_fields:
            setattr(settings, field, getattr(effective, field))
        load_operator_config(runtime)


@engine.command("build")
def build(request: Path = typer.Option(...), result: Path = typer.Option(...)):
    from chatspark.engine.build import execute_build_file

    if execute_build_file(request, result).status != "succeeded":
        raise typer.Exit(1)


@corpus.command("build")
def build_corpus(
    source: Path = typer.Option(...),
    output: Path = typer.Option(...),
    profile: str = "hybrid",
    build_id: str = "local-candidate",
    source_only: bool = False,
):
    from chatspark.engine.build import execute_build
    from chatspark.engine.contracts import DenseIndexRequestV1, EngineBuildRequestV1
    from chatspark.profiles import load_profile
    from chatspark.runtime.config import settings

    selected = load_profile(profile)
    dense = selected.retrieval.enable_dense and not source_only
    result = execute_build(
        EngineBuildRequestV1(
            build_id=build_id,
            source_root=source,
            output_root=output,
            profile=profile,
            dense_index=DenseIndexRequestV1(
                enabled=dense, collection_name=settings.QDRANT_COLLECTION if dense else None
            ),
        )
    )
    (output / "build-result.json").write_text(result.model_dump_json(indent=2) + "\n")
    print(result.model_dump_json())
    if result.status != "succeeded":
        raise typer.Exit(1)


@engine.command("evaluate")
def evaluate(request: Path = typer.Option(...), result: Path = typer.Option(...)):
    from chatspark.engine.evaluation import execute_evaluation_file

    if execute_evaluation_file(request, result).status != "succeeded":
        raise typer.Exit(1)


@engine.command("schema")
def schema(output: Path = typer.Option(...)):
    from chatspark.engine.contracts import engine_contract_schema_v1

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(engine_contract_schema_v1(), indent=2) + "\n")


@profiles.command("list")
def list_profiles():
    from chatspark.profiles import available_profiles

    print("\n".join(available_profiles()))


@profiles.command("validate")
def validate_profile(name: str = "hybrid"):
    from chatspark.profiles import load_profile, profile_digest
    from chatspark.runtime.validation import validate_runtime

    profile = load_profile(name)
    validate_runtime(profile, require_dense=True)
    print(json.dumps({"valid": True, "profile_digest": profile_digest(profile)}))


@profiles.command("schema")
def profile_schema():
    from chatspark.profiles.schema import composed_profile_schema
    from chatspark.runtime.registry import get_registry

    print(json.dumps(composed_profile_schema(get_registry()), indent=2))


@workspace.command("init")
def init_workspace(
    path: Path = typer.Option(...), copy_prompts: bool = False, offline: bool = False
):
    from chatspark.profiles import load_profile
    from chatspark.prompts import load_prompt

    path = path.expanduser().resolve()
    if path.exists() and any(path.iterdir()):
        raise typer.BadParameter("Workspace must be empty")
    path.mkdir(parents=True, exist_ok=True)
    directory = path / "profiles/local"
    directory.mkdir(parents=True)
    profile = load_profile("example" if offline else "hybrid")
    profile.corpus.name = "local"
    if copy_prompts:
        prompt_root = path / "prompts"
        prompt_root.mkdir()
        for field, prompt_id in [
            ("system_prompt", "rag.classic_chat.system.v6"),
            ("user_prompt", "rag.classic_chat.user.v2"),
        ]:
            filename = field + ".md"
            (prompt_root / filename).write_text(load_prompt(prompt_id).content + "\n")
            setattr(profile.generation, field, "../../prompts/" + filename)
    (directory / "profile.yaml").write_text(
        yaml.safe_dump(profile.model_dump(mode="json"), sort_keys=False)
    )
    from chatspark.runtime.operator import OperatorConfiguration

    (path / "runtime.yaml").write_text(
        yaml.safe_dump(OperatorConfiguration().model_dump(mode="json"), sort_keys=False)
    )
    (path / ".env.example").write_text(
        "CHATSPARK_PROFILE=local\nCHATSPARK_PROFILE_PATH="
        + str(path / "profiles")
        + "\n"
        + (
            "# Offline keyword-only example; no semantic embedding model is used.\n"
            if offline
            else "# Required for semantic search: select an installed backend and a real model.\n"
            "# EMBEDDING_BACKEND=openai\n# EMBEDDING_MODEL=your-embedding-model\n"
            "# OPENAI_COMPAT_BASE_URL=http://127.0.0.1:8000/v1\n"
            "# QDRANT_URL=http://127.0.0.1:6333\n# QDRANT_COLLECTION=your-built-collection\n"
        )
        + "OBS_REQUEST_TRACE_ENABLED=false\nOBS_CONVERSATION_STORE_ENABLED=false\nOBS_TRACE_CONTENT_MODE=metadata\n"
    )
    (path / "source-policy.json").write_text('{"schema_version":1,"acquisition_enabled":false}\n')
    (path / ".gitignore").write_text(".env\n.env.*\n!.env.example\n*.db\n*.db-*\n")
    print(str(path))


@prompts.command("list")
def prompt_list():
    from chatspark.prompts import prompt_inventory

    print(json.dumps(prompt_inventory(), indent=2))


@app.command("acquire")
def acquire_sources(
    profile: str,
    output: Path = typer.Option(...),
    policy: Path = typer.Option(...),
    allow_loopback: bool = False,
):
    from chatspark.ingestion.acquire import acquire
    from chatspark.profiles import load_profile

    print(json.dumps(acquire(load_profile(profile), output, policy, allow_loopback=allow_loopback)))


_runtime = selected_runtime()
if _runtime is not None:
    app = _runtime.cli_app()

if __name__ == "__main__":
    app()
