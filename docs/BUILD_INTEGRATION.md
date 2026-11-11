# Build integration v1

Trusted installed hosts can supply portable profiles/prompts, provider assets and
source provenance alongside strict engine request/result contract v1. These
optional inputs retain original profile values and references. Omitting them
preserves the existing file-based execution path. Native profiles remain v2 and
the plugin API remains v2; see [native profiles](NATIVE_PROFILES.md) and
[extensions](EXTENSIONS.md).

## Host responsibilities and identity

Hashes and associations establish integrity and declared scope, not tenant
ownership, network acquisition rights or signed publication authority. The host
must authorize inputs, retain and verify complete bundle/sidecar digests with the
job configuration, select the authoritative active build, fence publication and
bound resource use. Never trust browser-supplied build, collection or file selectors
or provider definitions. Installed validation code and providers are trusted code.

`profile_sha256` covers profile values and reference names, not referenced content.
Prompt/file digests cover that content. The runtime manifest records prompt hashes;
evaluation requires the bundle's profile digest to match the selected build.
Engine result v1's `request_sha256` excludes profile and provider asset bundles:

- Without source bindings, it hashes the v1 request alone.
- With source bindings, it hashes canonical JSON containing `request` and
  `source_bindings_sha256`, using sorted keys, compact separators and the engine's
  JSON encoding. Source URIs/times therefore affect build identity even when source
  bytes are unchanged; include them in rebuild/no-change decisions.

These interfaces do not select global model bindings, change retrieval defaults,
implement provider algorithms or establish deployment readiness. Private consumers
require a reviewed pin and migration. Synthetic tests do not certify pooled
production isolation, answer quality, PDF equivalence or host capacity.

## Profile and prompt bundle

Construct `chatspark.engine.profile.EngineProfileBundleV1` with
`EngineProfileBundleV1.from_profile(profile, prompts=verified_prompt_bindings)`.
It contains immutable `profile_json`, the original resolved `profile_sha256` and
`ServingPromptBindingsV1`; `load_profile()` returns an independent validated Profile.
Pass it as `profile_bundle=` to `execute_build` or `execute_evaluation`.

The request's legacy `profile` selector remains required but is not opened when a
bundle is supplied. Verified bound prompt text replaces original file reads, even
after those files are removed; template syntax and supported-variable validation
still apply. Built-in and allowlisted remote prompt selection retain their behavior.
The same prompt bindings work with the [answer application API](APPLICATION_API.md).

Explicit bundles cannot be combined with legacy profile reader/snapshot hooks or
evaluation retriever factories that bypass their settings. Legacy builds load the
profile once; profile drift or a mismatching snapshot fails the build. Provider
file references require the asset bindings below; a profile bundle alone rejects
them instead of implicitly reusing an old index for a fresh build.

## Provider inputs and fresh outputs

Provider descriptors classify `reference_fields` through mutually exclusive
`immutable_input_fields` and `build_output_fields` subsets. Unclassified legacy
fields retain their old path but fail on portable bindings. An old index cannot
become an auxiliary input merely because its descriptor omitted a role.

Reference metadata preserves original references, declaring-profile/section-relative
resolution and the profile digest. `describe_profile_references(profile, registry)`
and `describe_profile_assets(profile, registry)` expose reference v1 metadata and
`ProfileAssetInventoryV1` roles without opening files or invoking factories:

```sh
chatspark profiles resolve PROFILE --output profile.json --references-output references.json --assets-output assets-metadata.json
```

For builds, pass `EngineBuildAssetsV1` as `provider_assets=` alongside the profile
bundle and `services=BuildServices(registry=approved_registry)`. It binds the frozen
profile digest to:

- Immutable inputs: provider/field/original-reference triples, sealed materialized
  files, SHA-256 digests and sizes.
- Output targets: selected artifact builders, canonical relative output paths and
  consuming reference triples. One provider produces one declared output.

All references must match exactly: missing, duplicate or extra bindings fail.
The scoped registry validates approved settings and substitutes paths only in an
independent factory selection; it never rewrites the profile. Output destinations
are attempt-local and must be absent at admission and immediately before
construction. Consumers cannot read them during the candidate build. Extraction,
chunking and artifact paths use the scoped registry; PDF/OCR workers still discover
only installed allowlisted code. Explicit assets reject legacy parser, byte-parser,
chunker and artifact hooks that bypass this composition.

