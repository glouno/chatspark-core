# Release gates

Package/image publication and production deployment are separate operations.
A green implementation checkpoint is not a completed release review. Historical
staging refs and generated customer acceptance data must stay outside public source.

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

Container vulnerability review depends on exact image bytes, dated scanner/vendor
findings and runtime constraints. Rebuilds, dependency changes and architectures
require matching scans, inventories, corresponding-source delivery and tests.
Retain scoped reachability/mitigation assessments; do not claim vulnerability-free
artifacts. Package installations have a separate native-dependency environment to
review. See [licensing](LICENSE_POLICY.md) and [security](../SECURITY.md).

## Historical 0.3.0rc2 publication evidence

The [original release-gate record](history/0.3.0rc2-release-gates.md) is preserved
byte-for-byte from repository revision
[`025a22e194fdb2e7651df0a024fe87f25127cd36`](https://github.com/glouno/chatspark-core/commit/025a22e194fdb2e7651df0a024fe87f25127cd36).
Its relative links used the original `docs/` location. It describes runtime
`c0d9fa6d7bd667b2a9b18045147c8574629ef3e7`; later commits changed implementation.
Its reported test results, scoped vulnerability findings and synthetic OCR
rotated-page limitation are historical evidence, not verification of current HEAD.

The original publication's absence of a package/registry image is not a live
status check. Source acceptance does not establish installed or hosted acceptance;
synthetic tests do not establish production answer quality or PDF performance
equivalence. Apply the gates to each selected revision/artifact.
