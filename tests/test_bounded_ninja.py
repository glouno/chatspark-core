"""The image's metadata and wheel runner must enforce actual Ninja job bounds."""

import json
import runpy
import signal
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "bounded_ninja.py"
BOUNDED = runpy.run_path(str(SCRIPT))["bounded_arguments"]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ([], ["-j", "2"]),
        (["--version"], ["-j", "2", "--version"]),
        (["-j0", "-v"], ["-j", "2", "-v"]),
        (["-j", "8", "-j1", "target"], ["-j", "1", "target"]),
        (["-j1", "-j8"], ["-j", "2"]),
        (["-t", "commands"], ["-j", "2", "-t", "commands"]),
        (["--", "-j8"], ["-j", "2", "--", "-j8"]),
    ],
)
def test_job_limit_and_preserved_arguments(arguments, expected):
    assert BOUNDED(arguments) == expected


@pytest.mark.parametrize("arguments", [["-j"], ["-j-1"], ["-j", "invalid"]])
def test_invalid_job_options_fail_closed(arguments):
    with pytest.raises(ValueError, match="nonnegative integer"):
        BOUNDED(arguments)


@pytest.mark.parametrize("termination", [23, -signal.SIGTERM])
def test_native_exec_preserves_process_exit_and_signals(tmp_path, termination):
    native = tmp_path / "ninja"
    action = (
        f"os.kill(os.getpid(), {-termination})\n"
        if termination < 0
        else f"sys.exit({termination})\n"
    )
    native.write_text(
        f"#!{sys.executable}\n"
        "import json, os, signal, sys\n"
        "print(json.dumps({'pid':os.getpid(),'arguments':sys.argv[1:]}),flush=True)\n" + action
    )
    native.chmod(0o700)
    command = (
        "import runpy,sys; "
        "runpy.run_path(sys.argv[1])['exec_ninja'](['-j0','--version'],sys.argv[2])"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", command, str(SCRIPT), str(native)],
        stdout=subprocess.PIPE,
        text=True,
    )
    output, _ = process.communicate(timeout=10)
    assert process.returncode == termination
    assert json.loads(output) == {"pid": process.pid, "arguments": ["-j", "2", "--version"]}
