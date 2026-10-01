"""Requirements R-TR: trace of local MQTT messages (TC-TR-01-01 through TC-TR-01-20).

Covers record format, day changes, retention, redaction, and write errors. The
day change uses an injected clock so cases remain deterministic without waits.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from app.mqtt_trace import MqttTrace

TAG = datetime(2026, 9, 30, 11, 40, 23, 934774, tzinfo=timezone.utc)


class Clock:
    """Injected clock that the test advances as needed."""

    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, days: int = 0, hours: int = 0) -> None:
        self.now += timedelta(days=days, hours=hours)


def trace_in(tmp_path: Path, clock: Clock | None = None, retention_days: int = 7) -> tuple[MqttTrace, Path]:
    """Creates a trace in the temporary directory and returns it with its path."""
    target = tmp_path / "mqtt-trace"
    return MqttTrace(str(target), retention_days, now=clock or Clock(TAG)), target


def read_entries(target: Path) -> list[dict[str, Any]]:
    """Reads all daily files and returns entries in storage order."""
    entries: list[dict[str, Any]] = []
    for day_file in sorted(target.glob("mqtt-*.jsonl")):
        for line in day_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                entries.append(json.loads(line))
    return entries


# --------------------------------------------------------------------------
# R-TR-01-01 Record format
# --------------------------------------------------------------------------


def test_tc_tr_01_01_outgoing_record_carries_qos_and_retain(tmp_path: Path) -> None:
    """R-TR-01: Outgoing messages carry dir, topic, payload, qos, and retain."""
    trace, target = trace_in(tmp_path)

    trace.record("out", "gecko/status/heatPump/state", '{"state":"running"}', qos=1, retain=True)

    entry = read_entries(target)[0]
    assert entry["dir"] == "out"
    assert entry["topic"] == "gecko/status/heatPump/state"
    assert entry["payload"] == '{"state":"running"}'
    assert entry["qos"] == 1
    assert entry["retain"] is True
    assert entry["ts"].endswith("+00:00")


def test_tc_tr_01_02_incoming_record_has_no_qos_or_retain(tmp_path: Path) -> None:
    """R-TR-01: Incoming messages have no qos or retain."""
    trace, target = trace_in(tmp_path)

    trace.record("in", "gecko/cmd/heatPump", '{"action":"on"}')

    entry = read_entries(target)[0]
    assert entry["dir"] == "in"
    assert entry["topic"] == "gecko/cmd/heatPump"
    assert entry["payload"] == '{"action":"on"}'
    assert "qos" not in entry
    assert "retain" not in entry


def test_tc_tr_01_03_timestamp_is_utc(tmp_path: Path) -> None:
    """R-TR-01: The timestamp includes the UTC offset."""
    trace, target = trace_in(tmp_path)

    trace.record("out", "gecko/status/availability", "online")

    assert read_entries(target)[0]["ts"] == "2026-09-30T11:40:23.934774+00:00"


def test_tc_tr_01_04_one_line_per_message_keeps_order(tmp_path: Path) -> None:
    """R-TR-01: Each message occupies one line and order is preserved."""
    trace, target = trace_in(tmp_path)

    trace.record("in", "gecko/cmd/heatPump", '{"action":"on"}')
    trace.record("out", "gecko/cmd/heatPump/result", '{"success":true}', qos=1, retain=False)
    trace.record("out", "gecko/status/heatPump/state", '{"state":"running"}', qos=1, retain=True)

    day_file = target / "mqtt-2026-09-30.jsonl"
    raw = day_file.read_text(encoding="utf-8").splitlines()
    assert len(raw) == 3
    assert [entry["topic"] for entry in read_entries(target)] == [
        "gecko/cmd/heatPump",
        "gecko/cmd/heatPump/result",
        "gecko/status/heatPump/state",
    ]


def test_tc_tr_01_05_empty_payload_is_kept(tmp_path: Path) -> None:
    """R-TR-01: An empty payload is recorded as an empty payload."""
    trace, target = trace_in(tmp_path)

    trace.record("in", "gecko/auth/login", "")

    assert read_entries(target)[0]["payload"] == ""


# --------------------------------------------------------------------------
# R-TR-01-03 Day change
# --------------------------------------------------------------------------


def test_tc_tr_01_06_day_change_opens_a_new_file(tmp_path: Path) -> None:
    """R-TR-02: After midnight UTC, the next line goes into the new file."""
    clock = Clock(TAG)
    trace, target = trace_in(tmp_path, clock)

    trace.record("out", "gecko/status/heatPump/state", "a", qos=1, retain=True)
    clock.advance(days=1)
    trace.record("out", "gecko/status/heatPump/state", "b", qos=1, retain=True)

    assert (target / "mqtt-2026-09-30.jsonl").read_text(encoding="utf-8").count("\n") == 1
    assert (target / "mqtt-2026-10-01.jsonl").read_text(encoding="utf-8").count("\n") == 1
    assert [entry["payload"] for entry in read_entries(target)] == ["a", "b"]


def test_tc_tr_01_07_day_file_follows_the_utc_date(tmp_path: Path) -> None:
    """R-TR-02: The daily file follows the clock's UTC date."""
    clock = Clock(datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc))
    trace, target = trace_in(tmp_path, clock)

    trace.record("out", "gecko/status/heatPump/state", "a", qos=1, retain=True)

    assert (target / "mqtt-2026-09-30.jsonl").exists()


