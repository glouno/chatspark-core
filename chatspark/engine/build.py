from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from chatspark.documents.hashing import compute_content_hash
from chatspark.documents.urls import canonicalize_source_url
from chatspark.engine.contracts import (
    BuildArtifactV1,
    BuildStageResultV1,
    EngineBuildRequestV1,
    EngineBuildResultV1,
)
from chatspark.extraction.files import parse_bytes, read_input
from chatspark.indexing.v3_indexer import index_v3_database
from chatspark.ingestion.mirror_files import (
    iter_local_mirror_files,
    resolve_local_mirror_url,
)
from chatspark.ingestion.v3_rechunk import rechunk_v3_database
from chatspark.profiles.loader import load_profile, resolved_profile_artifact
from chatspark.runtime.config import settings
from chatspark.storage.v3_repository import V3Repository, stable_id, utc_now


def execute_build(request: EngineBuildRequestV1, *, services=None) -> EngineBuildResultV1:
    """Build one candidate corpus and return a stable result manifest."""
    from chatspark.engine.services import BuildServices

    services = services or BuildServices()
    version_reader = services.version_reader or _engine_version
    started_at = datetime.now(UTC)
    request_hash = _json_hash(request.model_dump(mode="json"))
    output_root = request.output_root.expanduser().resolve()
    result = EngineBuildResultV1(
        build_id=request.build_id,
        status="failed",
        started_at=started_at,
        finished_at=started_at,
        engine_version=version_reader(),
        request_sha256=request_hash,
    )
    try:
        _check_deadline(request.deadline_at)
        source_root = request.source_root.expanduser().resolve()
        if not source_root.is_dir():
            raise FileNotFoundError(f"source_root is not a directory: {source_root}")
        if output_root.exists() and any(output_root.iterdir()):
            raise FileExistsError(f"output_root must be absent or empty: {output_root}")
        output_root.mkdir(parents=True, exist_ok=True)

        profile_artifact = (services.profile_snapshot or resolved_profile_artifact)(request.profile)
        profile = (services.profile_reader or load_profile)(request.profile)
        from chatspark.runtime.validation import validate_runtime

        (services.validator or validate_runtime)(profile)
        result.profile_digest = str(profile_artifact["profile_digest"])
        profile_path = output_root / "resolved-profile.json"
        _write_json(
            profile_path,
            {key: value for key, value in profile_artifact.items() if key != "sources"},
        )
        result.artifacts.append(_artifact("profile_snapshot", profile_path, output_root))

        files = list((services.file_iterator or iter_local_mirror_files)(source_root))
        if not files:
            raise ValueError(f"source_root contains no supported documents: {source_root}")
        source_hashes = {}
        source_digest = _source_snapshot_digest(source_root, files, file_hashes=source_hashes)
        result.source_snapshot_sha256 = source_digest

        database = output_root / "corpus.db"
        ingestion_started = perf_counter()
        ingestion_counts, corpus_id, ingestion_run_id = _ingest_local_files(
            request=request,
            source_root=source_root,
            files=files,
            database=database,
            profile_artifact=profile_artifact,
            source_digest=source_digest,
            source_hashes=source_hashes,
            profile=profile,
            services=services,
        )
        result.corpus_id = corpus_id
        result.ingestion_run_id = ingestion_run_id
        result.stages["ingestion"] = BuildStageResultV1(
            status="succeeded",
            duration_ms=_elapsed_ms(ingestion_started),
            counts=ingestion_counts,
        )
        chunk_report_path = output_root / "chunk-report.json"
        chunk_started = perf_counter()
        chunk_report = (services.chunker or rechunk_v3_database)(
            database=database,
            chunk_strategy=request.chunk_strategy or profile.chunking.provider,
            profile=profile,
            chunk_set_name=request.chunk_set_name,
            output=chunk_report_path,
        )
        result.chunk_set_id = str(chunk_report["chunk_set_id"])
        chunk_counts = {
            "documents": int(chunk_report["metrics"]["documents_loaded"]),
            "chunks": int(chunk_report["metrics"]["chunks_after"]["chunk_count"]),
        }
        result.stages["chunking"] = BuildStageResultV1(
            status="succeeded",
            duration_ms=_elapsed_ms(chunk_started),
            counts=chunk_counts,
        )
        result.artifacts.append(_artifact("chunk_report", chunk_report_path, output_root))

        from chatspark.retrieval.pipeline import execution_context
        from chatspark.runtime.registry import get_registry

        if services.artifacts:
            extra_artifacts, extra_stages = services.artifacts(
                database, output_root, result, profile
            )
            result.artifacts.extend(extra_artifacts)
            result.stages.update(extra_stages)
        else:
            for selection in profile.artifact_builders:
                _check_deadline(request.deadline_at)
                provider = get_registry().resolve(selection, "artifact")
                artifact_context = execution_context(database, chunk_set_id=result.chunk_set_id)
                from dataclasses import replace

                artifact_context = replace(artifact_context, deadline_at=request.deadline_at)
                emitted = provider.build(database, output_root, artifact_context)
                _check_deadline(request.deadline_at)
                artifact = BuildArtifactV1.model_validate(emitted)
                _validate_artifacts([artifact], output_root)
                result.artifacts.append(artifact)
        if request.dense_index.enabled:
            if request.dense_index.collection_name != settings.QDRANT_COLLECTION:
                raise ValueError(
                    "dense_index.collection_name must match the configured vector collection"
                )
            index_report_path = output_root / "dense-index-report.json"
            dense_started = perf_counter()
            index_report = (services.indexer or index_v3_database)(
                database=database,
                reset_vector_store=request.dense_index.reset_vector_store,
                report_path=index_report_path,
                pre_index_policy=request.dense_index.pre_index_policy,
                chunk_set_id=result.chunk_set_id,
            )
            if not index_report.get("ok"):
                raise RuntimeError("dense indexing did not produce a successful report")
            result.index_run_id = index_report.get("index_run_id")
            result.stages["dense_index"] = BuildStageResultV1(
                status="succeeded" if index_report.get("ok") else "failed",
                duration_ms=_elapsed_ms(dense_started),
                counts={
                    "chunks_total": int(index_report.get("chunks_total") or 0),
                    "chunks_indexed": int(index_report.get("processed_chunks") or 0),
                },
                details={
                    "collection_name": request.dense_index.collection_name,
                    "embedding_model": settings.EMBEDDING_MODEL,
                },
            )
            result.artifacts.append(_artifact("dense_index_report", index_report_path, output_root))
        else:
            result.stages["dense_index"] = BuildStageResultV1(
                status="skipped",
                duration_ms=0,
                details={"reason": "disabled_by_request"},
            )

        result.artifacts.append(_artifact("canonical_database", database, output_root))
        # Later providers/indexing may have changed a previously declared output.
        # Validate the complete composition immediately before declaring success.
        _validate_artifacts(result.artifacts, output_root)
        _check_deadline(request.deadline_at)
        from chatspark.runtime.manifest import write_runtime_manifest

        embedding = None
        if request.dense_index.enabled:
            from chatspark.storage.evidence import EvidenceStore

            with EvidenceStore(database)._connect() as con:
                row = con.execute(
                    "SELECT config_json FROM index_runs WHERE index_run_id=?",
                    (result.index_run_id,),
                ).fetchone()
            if row:
                embedding = json.loads(row["config_json"]).get("embedding")

        (services.manifest_writer or write_runtime_manifest)(
            output_root / "runtime-manifest.json",
            profile=profile,
            artifacts=result.artifacts,
            embedding=embedding,
        )
        current_files = list((services.file_iterator or iter_local_mirror_files)(source_root))
        if _source_snapshot_digest(source_root, current_files) != source_digest:
            raise ValueError("Source snapshot changed during build")
        _validate_artifacts(result.artifacts, output_root)
        _check_deadline(request.deadline_at)
        result.status = "succeeded"
    except Exception as exc:
        from chatspark.runtime.errors import safe_error

        result.error = safe_error(exc, "Build")
    result.finished_at = datetime.now(UTC)
    return result


