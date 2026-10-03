import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from chatspark.plugins.contracts import RankedEvidence
from chatspark.storage.evidence import EvidenceStore, matches_filters, narrow_filters


class SparseOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fts_bm25_weights: list[float] = Field(
        default_factory=lambda: [1, 5, 3, 2.5], min_length=4, max_length=4
    )

    @field_validator("fts_bm25_weights")
    @classmethod
    def valid_weights(cls, values):
        from chatspark.profiles.models import HybridSettings

        return HybridSettings(fts_bm25_weights=values).fts_bm25_weights


class V3SQLiteSparseRetriever:
    def __init__(self, options=None):
        self.options = options or SparseOptions()

    def retrieve(self, query, limit, context):
        tokens = re.findall(r"\w+", query, re.UNICODE)[:64]
        if not tokens:
            return []
        expression = " OR ".join('"' + t.replace('"', '""') + '"' for t in tokens)
        store = EvidenceStore(context.database)
        narrow_filters({}, context.filters)
        # Restrict canonical membership before ranking, then resolve arbitrary
        # metadata filters in bounded batches rather than loading the corpus.
        with store._connect() as con:
            ranked = con.execute(
                """SELECT v3_chunks_fts.chunk_id, bm25(v3_chunks_fts,0,?,?,?,?,0) AS rank
                FROM v3_chunks_fts JOIN chunks c ON c.chunk_id=v3_chunks_fts.chunk_id
                JOIN document_revisions r ON r.revision_id=c.revision_id
                JOIN documents d ON d.document_id=r.document_id
                WHERE v3_chunks_fts MATCH ? AND c.chunk_set_id=? AND d.corpus_id=?
                AND d.deleted_at IS NULL AND d.current_revision_id=c.revision_id
                ORDER BY rank, v3_chunks_fts.chunk_id""",
                (
                    *self.options.fts_bm25_weights,
                    expression,
                    context.chunk_set_id,
                    context.corpus_id,
                ),
            )
            result = []
            while batch := ranked.fetchmany(max(64, limit)):
                rows = {
                    r["chunk_id"]: r
                    for r in store.rows(
                        corpus_id=context.corpus_id,
                        chunk_set_id=context.chunk_set_id,
                        evidence_ids=[row["chunk_id"] for row in batch],
                    )
                }
                for row in batch:
                    canonical = rows.get(row["chunk_id"])
                    if canonical is not None and matches_filters(
                        store.chunk(canonical).metadata, context.filters
                    ):
                        result.append(RankedEvidence(row["chunk_id"], -float(row["rank"])))
                        if len(result) >= limit:
                            return result
            return result
