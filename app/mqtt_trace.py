"""Trace local MQTT messages as a JSONL file.

The trace is a passive observer. It attaches to `MqttBridge.publish` and
`MqttBridge._on_message` and does not alter the flow. Two requirements
determine the implementation:

* The write operation must never affect the MQTT path. Errors are swallowed so
  a full or unwritable filesystem affects neither `publish` nor the event
  loop.
* Writes come from multiple threads: `_on_message` runs in the paho thread,
  `publish` in the event-loop thread. Therefore a lock surrounds the write.

The file is not synced to disk after flushing. This is enough for `tail -f` to
show something immediately and keeps the write operation short on the event
loop.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, TextIO

logger = logging.getLogger(__name__)

#: Topic whose payload carries the OAuth code from the redirect. Although the
#: code is immediately invalidated, it should not be stored in plain text in a
#: file shared for analysis.
REDACTED_SUFFIX = "auth/response"
REDACTED_PLACEHOLDER = "<redacted>"

#: File name for each UTC day.
FILE_PATTERN = re.compile(r"^mqtt-(\d{4}-\d{2}-\d{2})\.jsonl$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class MqttTrace:
    """Write each MQTT message as a JSON line to a daily file.

    `directory` disables the trace when empty or ``off``.
    `retention_days` deletes older files when creating a file and on each day
    change; ``0`` keeps everything. `now` is an injection point for tests that
    need to trigger a day change without waiting.
    """

    def __init__(
        self,
        directory: str | None,
        retention_days: int = 7,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._now = now or _utc_now
        self._retention_days = retention_days
        self._lock = threading.Lock()
        self._handle: TextIO | None = None
        self._day: str | None = None
        self._warned = False
        self._enabled = bool(directory) and str(directory).strip().lower() != "off"
        self._directory = str(directory) if self._enabled else ""
        if self._enabled:
            try:
                os.makedirs(self._directory, exist_ok=True)
            except OSError as exc:
                self._fail("cannot create directory %s: %s", self._directory, exc)

    # -- Recording ---------------------------------------------------------

    def record(
        self,
        direction: str,
        topic: str,
        payload: str,
        *,
        qos: int | None = None,
        retain: bool | None = None,
    ) -> None:
        """Write a message. Never expose an exception to the caller."""
        if not self._enabled:
            return
        entry: dict[str, Any] = {
            "ts": self._now().isoformat(),
            "dir": direction,
            "topic": topic,
        }
        if topic.endswith(REDACTED_SUFFIX):
            entry["bytes"] = len(payload.encode("utf-8", errors="replace"))
            entry["payload"] = REDACTED_PLACEHOLDER
        else:
            entry["payload"] = payload
            if direction == "out":
                entry["qos"] = qos
                entry["retain"] = retain
        with self._lock:
            self._write(json.dumps(entry, separators=(",", ":")))

    def close(self) -> None:
        """Close the daily file. Closing multiple times is allowed."""
        with self._lock:
            handle, self._handle = self._handle, None
            self._day = None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass

    # -- File access -------------------------------------------------------

    def _write(self, line: str) -> None:
        try:
            moment = self._now()
            if self._handle is None or self._day != moment.date().isoformat():
                self._roll(moment)
            if self._handle is None:
                return
            self._handle.write(line + "\n")
            self._handle.flush()
        except OSError as exc:
            self._fail("cannot write to %s: %s", self._directory, exc)

    def _roll(self, moment: datetime) -> None:
        """Switch to the daily file for `moment`."""
        if self._handle is not None:
            try:
                self._handle.close()
            except OSError as exc:
                self._fail("cannot close the daily file: %s", exc)
        self._prune(moment)
        day = moment.date().isoformat()
        try:
            self._handle = open(
                os.path.join(self._directory, f"mqtt-{day}.jsonl"),
                "a",
                encoding="utf-8",
                newline="\n",
            )
            self._day = day
        except OSError as exc:
            self._handle = None
            self._day = None
            self._fail("cannot open the daily file for %s: %s", day, exc)

    def _prune(self, moment: datetime) -> None:
        """Delete daily files older than the retention period."""
        if self._retention_days <= 0:
            return
        try:
            names = os.listdir(self._directory)
        except OSError as exc:
            self._fail("cannot read directory %s: %s", self._directory, exc)
            return
        limit = (moment - timedelta(days=self._retention_days)).date()
        for name in names:
            match = FILE_PATTERN.match(name)
            if match is None:
                continue
            try:
                stamp = date.fromisoformat(match.group(1))
            except ValueError:
                continue
            if stamp < limit:
                try:
                    os.remove(os.path.join(self._directory, name))
                except OSError as exc:
                    self._fail("cannot delete the old daily file %s: %s", name, exc)

    def _fail(self, message: str, *args: Any) -> None:
        """Warn once so an error does not flood the log."""
        if self._warned:
            return
        self._warned = True
        logger.warning("MQTT trace disabled: " + message, *args)
