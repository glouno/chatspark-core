# Security

Default operation does not record questions, answers, document text, identities
or provider exception payloads. Prompt bodies are excluded from runtime manifests.
Model downloads, OCR and remote prompt services require explicit configuration.
State belongs outside the source/configuration checkout with restricted permissions.

The baseline HTTP API permits localhost access by default. For remote use put it
behind a trusted authenticated reverse proxy and configure
`CHATSPARK_API_PROXY_SHARED_TOKEN`. Never expose the backend directly or trust
caller-supplied filters as an authorization policy. Set mandatory authorized
filters in the application integration before issuing retrieval requests.

PDF child processes bound time/input size; they do not provide a complete hostile
file sandbox. Run ingestion as an unprivileged container with limited memory/CPU
and no production credentials. Installed plugins are trusted executable code.
Use trusted model files and bounded ingestion workers. An anonymous hostile-document
service requires stronger isolation and rate limiting.

The optional OCR image builds Tesseract without URL fetching, compressed-model
archives or graphics support; it processes local bounded PNG inputs. Keep its
language-data directory read-only and accept only reviewed, operator-installed
data, never `.traineddata` from document uploads or profiles. These controls do
not make native image parsing a complete sandbox. Source provenance is pinned in
[tesseract-source.json](tesseract-source.json); model data and native dependencies
still need the [redistribution review](docs/LICENSE_POLICY.md#container-redistribution).

Report security issues through the repository's Security tab using GitHub private
vulnerability reporting. Do not post credentials or customer data in issues.
Each package/image release requires an exact-artifact privacy/license review.

Release image checks include a dated vulnerability database and vendor advisory
review alongside secret, dependency-license and functionality checks. A clean
secret scan does not clear dependency vulnerabilities. Update exact base-image
and OS package pins for available security fixes, then rebuild and refresh image
inventories, source delivery and tests. Document unresolved findings and exposure
rather than suppressing them based only on scanner severity. Source/package and
container release gates are reviewed separately.
