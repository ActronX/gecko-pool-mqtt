"""Logging facade for Gecko transport recovery diagnostics."""

from __future__ import annotations

import logging
import re


logger = logging.getLogger(__name__)
_URL_PATTERN = re.compile(r"(?:https?|wss?|mqtts?)://[^\s'\"]+")
_MAX_ERROR_LENGTH = 240


def redact_error(error: Exception | str) -> str:
    """Limit exception diagnostics and remove credential-bearing URLs."""
    return _URL_PATTERN.sub("<redacted-url>", str(error)[:_MAX_ERROR_LENGTH])


class GeckoRecoveryLogger:
    """Emit searchable recovery diagnostics without owning recovery state."""

    def transition(
        self,
        generation: int,
        source: str,
        target: str,
        *,
        attempt: int | None = None,
        delay: float | None = None,
        reason: str | Exception | None = None,
        level: int = logging.INFO,
    ) -> None:
        message = (
            f"Gecko recovery state transition generation={generation} "
            f"from={source} to={target}"
        )
        if attempt is not None:
            message += f" attempt={attempt}"
        if delay is not None:
            message += f" delay={delay:.2f}s"
        if reason is not None:
            message += f" reason={redact_error(reason)}"
        logger.log(level, message)

    def scheduled(self, generation: int, connected: bool, delay: float) -> None:
        logger.warning(
            "Gecko recovery scheduled generation=%s connected=%s delay=%.2fs",
            generation,
            connected,
            delay,
        )

    def waiting(self, generation: int, attempt: int, delay: float) -> None:
        logger.info(
            "Gecko recovery waiting generation=%s attempt=%s delay=%.2fs",
            generation,
            attempt,
            delay,
        )

    def rebuild_attempt(self, generation: int, attempt: int) -> None:
        logger.warning(
            "Gecko recovery rebuild attempt=%s generation=%s",
            attempt,
            generation,
        )

    def rebuild_succeeded(self, generation: int) -> None:
        logger.info(
            "Gecko recovery rebuild setup succeeded generation=%s; "
            "normal connect worker now owns connect attempts",
            generation,
        )

    def connect_succeeded(self, generation: int) -> None:
        logger.info("Gecko recovery connect succeeded generation=%s", generation)

    def connect_failed(self, generation: int, attempt: int, error: Exception, delay: float) -> None:
        logger.error(
            "Gecko recovery connect failed generation=%s attempt=%s: %s; retrying in %.0fs",
            generation,
            attempt,
            redact_error(error),
            delay,
        )

    def rebuild_failed(
        self,
        generation: int,
        attempt: int,
        error: Exception,
        next_delay: float,
    ) -> None:
        logger.error(
            "Gecko recovery rebuild failed attempt=%s generation=%s: %s; "
            "next attempt in %.2fs",
            attempt,
            generation,
            redact_error(error),
            next_delay,
        )

    def aborted(
        self,
        reason: str,
        generation: int | None = None,
        error: Exception | None = None,
        level: int = logging.INFO,
    ) -> None:
        message = f"Gecko recovery aborted: {reason}"
        if generation is not None:
            message += f" generation={generation}"
        if error is not None:
            message += f" error={redact_error(error)}"
        logger.log(level, message)

    def already_scheduled(self, generation: int) -> None:
        logger.debug("Gecko recovery already scheduled for generation=%s", generation)


recovery_logger = GeckoRecoveryLogger()
