from __future__ import annotations

import resource
import sys

import regex

MEMORY_MARGIN_BYTES = 32 * 1024 * 1024
CPU_LIMIT_SECONDS = 2
EXIT_OK = 0
EXIT_INVALID = 3
EXIT_TOO_COMPLEX = 4
COMPILE_FLAGS = regex.IGNORECASE


def current_memory_bytes() -> int:
    with open("/proc/self/statm", encoding="ascii") as statm:
        return int(statm.read().split()[0]) * resource.getpagesize()


def main() -> int:
    pattern = sys.stdin.read()
    limit = current_memory_bytes() + MEMORY_MARGIN_BYTES
    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    resource.setrlimit(
        resource.RLIMIT_CPU, (CPU_LIMIT_SECONDS, CPU_LIMIT_SECONDS)
    )
    try:
        regex.compile(pattern, COMPILE_FLAGS)
    except regex.error as exc:
        print(exc)
        return EXIT_INVALID
    except (MemoryError, RecursionError, OverflowError):
        return EXIT_TOO_COMPLEX
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