# --------------------------------------------------------------------------
# R-TR-01-04 Retention
# --------------------------------------------------------------------------


def test_tc_tr_01_08_retention_removes_files_beyond_the_window(tmp_path: Path) -> None:
    """R-TR-03: Files older than retention are deleted."""
    clock = Clock(TAG)
    trace, target = trace_in(tmp_path, clock, retention_days=7)
    target.mkdir(parents=True, exist_ok=True)
    for name in (
        "mqtt-2026-09-01.jsonl",
        "mqtt-2026-09-22.jsonl",
        "mqtt-2026-09-23.jsonl",
        "mqtt-2026-09-30.jsonl",
        "notiz.txt",
    ):
        (target / name).write_text("{}\n", encoding="utf-8")

    trace.record("out", "gecko/status/availability", "online")

    assert sorted(day_file.name for day_file in target.iterdir()) == [
        "mqtt-2026-09-23.jsonl",
        "mqtt-2026-09-30.jsonl",
        "notiz.txt",
    ]


def test_tc_tr_01_09_retention_runs_on_every_day_change(tmp_path: Path) -> None:
    """R-TR-03: Retention also applies on a day change."""
    clock = Clock(TAG)
    trace, target = trace_in(tmp_path, clock, retention_days=1)
    target.mkdir(parents=True, exist_ok=True)
    (target / "mqtt-2026-09-29.jsonl").write_text("{}\n", encoding="utf-8")

    trace.record("out", "gecko/status/availability", "online")
    assert (target / "mqtt-2026-09-29.jsonl").exists()

    clock.advance(days=1)
    trace.record("out", "gecko/status/availability", "online")

    assert not (target / "mqtt-2026-09-29.jsonl").exists()


def test_tc_tr_01_10_zero_retention_keeps_every_file(tmp_path: Path) -> None:
    """R-TR-03: Retention 0 keeps all daily files."""
    clock = Clock(TAG)
    trace, target = trace_in(tmp_path, clock, retention_days=0)
    target.mkdir(parents=True, exist_ok=True)
    (target / "mqtt-2020-01-01.jsonl").write_text("{}\n", encoding="utf-8")

    trace.record("out", "gecko/status/availability", "online")
    clock.advance(days=1)
    trace.record("out", "gecko/status/availability", "online")

    assert (target / "mqtt-2020-01-01.jsonl").exists()
    assert len(list(target.glob("mqtt-*.jsonl"))) == 3


# --------------------------------------------------------------------------
# R-TR-01-05 Disable
# --------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["", "   ", "off", "OFF", "Off"])
def test_tc_tr_01_11_disabled_writes_nothing(tmp_path: Path, value: str) -> None:
    """R-TR-04: Empty or off disables the trace without creating a file."""
    target = tmp_path / "mqtt-trace"
    trace = MqttTrace(value, 7, now=Clock(TAG))

    trace.record("out", "gecko/status/availability", "online", qos=1, retain=True)
    trace.close()

    assert not target.exists()


def test_tc_tr_01_12_none_disables_the_trace(tmp_path: Path) -> None:
    """R-TR-04: None also disables the trace."""
    trace = MqttTrace(None, 7, now=Clock(TAG))

    trace.record("out", "gecko/status/availability", "online", qos=1, retain=True)
    trace.close()

    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------
# R-TR-01-06 Redaction
# --------------------------------------------------------------------------


def test_tc_tr_01_13_auth_response_payload_is_redacted(tmp_path: Path) -> None:
    """R-TR-05: The OAuth code from auth/response is not stored in plain text."""
    trace, target = trace_in(tmp_path)

    trace.record("in", "gecko/auth/response", "https://app.test/redirect?code=SECRET&state=abc")

    entry = read_entries(target)[0]
    assert entry["topic"] == "gecko/auth/response"
    assert entry["payload"] == "<redacted>"
    assert entry["bytes"] == len("https://app.test/redirect?code=SECRET&state=abc")
    assert "SECRET" not in (target / "mqtt-2026-09-30.jsonl").read_text(encoding="utf-8")