def execute_build_file(request_path: str | Path, result_path: str | Path) -> EngineBuildResultV1:
    """Validate a request file, execute it, and always write the result manifest."""
    request = EngineBuildRequestV1.model_validate_json(
        Path(request_path).read_text(encoding="utf-8")
    )
    result = execute_build(request)
    _write_json(Path(result_path), result.model_dump(mode="json"))
    return result


def _ingest_local_files(
    *,
    request: EngineBuildRequestV1,
    source_root: Path,
    files: list[Path],
    database: Path,
    profile_artifact: dict[str, Any],
    source_digest: str,
    source_hashes: dict[str, str],
    profile=None,
    services=None,
) -> tuple[dict[str, int], str, str]:
    profile = profile or load_profile(request.profile)
    counts = {
        "files_seen": len(files),
        "documents_created": 0,
        "documents_unchanged": 0,
        "sections": 0,
    }
    with V3Repository(database) as repository, repository.transaction():
        corpus_id = repository.ensure_corpus(
            name=profile.corpus.name,
            metadata={
                "display_name": profile.corpus.display_name,
                "description": profile.corpus.description,
            },
        )
        profile_digest = str(profile_artifact["profile_digest"])
        repository.ensure_profile(
            digest=profile_digest,
            profile_id=f"{profile.corpus.name}:{profile_digest}",
            schema_version=profile.profile_version,
            profile_yaml=json.dumps(
                profile_artifact["profile"], ensure_ascii=False, sort_keys=True
            ),
        )
        source_system_id = repository.ensure_source_system(
            corpus_id=corpus_id,
            kind="local-files",
            root_uri=request.source_base_url,
            metadata={"snapshot_sha256": source_digest},
        )
        ingestion_run_id = repository.start_ingestion_run(
            corpus_id=corpus_id,
            profile_digest=profile_digest,
            source_snapshot=source_digest,
            code_version=(services.version_reader or _engine_version)(),
            config={"contract_version": 1, "build_id": request.build_id},
        )
        for path in files:
            _check_deadline(request.deadline_at)
            path.resolve().relative_to(source_root.resolve())
            raw = read_input(path, pdf_config=profile.extraction.pdf)
            raw_hash = hashlib.sha256(raw).hexdigest()
            expected_hash = source_hashes[path.relative_to(source_root).as_posix()]
            if raw_hash != expected_hash:
                raise ValueError("Source snapshot changed during build")
            canonical_uri, url_resolution = (services.url_resolver or resolve_local_mirror_url)(
                source_root, path, request.source_base_url, content_bytes=raw
            )
            canonical_uri = canonicalize_source_url(
                canonical_uri, profile.crawl.canonical_url_rewrites
            )
            if services.parser and not services.byte_parser:
                # Compatibility for trusted existing path-based integrations.
                parsed = services.parser(
                    path, source_url=canonical_uri, pdf_config=profile.extraction.pdf
                )
            else:
                parsed = (services.byte_parser or parse_bytes)(
                    raw,
                    name=path.name,
                    local_path=path,
                    source_url=canonical_uri,
                    pdf_config=profile.extraction.pdf,
                    **(
                        {
                            "ocr_config": profile.extraction.ocr,
                            "html_config": profile.extraction.html,
                        }
                        if not services.byte_parser
                        else {}
                    ),
                )
            path.resolve().relative_to(source_root.resolve())
            if _file_hash(path) != expected_hash:
                raise ValueError("Source snapshot changed during build")
            if profile.extraction.content_scope == "html-only" and not parsed.mime_type.startswith(
                ("text/html", "application/xhtml+xml")
            ):
                continue
            if len(parsed.content.text.strip()) < profile.extraction.min_document_text_chars:
                continue
            title = path.stem
            if parsed.mime_type.startswith("text/html"):
                element = BeautifulSoup(raw, "html.parser").title
                if element and element.get_text(" ", strip=True):
                    title = element.get_text(" ", strip=True)
            content_hash = compute_content_hash(parsed.content.text)
            document_id = stable_id("document", corpus_id, canonical_uri)
            revision_id, created = repository.upsert_content_document_revision(
                corpus_id=corpus_id,
                source_system_id=source_system_id,
                ingestion_run_id=ingestion_run_id,
                document_id=document_id,
                canonical_uri=canonical_uri,
                content_hash=content_hash,
                text=parsed.content.text,
                markdown=parsed.content.markdown,
                media_type=parsed.mime_type,
                fetched_at=utc_now(),
                # A downloaded/exported web file's local mtime is acquisition
                # metadata, not a source-declared publication/update date.
                source_modified_at=(
                    None
                    if urlsplit(canonical_uri).scheme in {"http", "https"}
                    else datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
                ),
                title=title,
                provenance={
                    "source_kind": "local-files",
                    "relative_path": path.relative_to(source_root).as_posix(),
                    "url_resolution": url_resolution,
                    "extraction": parsed.diagnostics,
                },
            )
            counts["documents_created" if created else "documents_unchanged"] += 1
            if created:
                for ordinal, section in enumerate(parsed.content.sections):
                    repository.add_section(
                        revision_id=revision_id,
                        ordinal=ordinal,
                        text=section.content,
                        heading_path=section.heading_path,
                        start_offset=section.start_char_idx,
                        end_offset=section.end_char_idx,
                    )
                    counts["sections"] += 1
            repository.record_source_observation(
                ingestion_run_id=ingestion_run_id,
                source_system_id=source_system_id,
                canonical_uri=canonical_uri,
                status="added" if created else "unchanged",
                raw_path=path.relative_to(source_root).as_posix(),
                raw_sha256=raw_hash,
                canonical_content_hash=content_hash,
            )
        repository.finish_ingestion_run(ingestion_run_id, status="succeeded", stats=counts)
    return counts, corpus_id, ingestion_run_id


