# ChatSpark Core

A corpus-agnostic retrieval engine under the
[Elastic License 2.0](LICENSE). This is source-available software.

Core owns canonical SQLite evidence, ordinary sparse/dense retrieval, grounded
answers, external profiles and trusted extension contracts. Private ChatSpark
implementations are separately installed and explicitly selected by the operator.
No model weights, customer corpus or credentials are included.

Python 3.11 and [uv](https://docs.astral.sh/uv/) are required.

```sh
git clone https://github.com/glouno/chatspark-core.git
cd chatspark-core
uv sync --frozen
uv run --frozen chatspark --help
```

Package metadata is `0.3.0rc2`. Its original source publication included no PyPI
package or hosted image; this is not a live registry-status check. Optional services
and PDF providers are explicit extras, for example
`uv sync --frozen --extra serve --extra qdrant`. Development checks use
`uv sync --frozen --group dev`.

## Hybrid search by default

Normal profiles and new workspaces enable semantic (dense) and keyword (sparse)
retrieval. Missing embedding configuration fails validation instead of silently
switching to keyword search or test fingerprints. Core never selects a model on
your behalf; indexing/retrieving does not download one.

```sh
uv run --frozen chatspark workspace init --path /tmp/my-chat-workspace --copy-prompts
```

The generated `.env.example` lists required configuration. Install the `qdrant`
extra; select a real `EMBEDDING_MODEL` with `EMBEDDING_BACKEND=openai` served at
`OPENAI_COMPAT_BASE_URL`, or install the `sentence-transformers` extra and select
that backend with a prepared local model directory. Keyless local endpoints need
no API key; configure a key separately when required.

`chatspark corpus build --source PATH --output NEW_PATH --profile PROFILE`
creates vectors when the profile enables dense retrieval. `--source-only` skips
vectors; lower-level engine requests retain explicit index control. Index/query
model fingerprints must match. `chatspark profiles validate --name PATH` checks
configuration/dependencies without contacting endpoints or downloading a model.
Serving/querying requires the selected services and built index to be available.

## Offline local-document build

Run from the installed source checkout. This creates a fresh temporary root;
configuration, source documents and mutable build output occupy separate trees.

```sh
example_root="$(mktemp -d "${TMPDIR:-/tmp}/chatspark-offline.XXXXXX")"
uv run --frozen chatspark workspace init --path "$example_root/config" --copy-prompts --offline
mkdir "$example_root/documents"
printf 'The workshop registration code is ORCHID-42.\n' > "$example_root/documents/guide.txt"
uv run --frozen chatspark profiles validate --name "$example_root/config/profiles/local/profile.yaml"
uv run --frozen chatspark corpus build --source "$example_root/documents" \
  --output "$example_root/candidate" --profile "$example_root/config/profiles/local/profile.yaml"
```

The command exits successfully and writes `candidate/build-result.json` with
`status: succeeded`, plus `candidate/corpus.db` and a runtime manifest. This builds
keyword-only SQLite evidence without a model, network acquisition or inference
endpoint. It does not exercise generation: answering requires an explicit
OpenAI-compatible endpoint. After inspecting the result, remove only this example's
temporary root when no longer needed. Deterministic embeddings are explicit test
fingerprints, not semantic search.

## Serving and integration

Install the `serve` extra, select the intended profile and set
`CHATSPARK_V3_DB_PATH` to its canonical database before starting
`uvicorn chatspark.serve.api:app --host 127.0.0.1 --no-access-log`.
The legacy API retains the v1 chat/source shape. Operator-controlled
`CHATSPARK_AUTHORIZED_FILTERS` supplies mandatory evidence filters; request/provider
filters may only narrow them. Follow [security constraints](SECURITY.md) before
remote exposure.

Configuration workspaces hold values, optional prompts and acquisition policy,
not downloaded code or mutable runtime state. Local prompt overrides must exist
and use supported variables; remote providers require explicit allowlisting.
Trusted hosts can use the [answer application API](docs/APPLICATION_API.md) for
explicit snapshot/dependency composition and shared capacity.

- [Native profiles](docs/NATIVE_PROFILES.md): model bindings, build/query and OCR.
- [Extensions](docs/EXTENSIONS.md): trusted installed provider contracts.
- [Build integration](docs/BUILD_INTEGRATION.md): profile/prompt bundles, reference
  inventories, fresh provider outputs and per-file source URIs/acquisition times.
- [License policy](docs/LICENSE_POLICY.md): contribution and container redistribution.
- [Release gates](docs/RELEASE_GATES.md): exact-artifact checks and historical scope.

The baseline image includes serving, SQLite, Qdrant support and pdfminer.
`Dockerfile.ocr` adds pinned Tesseract, English/French data and PDFium rendering;
profiles must explicitly select OCR. Build it with
`docker build -f Dockerfile.ocr --build-arg CORE_IMAGE=YOUR_CORE_IMAGE -t YOUR_OCR_IMAGE .`.
Neither image includes embedding weights. Native source pins are executable build
inputs, not proof of security acceptance; source, installed artifacts and hosted
runtime each require matching verification.
