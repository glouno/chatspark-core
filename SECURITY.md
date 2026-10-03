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

Report security issues privately to the repository owner through GitHub's private
vulnerability reporting when enabled. Do not post credentials or customer data in
issues. Publication remains gated on an exact-artifact privacy/license review.

Release image checks include a dated vulnerability database and vendor advisory
review alongside secret, dependency-license and functionality checks. A clean
secret scan does not clear dependency vulnerabilities. Update exact base-image
and OS package pins for available security fixes, then rebuild and refresh image
inventories, source delivery and tests. Document unresolved findings and exposure
rather than suppressing them based only on scanner severity. Source/package and
container release gates are reviewed separately.
