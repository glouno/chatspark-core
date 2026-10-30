# Answer application API v1

Hosts may pass the same `AnswerCapacityV1(max_streams=4, preparation_slots=2,
preparation_wait=2.0)` as `capacity=` to independently configured answer runtimes.
The immutable configuration owns shared admission gates, with FIFO preparation
waiting bounded by the deadline and responsive cancellation. It holds no corpus,
model, prompt, authorization or request content. Each runtime still verifies its
own snapshot and uses its own retrieval profile and prompts.

Without `capacity=`, existing arguments construct a runtime-local owner. With an
explicit owner, configure limits there; conflicting legacy arguments are rejected.
Runtime shutdown cancels only its own requests and never closes shared capacity.
Hosts still own model-memory limits, authorization and measured load fairness.

`chatspark.application` is an additive supported Python interface for trusted
installed hosts. Engine build/evaluation, CLI and existing HTTP v1 schemas are
unchanged. Install the `serve` extra only when using the optional HTTP adapter.

Construct `ServingSnapshotV1` from authoritative immutable build records, including
the SQLite digest, corpus/chunk-set identity, effective profile digest, collection
and embedding fingerprint. The host authorizes the caller before choosing this
snapshot. Core is corpus-agnostic and does not implement tenant authorization.

```python
from chatspark.application import AnswerRequestV1, create_answer_runtime

runtime = create_answer_runtime(
    snapshot,
    profile=profile,
    registry=allowlisted_installed_registry,
    llm=explicit_model,
    authorized_filters=mandatory_filters,
    # Dense profiles additionally require:
    embedder=explicit_embedder,
    vectorstore=explicit_vectorstore,
    embedding_identity=verified_embedding_identity,
)
await runtime.warmup()
runtime.readiness()
stream = runtime.stream_answer(AnswerRequestV1(question="What is the registration code?"))
try:
    async for event in stream:
        consume(event.event, event.data)
finally:
    await stream.aclose()
await runtime.shutdown()
```

The factory takes dependencies rather than reading model, database or collection
selection from environment settings. Embedding identities use the same manifest
as candidate indexing; `embedding_fingerprint(embedder, configuration=...)` supports
explicit operator configuration. Establish endpoint-dependent dimensions before
construction. The factory checks the actual collection binding and matching sealed
index record. Runtime warmup and canonical retrieval verify the SQLite identity.
Mount snapshots and prompt/configuration artifacts read-only; never update them
in place. Runtime selection and ownership remain the host's responsibilities.

For local prompt overrides, optionally supply `ServingPromptBindingsV1` through
`create_answer_runtime(..., prompt_bindings=...)`. It carries the original profile
SHA-256 and optional `PromptAssetV1` system/user values containing the exact frozen
reference, text and SHA-256 of its UTF-8 text. The engine verifies the profile,
reference and text digest; it reads the bound text without opening the reference.
The host must first verify the asset's authorized build ownership and sealed
artifact integrity. A digest alone does not authorize content. Missing, foreign or
mismatched bindings fail before prompt I/O. Bundled/remote selection, prompt
provenance, rendering and the original profile digest retain their behavior.
Omitting the bindings keeps existing file-based behavior for legacy hosts. This
contract does not relocate provider configuration assets or grant access to files.

Events retain `status`, `sources`, `token`, `done` and `error` names. Canonical
citations and source IDs retain the HTTP format. Safe failures never echo provider
errors or content. Diagnostics receive content-free counts through an explicit
callback; this API does not enable prompt/answer capture. Private implementations
must apply their own existing capture policy through scoped callbacks.

The early `sources` event contains retrieved context, before generation finishes.
The completed `done.sources` is empty when the answer cites no evidence, including
an unsupported-question abstention. Hosts must use this completed value for answer
references. When evidence is cited, the original source array positions remain the
`[C1]`, `[C2]`, … label table; unused positions are retained to preserve streamed
markers. Unavailable citation markers fail rather than linking to another source.

Models implement `stream_chat(messages, max_tokens=...)`. Each returned iterator
owns `close()` and may expose `get_metadata()` for request-local usage and gateway
attempt identity. Mutable "last call" metadata on a shared model is never read.
For immediate interruption of blocking reads, the request-local iterator implements
a thread-safe, idempotent `cancel()`. Synchronous providers without this hook must
bound their network reads; cancellation waits for the current read before closing
the generator. The engine never closes a generator while another thread is reading
it. Cancellation and explicit early close release preparation/stream slots; shutdown
prevents admission and interrupts current handles. The host drains consumers before
disposing the runtime/model pool. Python context variables propagate across reads.

Default bounds are four streams, two evidence preparations and a two-second wait.
They are configurable and must be shared by the host's stamp admission policy;
per-runtime limits alone are not a host memory/capacity envelope. Private query and
answer caches are absent from this implementation.
Paused status consumers do not hold evidence-preparation slots. Output is bounded
to 65,536 characters per delta and 262,144 characters per answer before buffering;
providers exceeding either limit fail with a bounded error and close their stream.

`chatspark.serve.application.create_http_app(runtime, authorize=...)` adapts this
interface to `/health`, `/ready`, `/chat` and `/chat/stream`. The host supplies an
authentication callback. The legacy environment-configured app remains supported
and uses the same answer preparation/citation validation functions. The explicit
adapter does not replace the legacy browser/static frontend.
The explicit HTTP readiness response omits the canonical database filesystem path;
trusted Python hosts can inspect the full snapshot directly.

Synthetic API tests prove interface, canonical grounding and cleanup behavior.
Shared hosting still requires real engine/vector/database isolation and capacity
campaigns in the consuming deployment.

### Owned vector-client cleanup

`QdrantVectorStore.close()` releases only its owned client's connections; it
does not delete the collection. Drain runtime streams before releasing the host's
vector binding. Active vector operations reject closure and keep the store open.
Successful closure is idempotent and all later operations (including empty
queries/writes) fail rather than reopening a connection. Failed disposal disables
operations but permits another `close()` attempt. Failed construction closes the
client before propagating its original discovery error. Runtime shutdown itself
does not dispose injected dependencies: their shared/scoped owner does.
