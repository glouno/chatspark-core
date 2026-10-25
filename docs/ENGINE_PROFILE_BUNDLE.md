# Portable profile and prompt composition v1

Trusted hosts can call `execute_build(request, profile_bundle=bundle)` and
`execute_evaluation(request, profile_bundle=bundle)` with
`chatspark.engine.profile.EngineProfileBundleV1`. The additive bundle contains
immutable `profile_json`, the original resolved profile's `profile_sha256` and
`ServingPromptBindingsV1`. Construct it with
`EngineProfileBundleV1.from_profile(profile, prompts=verified_prompt_bindings)`.
`bundle.load_profile()` returns an independent validated Profile, with the
original values, prompt references, heuristics and digest.

The same optional input is available as JSON through:

```sh
chatspark engine build --request request.json --result build-result.json --profile-bundle bundle.json
chatspark engine evaluate --request evaluation.json --result evaluation-result.json --profile-bundle bundle.json
```

The request's legacy `profile` selector remains required for compatibility, but
is not opened when a bundle is supplied. Neither the strict v1 engine request/
result schemas nor their existing file-selector execution are replaced. Default
build execution now loads a profile once and derives the snapshot from that
object. Trusted legacy reader/snapshot hooks remain supported, but mismatching
snapshots and profile changes during execution fail the build. Explicit bundles
cannot be combined with profile reader/snapshot hooks or evaluation retriever
factories that could bypass their settings.

Local prompt overrides use verified bound text for validation and runtime
manifest hashes, including after the original files are removed. Template
syntax and supported-variable checks still apply. Built-in and allowlisted
remote prompt selection keep their existing behavior. The prompt bindings can
also be passed to the answer application API. A result file cannot overwrite its
bundle input, including via a symlink or hardlink alias.

This is the first slice of portable asset composition. Provider file references
are rejected on the bundle path until supported auxiliary-input and fresh-build
output bindings exist. This prevents an old index path from being implicitly
reused to build a new candidate. Legacy artifact-backed deployments continue
through their existing path. Recovery/restore remains separate from new builds.

Hosts must authorize the profile and prompt assets before construction, retain
and verify the complete input bundle's content digest, and persist it with the
job/build configuration. `profile_sha256` covers profile values and reference
names, not prompt file content; prompt digests cover that content. Engine result
v1's `request_sha256` still covers only the v1 request. It does not attest an
external bundle. The runtime manifest records prompt hashes, and evaluation
requires the bundle's profile digest to match the selected build. These checks
are integrity checks, not customer ownership or publication authorization.

This interface does not remove global dense indexing configuration, implement
provider output associations, establish deployment readiness, or certify
production answer quality. Each remains a separate acceptance requirement.
