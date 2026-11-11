# Licensing and contribution policy

Owned source is licensed under unmodified Elastic License 2.0 (ELv2).
This project is source-available, not OSI-approved open source.

Personal experimentation and commercial self-hosting are permitted. Offering a
hosted service exposing substantial engine functionality is restricted. A small
embedded chatbot requires assessing what functionality customers receive; the
fact of commercial use alone does not prohibit it. Keep copyright/license notices.
Do not circumvent license-key controls; this project introduces no license keys.
ELv2 does not require upstream contributions and does not hide published code.
Previously distributed MIT versions retain their grants.

Dependencies and bundled third-party files retain their own licenses. NOTICE is
attribution, not a substitute for complying with those terms. Optional providers
have separate license inventories. No model weights or OCR stack is included in
the baseline distribution. Public PDF extras require artifact license review.

The owner confirmed ownership of the selected source. Contributions are accepted
under the project license. Commercial relicensing of external contributions needs
additional rights; no contributor license agreement is implied by this policy.

This repository publishes reviewed source. Package/image distribution and
production rollout have separate artifact and security gates. Building an image
does not establish that its exact bundled dependencies and notices are reviewed.

## Container redistribution

[NOTICE](../NOTICE) retains attribution and resource exclusions. The baseline
[Dockerfile](../Dockerfile) builds lxml against checksum-pinned upstream shared
libxml2/libxslt, avoiding the upstream wheel's static GNU libiconv. SQLite is also
built from upstream source; [native-sources.json](../native-sources.json) records
versions, hashes and vendor checksum references. NumPy is built from hash-pinned
source against Debian shared BLAS/LAPACK and GNU runtimes instead of vendored
numerical libraries. Image preparation checks actual links and a numerical case.

Unused lxml isoschematron resources have unresolved transformation redistribution
terms and are excluded while HTML parsing is retained. The final image exports
only the prepared filesystem so removed resources cannot remain in earlier layers.
Deliver exact corresponding-source archives and Python/native/OS notices with a
reviewed image release; source pins alone do not fulfill that delivery requirement.

[Dockerfile.ocr](../Dockerfile.ocr) requires its own native/OS attribution and
corresponding-source review, including Tesseract, language data and PDFium. See
[security](../SECURITY.md) for operator constraints and [release gates](RELEASE_GATES.md)
for exact-artifact acceptance. Wheel/extras contents retain their upstream terms
and need their own inventory rather than inheriting image-review conclusions.
