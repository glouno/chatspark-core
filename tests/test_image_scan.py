"""A Linux absolute symlink must not truncate scanning of later image files."""

import importlib.util
import io
import tarfile
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "image_scan", Path(__file__).resolve().parents[1] / "scripts/image_scan.py"
)
image_scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(image_scan)


def test_image_links_are_not_followed_and_later_files_are_scanned(tmp_path):
    archive = tmp_path / "image.tar"
    with tarfile.open(archive, "w") as output:
        link = tarfile.TarInfo("etc/alternatives/awk")
        link.type = tarfile.SYMTYPE
        link.linkname = "/usr/bin/mawk"
        output.addfile(link)
        source = tarfile.TarInfo("usr/local/lib/example.txt")
        body = b"synthetic content"
        source.size = len(body)
        output.addfile(source, io.BytesIO(body))
    destination = tmp_path / "files"
    report = image_scan.prepare(archive, destination)
    assert report["regular_files"] == 1
    assert report["regular_bytes"] == len(body)
    assert report["links_not_followed"] == 1
    assert (destination / "usr/local/lib/example.txt").read_bytes() == body
    assert not (destination / "etc/alternatives/awk").exists()


def test_image_path_traversal_is_rejected(tmp_path):
    archive = tmp_path / "image.tar"
    with tarfile.open(archive, "w") as output:
        source = tarfile.TarInfo("../../escape")
        source.size = 1
        output.addfile(source, io.BytesIO(b"x"))
    with pytest.raises(ValueError, match="Unsafe archive member"):
        image_scan.prepare(archive, tmp_path / "files")
    assert not (tmp_path / "escape").exists()
