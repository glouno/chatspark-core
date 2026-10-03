# Core repository guidance
Read README.md before changes. This repository owns the source-available baseline
engine and versioned extension contracts. Private algorithms, customer data,
PyMuPDF, Surya, model launch stacks and generated benchmark outputs must stay out
of source and distribution artifacts. Never import sibling source directories.
Run focused tests, then audit, full tests and independent wheel/sdist checks.
