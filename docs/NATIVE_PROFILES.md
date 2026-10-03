# Native profile v2

Normal execution requires an explicit `profile_version: 2`. Old profiles must be
converted offline by their owner. Core has no runtime converter or old-profile
provider. Engine request/result messages remain contract v1; plugin API is 2.

A profile describes corpus behavior. An explicitly selected `runtime.yaml`
describes operator model bindings, services, device/batch sizes, state and approved
providers. Secrets come from environment or workload identity. Explicit environment
values override operator-file values. `chatspark --runtime PATH` selects that file;
`CHATSPARK_RUNTIME_CONFIG` can select it for CLI/ASGI processes.

Create a workspace with `chatspark workspace init --path PATH`. It includes a
native profile, secret-free runtime file, environment example and acquisition
policy. `--offline` explicitly disables dense retrieval for an endpoint-free
keyword example. Normal profiles enable sparse and dense retrieval.

## Provider ownership

- `extraction.html` selects cleanup/extraction settings.
- `extraction.pdf` owns ordered extraction/fallback providers and shared limits.
- `extraction.ocr` is optional and explicit.
- `chunking` selects one chunker; its settings own sizes, overlap and structure.
- `retrieval.strategy` owns the complete ranking algorithm. Built-in `hybrid`
  settings contain optional named lanes, planner/processor, FTS weights and MMR
  mode. Shared budgets, fusion weights, standard reranker and final limits belong
  to the outer retrieval policy.
- `retrieval.context` selects canonical context after the strategy has finished.
- `generation` owns prompt overrides, timezone and generation budgets.
- `policy` optionally selects request policy; canonical authorization is enforced
  separately by core.

Providers receive their own validated settings, never an embedded old profile.
Unknown fields and unavailable or incompatible providers fail before processing.
`chatspark profiles schema` composes schemas from installed approved descriptors.

`extends` and external `sections` references resolve relative to the selected
profile. Prompt overrides resolve relative to that profile. No sibling checkout
is inferred. Runtime state remains outside the configuration workspace.

## Build and query

`chatspark corpus build --source PATH --output NEW_PATH --profile PROFILE` derives
dense indexing from the profile. `--source-only` deliberately skips vectors.
The lower-level `engine build` still honors its explicit contract-v1 indexing
request. Index and query use document/query formatting respectively. E5 uses one
`passage:`/`query:` prefix. Serving dense retrieval requires a successful index
with a matching secret-free model fingerprint and populated collection.

The operator embedding binding selects `document_composition: text` (default) or
`title_heading_text`. The latter prepends the canonical title and stored chunk
heading to each passage, deduplicating identical components. It changes model
inputs only; source text and citations remain canonical. Composition is recorded
in the index fingerprint, so changing it requires a new candidate index. E5
prefixes are applied after composition, exactly once. Existing indexes without
this explicit fingerprint field require rebuilding.

Local models must already exist at the selected directory. Loading never executes
remote model code or implicitly downloads a model. The explicit
`chatspark models prepare-e5 --path NEW_MODEL_DIRECTORY` command downloads only
the hash-pinned data files for `intfloat/multilingual-e5-small` revision
`614241f622f53c4eeff9890bdc4f31cfecc418b3`. Its pinned model card declares MIT;
the card is preserved with the selected weights. Weights stay outside the engine.
Install `sentence-transformers` and `qdrant` extras, select the prepared directory
as `runtime.yaml`'s embedding model, backend `sentence-transformers`, revision
above and `text_format: e5`. Run an isolated Qdrant 1.16.3 service, select its URL
and a fresh collection, then use `corpus build`. The reviewed recipe ships hashes,
not weights. This small multilingual model is a useful starting point; evaluate
its recall on your corpus before production use.

## Optional OCR

Install the `ocr` extra and Tesseract with the selected language data, then select
`extraction.ocr: {provider: tesseract, settings: {languages: [eng]}}`. English is
the default; French requires explicitly installed `fra` data. OCR is disabled by
default. Startup validation checks binaries/languages; configuration never installs
them. PDF OCR requires an explicitly selected inspector (normally `pdfium`).

PDF extractors return ordered, page-indexed native content. Tesseract recognizes
pages below `min_native_text_chars` (default 40), keeps other native pages and
merges them in original order. Provenance records each page's extraction method.
Rendering processes one page at a time with 4096-pixel dimension and 16-megapixel
defaults; images with multiple frames are rejected. The extraction service shares
one input/output/deadline budget across native parsing and OCR and kills provider
subprocess descendants on timeout. OCR accuracy and table fidelity require corpus
evaluation; successful extraction alone does not establish answer quality.

Structural chunking packs paragraphs, list items and Markdown table rows. Table
continuations repeat headers where they fit. Oversized rows use bounded character
windows, and their complete-row fidelity is not guaranteed. Configurable tiny
chunks can be kept, merged within the target limit, or dropped. Chunk-set identities
include effective chunker settings; a changed digest requires a new candidate build.
# Metadata and removed declarations

`crawl.canonical_url_rewrites` applies ordered host, exact-path and path-prefix
rules when building canonical document identities. It uses the selected profile,
including builds invoked with an explicit profile different from the serving
default. Rewriting an identity requires a fresh candidate build and index; it
does not change which network addresses the crawler is authorized to fetch.

Corpus scope and language hints describe the corpus; they do not grant access,
select embeddings or activate OCR languages. Evaluation references and example
questions are planning/interface metadata. Evaluation runs require an explicit
request. The former `crawl.trusted_path_roots` declaration had no runtime effect
and is rejected rather than implying a trust or authorization boundary.
