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
        (["-vj8"], ["-j", "2", "-v"]),
        (["-nvj0"], ["-j", "2", "-n", "-v"]),
        (["-vj1", "-vj8", "-vj0"], ["-j", "2", "-v", "-v", "-v"]),
        (["-vj8", "-j1"], ["-j", "1", "-v"]),
        (["-vvj", "8"], ["-j", "2", "-v", "-v"]),
        (["-C", "-j8"], ["-j", "2", "-C", "-j8"]),
        (["-vC-j8", "-f-j0"], ["-j", "2", "-v", "-C", "-j8", "-f", "-j0"]),
        (
            ["-k", "-j8", "-l0", "-dlist", "-w", "dupbuild=err"],
            ["-j", "2", "-k", "-j8", "-l", "0", "-d", "list", "-w", "dupbuild=err"],
        ),
        (["target", "-vj8"], ["-j", "2", "target", "-v"]),
        (
            ["-vtcommands", "-j8", "--", "target"],
            ["-j", "2", "-v", "-t", "commands", "-j8", "--", "target"],
        ),
        (["--version", "-jinvalid"], ["-j", "2", "--version", "-jinvalid"]),
        (["-vhwhatever", "-j8"], ["-j", "2", "-v", "-hwhatever", "-j8"]),
        (["-j", " +1"], ["-j", "1"]),
        (["-j", ""], ["-j", "2"]),
        (["-j0000000000002"], ["-j", "2"]),
    ],
)
def test_job_limit_and_preserved_arguments(arguments, expected):
    assert BOUNDED(arguments) == expected


@pytest.mark.parametrize(
    "arguments", [["-j"], ["-j-1"], ["-j", "invalid"], ["-vj8v"], ["-j\u0662"]]
)
def test_invalid_job_options_fail_closed(arguments):
    with pytest.raises(ValueError, match="nonnegative integer"):
        BOUNDED(arguments)


@pytest.mark.parametrize("arguments", [["--jobs=8"], ["--ver"], ["-xj8"], ["-C"], ["-j4294967296"]])
def test_unsupported_or_overflow_arguments_fail_before_exec(arguments):
    with pytest.raises(ValueError):
        BOUNDED(arguments)


@pytest.mark.parametrize("target", ["target", "-"])
def test_posix_target_stops_option_parsing(monkeypatch, target):
    monkeypatch.setenv("POSIXLY_CORRECT", "")
    assert BOUNDED([target, "-vj8"]) == ["-j", "2", target, "-vj8"]


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
