"""Collect exact installed Python attribution files for a release dossier."""

import argparse
import hashlib
import json
import shutil
from importlib.metadata import distributions
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    rows = []
    for dist in sorted(distributions(), key=lambda d: d.metadata["Name"].lower()):
        name, version = dist.metadata["Name"], dist.version
        destination = args.output / (name + "-" + version)
        destination.mkdir(exist_ok=True)
        license_files = []
        for entry in dist.files or ():
            if (
                any(
                    term in str(entry).lower()
                    for term in ("license", "licence", "copyright", "notice")
                )
                or str(entry) == "pdfminer/cmap/README.txt"
            ):
                source = Path(dist.locate_file(entry))
                if not source.is_file():
                    continue
                filename = hashlib.sha256(str(entry).encode()).hexdigest()[:12] + "-" + entry.name
                target = destination / filename
                shutil.copyfile(source, target)
                license_files.append(
                    {
                        "path": filename,
                        "original": str(entry),
                        "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    }
                )
        rows.append(
            {
                "name": name,
                "version": version,
                "license_expression": dist.metadata.get("License-Expression"),
                "legacy_license": dist.metadata.get("License"),
                "classifiers": dist.metadata.get_all("Classifier") or [],
                "license_files": license_files,
            }
        )
    (args.output / "python-inventory.json").write_text(
        json.dumps(rows, indent=2, sort_keys=True) + "\n"
    )


if __name__ == "__main__":
    main()
