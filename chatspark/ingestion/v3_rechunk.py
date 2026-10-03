import json
from pathlib import Path

from chatspark.storage.v3_fts import rebuild_v3_chunks_fts_database
from chatspark.storage.v3_repository import V3Repository


def rechunk_v3_database(
    *,
    database,
    chunk_strategy="structure",
    chunk_set_name="active-v1",
    output=None,
    profile=None,
    **kwargs,
):
    from chatspark.profiles.models import StructuralSettings

    if profile:
        from chatspark.runtime.registry import get_registry

        provider = get_registry().resolve(profile.chunking, "chunker")
        return provider.rechunk(
            database=database,
            chunk_strategy=chunk_strategy,
            chunk_set_name=chunk_set_name,
            output=output,
        )
    config = kwargs.pop("structural_settings", None) or StructuralSettings()
    if chunk_strategy not in {"structure", "structural"}:
        raise ValueError("Non-baseline chunking requires an explicit provider")
    size = config.target_chunk_size_chars if config else 1000
    overlap = config.overlap_chars if config else 100
    with V3Repository(database) as repo, repo.transaction():
        con = repo.connection
        runs = con.execute(
            "SELECT ingestion_run_id,corpus_id,profile_digest FROM ingestion_runs WHERE status='succeeded' ORDER BY started_at DESC"
        ).fetchall()
        if not runs:
            raise ValueError("Successful ingestion required")
        run = runs[0]
        identifier = repo.create_chunk_set(
            corpus_id=run["corpus_id"],
            ingestion_run_id=run["ingestion_run_id"],
            profile_digest=run["profile_digest"],
            chunker=chunk_strategy,
            config={
                "chunk_set_name": chunk_set_name,
                "size": size,
                "overlap": overlap,
                "pdf_size": config.pdf_target_chunk_size_chars if config else size,
                "pdf_overlap": config.pdf_overlap_chars if config else overlap,
                "settings": config.model_dump(mode="json"),
            },
        )
        documents = con.execute(
            """SELECT d.document_id,d.canonical_uri,r.revision_id,r.title,r.text,r.media_type
            FROM documents d JOIN document_revisions r ON d.current_revision_id=r.revision_id WHERE d.deleted_at IS NULL AND d.corpus_id=? ORDER BY d.canonical_uri""",
            (run["corpus_id"],),
        ).fetchall()
        count = 0
        for doc in documents:
            sections = con.execute(
                "SELECT * FROM sections WHERE revision_id=? ORDER BY ordinal", (doc["revision_id"],)
            ).fetchall()
            pieces = [
                (s["text"], s["section_id"], json.loads(s["heading_path_json"] or "[]"))
                for s in sections
            ] or [(doc["text"], None, [])]
            document_size = (
                config.pdf_target_chunk_size_chars
                if config and doc["media_type"] == "application/pdf"
                else size
            )
            document_overlap = (
                config.pdf_overlap_chars
                if config and doc["media_type"] == "application/pdf"
                else overlap
            )
            ordinal = 0
            for text, section, headings in pieces:
                for content in structural_chunks(text, document_size, document_overlap, config):
                    if not content:
                        continue
                    meta = {
                        "url": doc["canonical_uri"],
                        "title": doc["title"],
                        "doc_id": doc["document_id"],
                        "heading_path": headings,
                        "position": ordinal,
                    }
                    repo.add_chunk(
                        chunk_set_id=identifier,
                        revision_id=doc["revision_id"],
                        ordinal=ordinal,
                        text=content,
                        section_id=section,
                        metadata=meta,
                    )
                    ordinal += 1
                    count += 1

        repo.finish_chunk_set(identifier, status="succeeded")
    rebuild_v3_chunks_fts_database(database=database)
    report = {
        "report_version": 1,
        "chunk_set_id": identifier,
        "metrics": {"documents_loaded": len(documents), "chunks_after": {"chunk_count": count}},
    }
    if output:
        Path(output).write_text(json.dumps(report, indent=2) + "\n")
    return report


class StructuralChunker:
    def __init__(self, options):
        self.options = options

    def rechunk(
        self, *, database, chunk_strategy="structural", chunk_set_name="active", output=None
    ):
        if chunk_strategy not in {"structure", "structural"}:
            raise ValueError("Structural chunker cannot execute another chunk strategy")
        return rechunk_v3_database(
            database=database,
            chunk_strategy="structural",
            chunk_set_name=chunk_set_name,
            output=output,
            structural_settings=self.options,
        )


def structural_chunks(text, size, overlap, settings):
    """Pack paragraph/list/table rows; oversized rows use bounded character windows.

    Table continuation chunks repeat a Markdown header where it fits. Overlap
    carries complete recent blocks only, so ordinary rows are never cut in half.
    """
    import re

    blocks = []
    paragraph = []
    header = []
    in_table = False

    def flush():
        if paragraph:
            blocks.append(("\n".join(paragraph), ""))
            paragraph.clear()

    lines = text.splitlines()
    for index, line in enumerate(lines):
        table = settings.table_aware and line.strip().startswith("|") and line.strip().endswith("|")
        if table:
            flush()
            if not in_table:
                header = [line]
                if index + 1 < len(lines) and re.fullmatch(r"[|\s:\-]+", lines[index + 1]):
                    header.append(lines[index + 1])
            repeated = "\n".join(header)
            blocks.append((line, repeated))
            in_table = True
        else:
            in_table = False
            if not line.strip():
                flush()
            elif re.match(r"^\s*(?:[-*+] |\d+[.)] )", line):
                flush()
                blocks.append((line, ""))
            else:
                paragraph.append(line)
    flush()
    chunks, buffer = [], ""
    for block, header in blocks:
        if len(block) > size:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            for start in range(0, len(block), size - overlap):
                chunks.append(block[start : start + size].strip())
            continue
        combined = buffer + ("\n" if buffer else "") + block
        if len(combined) <= size:
            buffer = combined
            continue
        if buffer:
            chunks.append(buffer)
        prefix = header if header and not header.startswith(block) else ""
        if not prefix and overlap:
            previous_lines = buffer.splitlines()
            carried = []
            for value in reversed(previous_lines):
                if len("\n".join([value, *carried])) > overlap:
                    break
                carried.insert(0, value)
            prefix = "\n".join(carried)
        if len(prefix) + len(block) + 1 > size:
            prefix = ""
        buffer = prefix + ("\n" if prefix else "") + block
    if buffer:
        chunks.append(buffer)
    result = []
    for chunk in chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        if len(chunk) < settings.min_chunk_chars:
            if settings.tiny_chunk_policy == "drop":
                continue
            if (
                settings.tiny_chunk_policy == "merge"
                and result
                and len(result[-1]) + len(chunk) + 1 <= size
            ):
                result[-1] += "\n" + chunk
                continue
        result.append(chunk)
    return result