def _source_snapshot_digest(
    root: Path, files: list[Path], *, file_hashes: dict[str, str] | None = None
) -> str:
    digest = hashlib.sha256()
    for path in files:
        path.resolve().relative_to(root.resolve())
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        content_hash = _file_hash(path)
        digest.update(bytes.fromhex(content_hash))
        if file_hashes is not None:
            if relative in file_hashes:
                raise ValueError("Duplicate source snapshot path")
            file_hashes[relative] = content_hash
        digest.update(b"\0")
    return digest.hexdigest()


def _artifact(kind: Any, path: Path, output_root: Path) -> BuildArtifactV1:
    return BuildArtifactV1(
        kind=kind,
        relative_path=path.resolve().relative_to(output_root.resolve()).as_posix(),
        sha256=_file_hash(path),
    )


def _file_hash(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _validate_artifacts(artifacts, output_root):
    seen = set()
    for artifact in artifacts:
        path = output_root / artifact.relative_path
        resolved = path.resolve()
        resolved.relative_to(output_root.resolve())
        if (
            path.is_symlink()
            or not path.is_file()
            or resolved in seen
            or _file_hash(path) != artifact.sha256
        ):
            raise ValueError("Invalid, changed or duplicate build artifact")
        seen.add(resolved)


def _json_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _elapsed_ms(started: float) -> float:
    return round((perf_counter() - started) * 1000, 3)


def _check_deadline(deadline: datetime | None) -> None:
    if deadline is None:
        return
    normalized = deadline if deadline.tzinfo is not None else deadline.replace(tzinfo=UTC)
    if datetime.now(UTC) >= normalized:
        raise TimeoutError(f"build deadline has passed: {normalized.isoformat()}")


def _engine_version() -> str:
    try:
        return version(settings.CHATSPARK_RUNTIME_DISTRIBUTION)
    except PackageNotFoundError:
        return "0+unknown"
