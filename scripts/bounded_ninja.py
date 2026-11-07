#!/usr/local/bin/python
"""Cap native image Ninja jobs, including pip's metadata wheel fallback.

meson-python probes the executable selected by NINJA with --version, then
executes it for compilation. pip may omit per-requirement config settings during
metadata preparation. Enforce the bound at that executable boundary instead.
Parse Ninja 1.12.1's short-option clusters and operand boundaries. Only job
options are capped; probes, tool arguments and targets are preserved. Unsupported
long abbreviations and ambiguous job values fail closed before native execution.
execv preserves Ninja's exit status, process identity and signal handling.
"""

import os
import re
import sys


def bounded_arguments(arguments):
    result = []
    jobs = 2
    iterator = iter(arguments)
    for argument in iterator:
        if argument == "--":
            result.extend([argument, *iterator])
            break
        if argument in ("--version", "--help"):
            result.extend([argument, *iterator])
            break  # ReadFlags exits before compilation or parsing later options.
        if argument in ("--verbose", "--quiet", "-") or not argument.startswith("-"):
            result.append(argument)
            if (
                argument == "-" or not argument.startswith("-")
            ) and "POSIXLY_CORRECT" in os.environ:
                result.extend(iterator)
                break
            continue
        if argument.startswith("--"):
            raise ValueError("Unsupported Ninja long option or abbreviation")
        cluster = argument[1:]
        for index, option in enumerate(cluster):
            if option in "nvh":
                result.append("-" + option)
                if option == "h":
                    # Ninja exits at this character; leave the unparsed suffix.
                    result[-1] = "-" + cluster[index:]
                    result.extend([*iterator])
                    return ["-j", str(jobs), *result]
                continue
            if option not in "dfjkltwC":
                raise ValueError("Unsupported Ninja short option")
            value = cluster[index + 1 :] or next(iterator, None)
            if value is None:
                if option == "j":
                    raise ValueError("Ninja -j requires a nonnegative integer")
                raise ValueError("Ninja option requires an operand")
            if option == "j":
                # strtol permits leading ASCII whitespace, + and empty operands.
                # Bound its int range rather than depend on overflow conversion.
                if value and not re.fullmatch(r"[\t\n\v\f\r ]*\+?[0-9]+", value):
                    raise ValueError("Ninja -j requires a nonnegative integer")
                digits = value.lstrip("\t\n\v\f\r +").lstrip("0")
                if len(digits) > 10 or (digits and int(digits) > 2147483647):
                    raise ValueError("Ninja -j exceeds the supported integer range")
                requested = int(digits or "0")
                jobs = min(requested, 2) if requested else 2
            else:
                result.extend(["-" + option, value])
                if option == "t":
                    result.extend(iterator)
                    return ["-j", str(jobs), *result]
            break  # An operand consumes the entire remaining cluster.
    return ["-j", str(jobs), *result]


def exec_ninja(arguments, executable="/usr/bin/ninja"):
    os.execv(executable, [executable, *bounded_arguments(arguments)])


if __name__ == "__main__":
    try:
        exec_ninja(sys.argv[1:])
    except ValueError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