def test_tc_tr_01_14_other_topics_stay_readable(tmp_path: Path) -> None:
    """R-TR-05: Only auth/response is redacted."""
    trace, target = trace_in(tmp_path)

    trace.record("out", "gecko/auth/challenge", '{"authorize_url":"https://x.test/a?state=s1"}')
    trace.record("out", "gecko/status/heatPump/reassert", '{"reason":"zone reported inactive"}')

    payloads = [entry["payload"] for entry in read_entries(target)]
    assert "state=s1" in payloads[0]
    assert "zone reported inactive" in payloads[1]
    assert all("bytes" not in entry for entry in read_entries(target))


# --------------------------------------------------------------------------
# R-TR-01-07 Errors
# --------------------------------------------------------------------------


def test_tc_tr_01_15_write_error_never_raises(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """R-TR-06: An uncreatable directory absorbs the error."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n", encoding="utf-8")
    trace = MqttTrace(str(blocker / "mqtt-trace"), 7, now=Clock(TAG))

    with caplog.at_level("WARNING"):
        trace.record("out", "gecko/status/availability", "online", qos=1, retain=True)
        trace.record("in", "gecko/cmd/heatPump", '{"action":"on"}')
    trace.close()

    assert blocker.read_text(encoding="utf-8") == "not a directory\n"
    warnungen = [entry for entry in caplog.records if entry.levelname == "WARNING"]
    assert len(warnungen) == 1, "The error was reported multiple times"


def test_tc_tr_01_16_unwritable_file_does_not_break_publishing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """R-TR-06: An existing file that cannot be appended to does not block `record`.

    Directory creation can succeed at startup while appending later fails, for
    example when the disk is full.
    """
    target = tmp_path / "mqtt-trace"
    target.mkdir(parents=True, exist_ok=True)
    (target / "mqtt-2026-09-30.jsonl").mkdir()
    trace = MqttTrace(str(target), 7, now=Clock(TAG))

    with caplog.at_level("WARNING"):
        trace.record("out", "gecko/status/availability", "online", qos=1, retain=True)

    assert (target / "mqtt-2026-09-30.jsonl").is_dir()


def test_tc_tr_01_17_record_after_close_reopens_the_file(tmp_path: Path) -> None:
    """R-TR-02: After `close`, the trace writes to the daily file again."""
    trace, target = trace_in(tmp_path)

    trace.record("out", "gecko/status/availability", "online")
    trace.close()
    trace.record("out", "gecko/status/availability", "online")
    trace.close()

    assert len(read_entries(target)) == 2


def test_tc_tr_01_18_close_is_idempotent(tmp_path: Path) -> None:
    """R-TR-02: Repeated `close` is allowed and writes nothing extra."""
    trace, target = trace_in(tmp_path)

    trace.record("out", "gecko/status/availability", "online")
    trace.close()
    trace.close()

    assert len(read_entries(target)) == 1


# --------------------------------------------------------------------------
# R-TR-01-08 Concurrency
# --------------------------------------------------------------------------


def test_tc_tr_01_19_concurrent_records_produce_intact_lines(tmp_path: Path) -> None:
    """R-TR-07: Concurrent calls do not split a line.

    `_on_message` runs in the paho thread, and `publish` runs in the event-loop thread.
    """
    trace, target = trace_in(tmp_path)
    track: list[threading.Thread] = []
    expected: list[str] = []
    for number in range(4):
        expected.extend(f'{{"n":{number * 100 + index}}}' for index in range(25))

        def write_records(number: int = number) -> None:
            for index in range(25):
                trace.record("out", "gecko/status/heatPump/state", f'{{"n":{number * 100 + index}}}', qos=1, retain=True)

        thread = threading.Thread(target=write_records)
        track.append(thread)
        thread.start()
    for thread in track:
        thread.join()

    day_file = target / "mqtt-2026-09-30.jsonl"
    raw = day_file.read_text(encoding="utf-8").splitlines()
    assert len(raw) == 100
    assert all(isinstance(json.loads(line), dict) for line in raw)
    assert sorted(json.loads(line)["payload"] for line in raw) == sorted(expected)


def test_tc_tr_01_20_directory_is_created_on_demand(tmp_path: Path) -> None:
    """R-TR-07: The target directory is created on initialization."""
    target = tmp_path / "deep" / "mqtt-trace"

    trace = MqttTrace(str(target), 7, now=Clock(TAG))
    trace.record("out", "gecko/status/availability", "online")
    trace.close()

    assert os.path.isdir(target)
