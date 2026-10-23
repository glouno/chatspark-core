# Answer application API v1

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

Events retain `status`, `sources`, `token`, `done` and `error` names. Canonical
citations and source IDs retain the HTTP format. Safe failures never echo provider
errors or content. Diagnostics receive content-free counts through an explicit
callback; this API does not enable prompt/answer capture. Private implementations
must apply their own existing capture policy through scoped callbacks.

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
