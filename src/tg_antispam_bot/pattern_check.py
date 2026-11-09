from __future__ import annotations

import asyncio
import logging
import sys

from .config import (
    MAX_PATTERN_LENGTH,
    PATTERN_DETECTORS,
    ConfigError,
    RulesFile,
    parse_chat_rules,
)
from .pattern_probe import EXIT_INVALID, EXIT_OK, EXIT_TOO_COMPLEX

logger = logging.getLogger(__name__)

FILE_CHECK_CONCURRENCY = 4
PROBE_TIME_LIMIT_SECONDS = 5
TOO_COMPLEX = "that regex is too complex"
CHECK_FAILED = "could not check that regex, see the bot's log"
ERROR_TAIL_CHARS = 500


async def check_user_pattern(pattern: str) -> str | None:
    if not pattern:
        return "pattern is empty"
    if len(pattern) > MAX_PATTERN_LENGTH:
        return f"pattern is longer than {MAX_PATTERN_LENGTH} characters"
    return await probe_pattern(pattern)


async def check_file_patterns(rules_file: RulesFile) -> None:
    patterns: set[str] = set()
    for chat_id in (None, *rules_file.chats):
        document = rules_file.base_document(chat_id)
        detectors = parse_chat_rules(document).detectors
        for name in PATTERN_DETECTORS:
            patterns.update(getattr(detectors, name).patterns)
    limit = asyncio.Semaphore(FILE_CHECK_CONCURRENCY)

    async def check(pattern: str) -> tuple[str, str | None]:
        async with limit:
            return pattern, await probe_pattern(pattern)

    for pattern, problem in await asyncio.gather(*map(check, sorted(patterns))):
        if problem is not None:
            raise ConfigError(f"pattern {pattern!r} in rules file: {problem}")


async def probe_pattern(pattern: str) -> str | None:
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-P",
            "-m",
            "tg_antispam_bot.pattern_probe",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={},
        )
    except OSError as exc:
        logger.error("pattern probe failed to start", extra={"error": str(exc)})
        return CHECK_FAILED
    try:
        output, errors = await asyncio.wait_for(
            process.communicate(pattern.encode()), PROBE_TIME_LIMIT_SECONDS
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        return TOO_COMPLEX
    if process.returncode == EXIT_OK:
        return None
    if process.returncode == EXIT_INVALID:
        return f"invalid regex: {output.decode(errors='replace').strip()}"
    if process.returncode == EXIT_TOO_COMPLEX or (process.returncode or 0) < 0:
        return TOO_COMPLEX
    logger.error(
        "pattern probe crashed",
        extra={
            "exit_code": process.returncode,
            "error": errors.decode(errors="replace")[-ERROR_TAIL_CHARS:],
        },
    )
    return CHECK_FAILED
