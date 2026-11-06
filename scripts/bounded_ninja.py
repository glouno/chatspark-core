#!/usr/local/bin/python
"""Cap native image Ninja jobs, including pip's metadata wheel fallback.

meson-python probes the executable selected by NINJA with --version, then
executes it for compilation. pip may omit per-requirement config settings during
metadata preparation. Enforce the bound at that executable boundary instead.
Only job options are capped; probes, tool arguments and targets are preserved.
execv preserves Ninja's exit status, process identity and signal handling.
"""

import os
import sys


def bounded_arguments(arguments):
    result = []
    jobs = 2
    iterator = iter(arguments)
    for argument in iterator:
        if argument == "--":
            result.extend([argument, *iterator])
            break
        if argument == "-j" or argument.startswith("-j"):
            value = next(iterator, "") if argument == "-j" else argument[2:]
            if not value.isascii() or not value.isdecimal():
                raise ValueError("Ninja -j requires a nonnegative integer")
            requested = int(value)
            jobs = min(requested, 2) if requested else 2
        else:
            result.append(argument)
    return ["-j", str(jobs), *result]


def exec_ninja(arguments, executable="/usr/bin/ninja"):
    os.execv(executable, [executable, *bounded_arguments(arguments)])


if __name__ == "__main__":
    try:
        exec_ninja(sys.argv[1:])
    except ValueError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