Output paths/digests must match declared artifacts. Sealed outputs must be regular,
single-link files: unsafe paths, symlinks (including parents), hardlinks and changed
content fail. Finalization emits `provider-output-associations.json` as
`EngineOutputAssociationsV1` and the runtime manifest's optional `provider_outputs`.
Associations bind producer settings/implementation/distribution, build, corpus,
chunk set, source/profile/database digests and output digest/size. Publish and
integrity-check these sidecars with the authoritative build; never infer scope
from a filename. The strict engine result v1 shape is unchanged.

For evaluation, pass `EngineServingAssetsV1` with `profile_bundle=` and
`registry=approved_registry`. It carries the same immutable inputs and sealed
outputs/associations. Outputs must match the successful selected build, declared
artifacts and current producer settings/implementation. The canonical database is
verified before retrieval. For answering, pass `provider_assets` and `selected_build`
to `create_answer_runtime` with the snapshot, profile, prompt bindings and explicit
dependencies. Snapshot, build and database must match. Evidence still resolves
through canonical storage/citation validation; filters cannot select foreign assets.

Files are fully hashed at admission. Registry-local receipts check identity,
permissions, links, size and modification/change timestamps before each preparation
or evaluation case; changes invalidate admission. Receipts are not shared across
corpora. This assumes a trusted Linux host/filesystem and does not protect against
an administrator or compromised application process. A sealed checkpoint may be
materialized for recovery or serving the same build, never as input to a new output
role: each new candidate constructs its index from its new source/chunk set.

## Per-file source provenance

`chatspark.engine.source_bindings.EngineSourceBindingsV1.from_directory` constructs
a frozen manifest from `build_id`, `source_root`, a complete
`source_uris={relative_path: absolute_http_uri}` mapping and optional
`acquisition_times={relative_path: aware_datetime}`. Pass it as `source_bindings=`
to `execute_build`. URIs are provenance, never fetched or authorization to acquire
content. Hosts must exclude credentials, temporary signatures and signed query
parameters. Acquisition times normalize to UTC; naive times and unknown source
names fail. Omitted times retain build-time observation; file mtime is not evidence
of acquisition time.

The manifest binds the build ID and existing file-name/content snapshot digest to
exactly one entry per supported source file. Entries carry safe POSIX relative
paths, content SHA-256, absolute HTTP(S) URIs and optional `acquired_at`.
Missing/extra/duplicate paths or URIs, foreign build/snapshot identity, content
mismatches, symlinks and hardlinks fail before extraction. URI validation rejects
malformed authority, userinfo, whitespace and controls; credential-bearing query
parameters additionally require host policy. See the executable
[source-binding models](../chatspark/engine/source_bindings.py) for field limits.

Explicit bindings require the standard source inventory and bypass even a supplied
legacy URL resolver. Selected-profile canonical URL rewrites still apply; collisions
fail before extraction. Parsers receive the URI and SQLite stores it with the
original acquisition time when supplied. Bindings do not change source bytes,
profile values, extraction/chunking or retrieval policy, or combine corpora.

Bindings are revalidated for Python `model_copy`/`model_construct` callers. Source
bytes are checked during/after extraction; inventory and path boundaries are
checked again before success. Freeze inputs: checks detect observed changes but
are not a filesystem snapshot. Failed candidates must never be published.

`source_snapshot_sha256` remains the file-name/content digest. The binding digest
hashes canonical JSON with entries sorted by relative path, including URIs/times
and excluding mutable host paths. It is recorded in ingestion stage details,
ingestion-run configuration and document provenance. Database artifact hashes cover
the resulting evidence. Preserve the manifest for verification and recovery.

## CLI composition

The separate versioned JSON inputs are additive; select only those needed:

```sh
chatspark engine build --request request.json --result build-result.json \
  --profile-bundle bundle.json --provider-assets build-assets.json \
  --source-bindings source-bindings.json
chatspark engine evaluate --request evaluation.json --result evaluation-result.json \
  --profile-bundle bundle.json --provider-assets serving-assets.json
```

Profile/provider manifest result aliases, including symlinks and hardlinks, are
rejected. Source-binding CLI input is capped at 8 MiB and loaded once; hosts must
keep its path and the result path distinct, including aliases. The source-binding
loader checks resolved paths, but unlike profile/provider loaders does not itself
reject a pre-existing hardlink alias; host isolation remains required.
