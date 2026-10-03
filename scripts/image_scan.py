"""Prepare every regular image-file byte for scanning without following links."""

import argparse
import hashlib
import json
import shutil
import tarfile
from pathlib import Path, PurePosixPath


def prepare(archive_path: Path, destination: Path):
    if destination.exists():
        raise ValueError("Scan destination must not already exist")
    destination.mkdir(parents=True)
    files = byte_count = links = 0
    with tarfile.open(archive_path) as archive:
        for member in archive:
            name = PurePosixPath(member.name.lstrip("/"))
            if ".." in name.parts:
                raise ValueError("Unsafe archive member")
            if member.isfile():
                output = destination.joinpath(*name.parts)
                output.parent.mkdir(parents=True, exist_ok=True)
                stream = archive.extractfile(member)
                with output.open("wb") as target:
                    shutil.copyfileobj(stream, target)
                files += 1
                byte_count += member.size
            elif member.issym() or member.islnk():
                links += 1
    with archive_path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return {
        "archive_sha256": digest,
        "regular_files": files,
        "regular_bytes": byte_count,
        "links_not_followed": links,
        "scope": "All regular-file bytes; scanners may skip binary contents",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("archive", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    report = prepare(args.archive, args.destination)
    if report["regular_files"] < 1000 or report["regular_bytes"] < 50_000_000:
        raise RuntimeError("Export appears incomplete; do not treat its scan as image coverage")
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
