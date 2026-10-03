"""Exclude unused bundled resources with unresolved redistribution provenance."""

import ctypes
import shutil
import sqlite3
import subprocess
from importlib.metadata import distribution
from pathlib import Path

import numpy
from lxml import etree
from numpy.linalg import _umath_linalg

# lxml's installed license calls these legacy transformations unlicensed.
# ChatSpark uses HTML/XML parsing, never the isoschematron validator.
root = Path(distribution("lxml").locate_file("lxml/isoschematron"))
if root.is_dir():
    shutil.rmtree(root)
print("Removed unused lxml isoschematron implementation/resources from baseline image")


# Inspect the installed ELF's actual links. Upstream's ICONV_COMPILED_VERSION
# reports zero even in wheels that export statically bundled GNU libiconv.

library = ctypes.CDLL(etree.__file__)
if hasattr(library, "libiconv") or hasattr(library, "_libiconv_version"):
    raise RuntimeError("Baseline image unexpectedly contains static GNU libiconv")
links = subprocess.check_output(["ldd", etree.__file__], text=True)
for dependency in ("libxml2.so", "libxslt.so", "libexslt.so"):
    if dependency not in links:
        raise RuntimeError("Baseline lxml must use replaceable system XML libraries")
print("Verified lxml links to system libxml2/libxslt/libexslt without static GNU libiconv")


# NumPy's PyPI wheel vendors GNU libraries. Source-build the same version against
# exact Debian shared libraries so their source/version pairing is reviewable and
# operators can replace the libraries without rebuilding proprietary code.
if Path(distribution("numpy").locate_file("numpy.libs")).exists():
    raise RuntimeError("Baseline NumPy must not contain vendored native libraries")
links = subprocess.check_output(["ldd", _umath_linalg.__file__], text=True)
# Trixie LAPACK does not depend on libquadmath; require its actual shared dependencies.
for dependency in ("libblas.so.3", "liblapack.so.3", "libgfortran.so.5"):
    if dependency not in links:
        raise RuntimeError("Baseline NumPy must use replaceable system numerical libraries")
if not numpy.allclose(numpy.linalg.solve([[2.0, 0.0], [0.0, 4.0]], [4.0, 8.0]), [2.0, 2.0]):
    raise RuntimeError("Baseline numerical-library smoke check failed")
print("Verified NumPy uses replaceable system BLAS/LAPACK/GNU runtime libraries")


# Core imports existing Office files. It never creates documents from bundled
# upstream templates; exclude their thumbnails/printer metadata and unused assets.
for package, relative in (("python-docx", "docx/templates"), ("python-pptx", "pptx/templates")):
    template_root = Path(distribution(package).locate_file(relative))
    if template_root.is_dir():
        shutil.rmtree(template_root)
print("Removed unused Office creation templates from baseline image")

if sqlite3.sqlite_version_info < (3, 53, 4):
    raise RuntimeError("Patched SQLite shared library was not loaded")
if etree.LIBXML_VERSION < (2, 15, 4) or etree.LIBXSLT_VERSION < (1, 1, 45):
    raise RuntimeError("Patched XML/XSLT shared libraries were not loaded")
print("Verified patched SQLite, XML and XSLT runtime versions")
