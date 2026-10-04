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

This is the 0.3.0rc2 source release. No PyPI package or hosted image is published
by this release. Optional services and PDF providers are explicit extras; for
example, `uv sync --frozen --extra serve --extra qdrant`. Development checks use
`uv sync --frozen --group dev`.

## Hybrid search by default

Normal profiles and new workspaces enable both semantic (dense) and keyword
(sparse) retrieval. Configure a real embedding backend/model and a Qdrant index;
missing embedding configuration fails validation instead of silently switching
to keyword search or test fingerprints. Core never selects or downloads a model
on your behalf.

```sh
uv run chatspark workspace init --path /tmp/my-chat-workspace --copy-prompts
```

The generated `.env.example` lists the required configuration. Install the
`qdrant` extra; set `EMBEDDING_BACKEND=openai` and `EMBEDDING_MODEL` to a model
served by your `OPENAI_COMPAT_BASE_URL`, or install the `sentence-transformers`
extra and explicitly select that backend and a local model. Local model selection
requires a prepared directory; indexing/retrieving does not download models. No API key is required for a
keyless local endpoint; configure a key separately when your provider requires it.

Build a candidate with `chatspark corpus build --source PATH --output NEW_PATH
--profile PROFILE`. It creates vectors when the selected profile enables dense
retrieval. `--source-only` deliberately skips indexing. Lower-level engine requests
retain explicit index control. Query and index model fingerprints must match.

`chatspark profiles validate --name PATH` checks embedding configuration/dependencies
without contacting the endpoint or downloading a model. Serving/querying then
requires the configured service and built index to be available.

## Explicit offline example

```sh
uv run chatspark workspace init --path /tmp/my-chat-workspace --copy-prompts --offline
uv run chatspark profiles validate --name /tmp/my-chat-workspace/profiles/local/profile.yaml
```

Supply owned local documents through `engine build --request … --result …` using
engine contract v1. The bundled `example` profile and `--offline` workspaces use
keyword-only SQLite retrieval, without a model or inference endpoint. These are
offline examples, not the normal hybrid default. Deterministic embeddings are
test fingerprints and must be selected explicitly for tests; they do not provide
semantic search. Generation requires an explicit OpenAI-compatible endpoint.

For HTTP serving install the `serve` extra and select `CHATSPARK_V3_DB_PATH`.
`uvicorn chatspark.serve.api:app --host 127.0.0.1 --no-access-log` preserves the
version-1 chat request/source response shape. Operator-controlled
`CHATSPARK_AUTHORIZED_FILTERS` is a JSON mapping of mandatory evidence filters;
request and provider filters may narrow it. See [security](SECURITY.md).

Configuration workspaces contain values, optional prompts and acquisition policy.
They do not contain downloaded code or mutable runtime state. Bundled prompts
are generic; local overrides must exist and use only supported template variables.
Remote prompt providers require explicit selection and allowlisting.

Approved providers can declare file-reference settings in their descriptor.
Those references, like local prompt files, resolve relative to the profile or
external section that declares them, including inherited profiles. Other
provider values are preserved and validated by the selected settings model.

[Native profiles](docs/NATIVE_PROFILES.md) · [Extension API](docs/EXTENSIONS.md) · [License policy](docs/LICENSE_POLICY.md)

The reviewed runtime snapshot is
[`c0d9fa6d`](https://github.com/glouno/chatspark-core/commit/c0d9fa6d7bd667b2a9b18045147c8574629ef3e7).
Publication documentation may follow that snapshot without changing the engine.
Synthetic tests do not establish production answer quality or PDF performance
equivalence. See [release gates](docs/RELEASE_GATES.md) and
[security constraints](SECURITY.md) before exposing a deployment.

The baseline container includes serving, SQLite retrieval and pdfminer extraction.
Qdrant support is included in the baseline image; other PDF adapters remain explicit installation extras; the separate
`requirements-qdrant.lock` records the reviewed vector-service environment.
The container excludes unused lxml isoschematron resources whose bundled license
lists transformations with unresolved redistribution terms. HTML parsing is retained.

`Dockerfile.ocr` extends a locally built core candidate with pinned Tesseract,
English/French language data and PDFium rendering. It adds OCR capability; a
profile must still explicitly select `tesseract`. Build with
`docker build -f Dockerfile.ocr --build-arg CORE_IMAGE=YOUR_CORE_IMAGE -t YOUR_OCR_IMAGE .`.
The optional image requires its own OS/native attribution and corresponding-source
review. Neither image bundles embedding model weights.

The Linux image builds lxml from source against checksum-pinned shared
libxml2/libxslt, avoiding the upstream wheel's statically bundled GNU libiconv.
Corresponding source and notice bundles must accompany a reviewed image release.

The image also builds NumPy from its hash-pinned source against Debian shared
BLAS/LAPACK and GNU runtimes. It excludes the upstream wheel's vendored numerical
libraries. Image preparation verifies the actual links and a numerical smoke
case; exact dependency notices and corresponding source remain release artifacts.

The security candidate uses Python 3.11.17 on a pinned Debian Trixie image.
XML/XSLT and SQLite are built from checksum-verified upstream sources and loaded
as replaceable shared libraries (libxml2 2.15.4, libxslt 1.1.45, SQLite 3.53.4).
Their source pins and vendor checksum references are in `native-sources.json`;
exact source archives and notices accompany a selected image release. Installing
from a wheel uses the host's native libraries and requires a separate environment
security review. The optional OCR image uses Tesseract 5.5.3 with explicit English
and French language data. Candidate images remain subject to the dated release
security review; a successful build is not that review.

The optional OCR binary is built without URL fetching, compressed-model archives
or graphics support. It processes local bounded PNG inputs; only reviewed,
operator-installed language data is supported. Keep the language-data directory
read-only and never accept `.traineddata` files from document uploads or profiles.
The exact source pin is in `tesseract-source.json`. These constraints do not make
native image parsing a complete hostile-input sandbox.
