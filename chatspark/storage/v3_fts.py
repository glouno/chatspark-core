import json
import sqlite3


def rebuild_v3_chunks_fts_database(*, database, dry_run=False):
    with sqlite3.connect(database) as con:
        before = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        if not dry_run:
            con.execute("DROP TABLE IF EXISTS v3_chunks_fts")
            con.execute(
                "CREATE VIRTUAL TABLE v3_chunks_fts USING fts5(chunk_id UNINDEXED, content, display_title, heading_text, url_tokens, retrieval_lane UNINDEXED)"
            )
            rows = con.execute("""SELECT c.chunk_id, c.text, c.metadata_json, r.title, d.canonical_uri, s.heading_path_json
                FROM chunks c JOIN document_revisions r ON r.revision_id=c.revision_id
                JOIN documents d ON d.document_id=r.document_id LEFT JOIN sections s ON s.section_id=c.section_id ORDER BY c.chunk_id""")
            values = [
                (
                    cid,
                    text,
                    title or "",
                    " ".join(json.loads(headings or "[]")),
                    url or "",
                    json.loads(meta or "{}").get("retrieval_lane", ""),
                )
                for cid, text, meta, title, url, headings in rows
            ]
            con.executemany("INSERT INTO v3_chunks_fts VALUES (?,?,?,?,?,?)", values)
    return {"report_version": 3, "ok": True, "chunks": before}
