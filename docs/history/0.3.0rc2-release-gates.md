# Release gates

The 0.3.0rc2 source release comes from a reviewed fresh-history snapshot. Historical
staging repositories and generated customer acceptance data are not published.
Package/image publication and production deployment remain separate operations.
A green implementation checkpoint is not a completed release review.

Required for each release candidate, using its exact source and artifact bytes:

- Core-only frozen install, independent wheel/sdist resource and migration tests.
- Baseline acquisition/import, retrieval and mock grounded-generation tests.
- SQLite/Qdrant filter parity and unauthorized/stale evidence rejection.
- Optional PDF adapter tests, bounded worker timeout/reaping and error handling.
- Exact wheel/sdist/container dependency inventory and bundled-license review.
- Third-party attribution/source references, asset/template ownership review.
- Secret scan of the complete fresh history and exact release artifacts.
- Review of every public file for reserved implementations and customer content.
- Standalone synthetic tutorial run against the exact candidate wheel.
- Private combined artifact and original identity/ranking acceptance, plus private
  customer acceptance outside public source.
- Upgrade/rollback instructions, retained prior combined image and runtime manifests.

Unresolved commercial terms for private PDF/OCR providers do not justify adding
those providers to this distribution. Source availability exposes implementation;
ELv2 cannot conceal an algorithm after release.

## Scope of this source release

The runtime snapshot is `c0d9fa6d7bd667b2a9b18045147c8574629ef3e7`;
subsequent publication documentation does not change its engine implementation.
Native profile v2 and plugin API 2 are required; engine request/result contract v1
is retained. The baseline suite, independent installations, optional extraction
integrations and isolated Qdrant checks passed for the selected runtime.

Container vulnerability review is dated and depends on exact image bytes and
runtime constraints. Reviewed candidates retain scanner matches with scoped
reachability/mitigation assessments; they are not claimed to be vulnerability-free.
Rebuilds, new dependency versions and different architectures require new scans,
inventories, corresponding-source delivery and tests. No container registry or
package release accompanies this GitHub source publication.

Self-host with trusted plugins/model files, bounded ingestion workers and an
authenticated proxy. An anonymous hostile-document service requires stronger
isolation and rate limiting. OCR is opt-in and its small synthetic evaluation
retains a rotated-page limitation. See [SECURITY.md](../SECURITY.md).
