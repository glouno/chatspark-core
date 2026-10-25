# Portable provider assets and fresh output associations v1

Trusted installed hosts can combine `EngineProfileBundleV1` with explicit
`EngineBuildAssetsV1` through `execute_build(..., profile_bundle=bundle,
provider_assets=assets, services=BuildServices(registry=approved_registry))`.
The optional registry is application composition, never customer executable code.
The profile and original reference values remain unchanged.

Provider descriptors classify file-reference fields with `immutable_input_fields`
or `build_output_fields`, both subsets of `reference_fields` and mutually
exclusive. Unclassified legacy fields remain supported on their existing path;
they are rejected by portable bindings. An index cannot become an auxiliary input
simply because an older descriptor did not declare its role. The additive
`ProfileAssetInventoryV1` / `describe_profile_assets(profile, registry)` describes
these roles without reading files or invoking factories. Existing reference v1
metadata remains unchanged. CLI hosts can emit role metadata alongside it:

```sh
chatspark profiles resolve PROFILE --output profile.json --references-output references.json --assets-output assets-metadata.json
```

`EngineBuildAssetsV1` contains the frozen profile digest, immutable input files
and output targets. Each input binds a provider/field/original-reference triple
to a sealed materialized file, its SHA-256 and size. Each output target identifies
one selected artifact builder, a canonical relative output path and the consuming
reference triples. All references must match the frozen profile exactly, without
missing, duplicate or extra bindings. One provider can produce one declared
output, matching the current artifact-builder interface.

The scoped registry validates settings through the approved descriptor and
substitutes paths only into an independent factory selection. It never rewrites
the profile. Build factories receive new attempt-local output destinations;
consumer factories cannot read those destinations during a candidate build.
Targets must be absent at admission and immediately before artifact construction.
The normal extraction, chunking and artifact paths receive this registry.
PDF/OCR subprocesses receive materialized selections and still discover only
installed allowlisted code. Explicit assets cannot be combined with legacy
parser/chunker/artifact hooks that could bypass the supported composition.

After construction, output digests and paths must match the declared artifacts.
Outputs are sealed as regular, single-link files; symlinks, parent symlinks,
unsafe relative paths, hardlinks and changed content fail. Finalization emits
`provider-output-associations.json` as `EngineOutputAssociationsV1`, and the same
content in the separate runtime manifest's optional `provider_outputs` field.
Each association names the producer settings digest, implementation/distribution,
build ID, corpus, chunk set, source digest, original profile digest, final
canonical database digest and output file digest/size. The strict engine result
v1 is unchanged. Hosts must publish and integrity-check these sidecars along with
the authoritative build, rather than infer them from a filename.

For evaluation, supply `EngineServingAssetsV1` to
`execute_evaluation(..., profile_bundle=bundle, provider_assets=assets,
registry=approved_registry)`. It contains the same immutable inputs and sealed
output files with their associations. Each output must match the successful
selected build and its declared artifact, and the current producer settings and
implementation. Evaluation verifies the canonical database before constructing
retrieval. For answering, pass `provider_assets` and `selected_build` to the
existing `create_answer_runtime` alongside the snapshot, profile, prompt bindings
and explicit dependencies. The snapshot must match that build and database.
All model-facing evidence still resolves through canonical storage and citation
validation. A caller cannot select a foreign artifact by supplying filters.

Published files are fully hashed at admission. Registry-local receipts check
file identity, permissions, links, size and modification/change timestamps before
each preparation/evaluation case; a change invalidates admission. This avoids
rehashing an entire graph on every answer and does not share content-derived
receipts across corpora. This assumes a trusted Linux host/filesystem and is not
protection against an administrator or a compromised application process.

The CLI accepts the appropriate versioned asset JSON beside the profile bundle:

```sh
chatspark engine build --request request.json --result build-result.json --profile-bundle bundle.json --provider-assets build-assets.json
chatspark engine evaluate --request evaluation.json --result evaluation-result.json --profile-bundle bundle.json --provider-assets serving-assets.json
```

Result files cannot overwrite the asset/profile manifests, including aliases.
Omitting bindings preserves legacy engine behavior. A sealed checkpoint can be
materialized for recovery or serving the same build; it is never accepted as
input for a new output role. A new candidate constructs its index from its new
canonical source/chunk set.

Hashes and associations establish integrity and declared scope, not tenant
ownership or signed publication authority. The host separately authorizes and
attests complete configuration/assets, selects the authoritative active build,
retains sidecar digests, fences publication and bounds resource use. It must not
trust browser-supplied build/collection/file selectors or provider definitions.
The v1 request hash still excludes external bundles. No new global model settings,
retrieval defaults, provider algorithms or production placement are selected by
this interface. Private consumers and platform workers require a reviewed pin and
migration before using it; synthetic tests do not certify pooled production
isolation, answer quality or VPS capacity.
