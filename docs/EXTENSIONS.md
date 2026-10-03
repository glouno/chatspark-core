# Extension API v2

Core exposes complete retrieval strategies, lanes, planners, candidate processors, context builders,
artifact builders and PDF/OCR contracts through `chatspark.plugins`. Implement
providers in a separate installable distribution. Core owns canonical evidence
text, URLs and authorization; providers return ranked evidence IDs.

Register descriptors through the `chatspark.plugins` entry-point group. The
operator must list provider IDs in `CHATSPARK_PLUGINS_ALLOWED`. A profile selects
those IDs and schema-validated settings. Installed packages alone have no effect.
Unknown, incompatible, duplicated or unapproved required providers fail validation.
Profiles cannot install code, specify Python import paths or execute commands.

Providers run as trusted application code. This interface is not a sandbox.
PDF extraction uses bounded child processes, a shared 120-second deadline and
50 MiB input limit by default. Operators may adjust those limits explicitly.
Providers may narrow authorized filters, never widen them. Core validates each
ID against the chosen corpus, current revisions, chunk set and snapshot before
fusion and generation. Processors may select only their input candidates.
Context builders may select further authorized canonical chunks within the limit.

The runtime manifest is separate from strict engine request/result contract v1.
It records distribution/provider versions, configuration and prompt hashes and
artifact hashes. Never put credentials in provider settings or profiles.

Planners return at most 16 typed lane requests with nonempty queries of at most
16,000 characters, integer limits of 1–1,000 and finite weights of 0–100. Filters
contain named scalar values or lists of scalars. Malformed requests fail before
backend execution. Core checks the shared deadline after planning and every
retrieval, processing, reranking and context stage, and before returning evidence.
In-process trusted providers cannot be forcibly interrupted; late results are
rejected. Extraction subprocesses have enforced termination deadlines.

Artifact builders receive the build deadline and return a safe relative path and
SHA256 digest. Core rejects missing, escaping, symlinked, duplicate or changed
outputs and rechecks all declared artifacts before the build succeeds. Store
completed outputs as immutable artifacts; trusted in-process providers retain
filesystem privileges, so this validation does not isolate malicious code.

Build extraction consumes the bytes verified against the source snapshot.
Source hashes are checked before/after extraction and the complete selected file
set is checked again before success. Existing trusted path-based build services
remain compatible but should use `BuildServices.byte_parser` to avoid rereading
input. Inputs must still be frozen by the operator; hash checks detect observed
changes and do not provide a filesystem snapshot or sandbox concurrent writers.

A trusted `runtime` provider may compose an existing application's CLI and ASGI
app. Selection belongs to the operator through `CHATSPARK_RUNTIME_PROVIDER` and
its explicit allowlist, never customer YAML. The provider exposes `cli_app()` and
`http_app()`. Default CLI/HTTP behavior is unchanged by installing a distribution.
A selected provider owns the lifecycle and compatibility of its composed app;
core still owns its canonical package files and versioned engine contracts.

Explicit v2 OCR selection runs for image input and low-text PDF pages, including
mixed native/scanned PDFs. It shares the extraction limits and deadline. The
optional Tesseract provider requires separately installed binaries and language
data; core never installs them. Pages retain source order and native/OCR provenance.
Private sidecar workflows remain separately selected offline integrations.

Worker input/output use private temporary files; the parent waits for process
completion rather than an unbounded pipe read. PDF, inspection and OCR result
serialization/transfer share the extraction deadline. Serialized output is limited
to 64 MiB by default (`extraction.pdf.max_output_bytes`); oversized results fail
with `output_limit`, never become indexed text. Temporary files are removed after
worker/descendant cleanup. These bounds are resource controls, not a code sandbox.


A `RetrievalStrategy` receives the shared retrieval policy, immutable execution
context and operator services. It returns ranked canonical IDs, named ranking
stages and bounded numeric diagnostics. Core validates every final/intermediate
ID, then runs the selected context provider and revalidates. It never repeats
fusion, reranking or final selection after the strategy. The built-in `hybrid`
strategy owns ordinary lane orchestration. No plugin API-1 shim is installed.

Serving can explicitly call `StrategyRetriever.warmup()`. It executes a generic
readiness query through normal canonical validation, then invokes the selected
strategy's optional `warmup(context, policy, services)` lifecycle method. This
permits local model initialization without adding another ranking pass. Installing
a provider alone never invokes its lifecycle.

A selected prompt provider returns a typed `ResolvedPrompt` with text, actual
revision and implementation version. Generation records only hashes, provider
identity and those versions by default. A local prompt override takes precedence;
remote failures use bundled prompts only when fallback is explicitly enabled.


Canonical ranking stages are resolved together through indexed evidence-ID
lookups within one immutable snapshot and authorization scope. Each stage keeps
its own bounds, scores and independent canonical objects. Final results, context
expansion and generation still validate their evidence separately; stage batching
does not allow providers to replace content or widen access.
