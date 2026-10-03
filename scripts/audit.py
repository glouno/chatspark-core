"""Audit tracked source paths without printing secret values."""

import re
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
files = subprocess.check_output(
    ["git", "ls-files", "--cached", "--others", "--exclude-standard"], cwd=root, text=True
).splitlines()
problems = []
for name in files:
    path = root / name
    if not path.is_file():
        continue
    if re.search(
        r"(^|/)(\.env|__pycache__|dist|build|private)(/|$)|\.(db|sqlite|log|pdf|png|jpg|parquet)$",
        name,
    ):
        problems.append((name, "runtime/binary/private path"))
    if path.suffix in {".py", ".md", ".toml", ".yaml", ".yml", ".json", ".html", ".sh"}:
        text = path.read_text()
        if name not in {"scripts/audit.py", "scripts/install_smoke.py"} and re.search(
            r"lip6|sorbonne|paulbeglin|/home/[^/]+/|/Users/[^/]+/|chatspark_private|import pymupdf|import surya|private\.example",
            text,
            re.I,
        ):
            problems.append((name, "reserved deployment/implementation reference"))
        if re.search(
            r"sk-proj-[A-Za-z0-9_-]{20,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", text
        ):
            problems.append((name, "potential credential"))
for name, reason in problems:
    print(name + ": " + reason)
if problems:
    raise SystemExit(1)
print(f"Audited {len(files)} source files")
