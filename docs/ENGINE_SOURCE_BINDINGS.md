# Explicit source bindings

Offline builds may bind each immutable input file to its authoritative source URI
and optional original acquisition timestamp. This avoids interpreting HTML mirror
text or guessing one base URL for a mixed collection of files. The engine does not
fetch these URIs or authorize access to content. Hosts supply stable URIs without
credentials or temporary signatures and retain the immutable manifest with their
build inputs; host policy owns network acquisition and authorization.

`EngineBuildRequestV1` and `EngineBuildResultV1` retain their strict wire shapes.
The additive Python keyword and CLI option accept a separate versioned manifest:

```python
from chatspark.engine.build import execute_build
from chatspark.engine.source_bindings import EngineSourceBindingsV1

bindings = EngineSourceBindingsV1.from_directory(
    build_id=request.build_id,
    source_root=request.source_root,
    source_uris={
        "guide.html": "https://docs.example/guide",
        "syllabus.txt": "https://sources.invalid/uploads/syllabus",
    },
)
result = execute_build(request, source_bindings=bindings)
```

Supply `acquisition_times={relative_path: aware_datetime}` to preserve original
acquisition times. Times normalize to UTC; naive timestamps and extra source names
are rejected. Omitted times retain the existing build-time observation behavior.
No acquisition date is inferred from a copied file's mtime.

```sh
chatspark engine build --request request.json --result result.json \
  --source-bindings source-bindings.json
```

The strict frozen `chatspark.engine-source-bindings.v1` manifest contains a build ID,
the existing source snapshot SHA256, and up to 10,000 entries with safe POSIX relative
names, content SHA256, absolute HTTP(S) source URIs and optional timezone-qualified
`acquired_at`. CLI manifests are capped at 8 MiB and loaded once. URI validation
rejects malformed authority, userinfo, whitespace and control characters; hosts
must also reject signed or credential-bearing query parameters under their policy.

All supported source files must have exactly one binding. Missing/extra/duplicate
paths or URIs, foreign build/snapshot identity, content hash mismatch, symlinks and
hardlinks fail before extraction. Explicit binding requires the standard source
inventory and bypasses the legacy URL resolver, including a supplied trusted
resolver. The selected profile's canonical URL rewrites still apply; collisions
after rewriting fail before extraction. Parsers receive the selected URI, and
canonical SQLite evidence stores it with the original acquisition time when given.
Source bytes, profile values and extraction/chunking behavior remain unchanged.

Bindings are revalidated even for Python `model_copy`/`model_construct` callers.
The engine checks source bytes against the frozen hashes during and after extraction
and checks the source inventory and path boundaries again before success. Failed
candidates never attest success and must not be published by their host.

## Identity and compatibility

When bindings are absent, legacy URL inference, source digest and request hash
behavior are unchanged. With explicit bindings, `source_snapshot_sha256` remains
the existing file-name/content snapshot digest. Binding SHA256 is computed from
canonical JSON of the manifest with entries sorted by relative path; it includes
source URIs and acquisition times and excludes mutable host paths.

`request_sha256` becomes SHA256 of canonical JSON containing `request` (the unchanged
request model) and `source_bindings_sha256`. Canonical JSON uses sorted keys and
compact separators with the existing engine JSON encoding. The binding digest is
also recorded in ingestion stage details, ingestion-run configuration and document
provenance. Canonical database artifact hashes cover the resulting evidence.
Changing a URI or acquisition time therefore changes build identity even if source
bytes are identical. Hosts must include the binding in their rebuild/no-change
decision and preserve it for verification and recovery.

The baseline extraction and corpus profile are still selected independently.
Bindings supply provenance; they do not combine corpora, change retrieval policy,
grant publication authority or permit using an old index as new candidate input.
