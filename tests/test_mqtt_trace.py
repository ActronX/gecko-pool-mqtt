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


class Uhr:
    """Injected clock that the test advances as needed."""

    def __init__(self, start: datetime) -> None:
        self.jetzt = start

    def __call__(self) -> datetime:
        return self.jetzt

    def springe(self, tage: int = 0, stunden: int = 0) -> None:
        self.jetzt += timedelta(days=tage, hours=stunden)


def trace_in(tmp_path: Path, uhr: Uhr | None = None, retention_days: int = 7) -> tuple[MqttTrace, Path]:
    """Creates a trace in the temporary directory and returns it with its path."""
    ziel = tmp_path / "mqtt-trace"
    return MqttTrace(str(ziel), retention_days, now=uhr or Uhr(TAG)), ziel


def zeilen(ziel: Path) -> list[dict[str, Any]]:
    """Reads all daily files and returns entries in storage order."""
    eintraege: list[dict[str, Any]] = []
    for datei in sorted(ziel.glob("mqtt-*.jsonl")):
        for zeile in datei.read_text(encoding="utf-8").splitlines():
            if zeile.strip():
                eintraege.append(json.loads(zeile))
    return eintraege


# --------------------------------------------------------------------------
# R-TR-01-01 Record format
# --------------------------------------------------------------------------


def test_tc_tr_01_01_outgoing_record_carries_qos_and_retain(tmp_path: Path) -> None:
    """R-TR-01: Outgoing messages carry dir, topic, payload, qos, and retain."""
    trace, ziel = trace_in(tmp_path)

    trace.record("out", "geeko/status/heatPump/state", '{"state":"running"}', qos=1, retain=True)

    eintrag = zeilen(ziel)[0]
    assert eintrag["dir"] == "out"
    assert eintrag["topic"] == "geeko/status/heatPump/state"
    assert eintrag["payload"] == '{"state":"running"}'
    assert eintrag["qos"] == 1
    assert eintrag["retain"] is True
    assert eintrag["ts"].endswith("+00:00")


def test_tc_tr_01_02_incoming_record_has_no_qos_or_retain(tmp_path: Path) -> None:
    """R-TR-01: Incoming messages have no qos or retain."""
    trace, ziel = trace_in(tmp_path)

    trace.record("in", "geeko/cmd/heatPump", '{"action":"on"}')

    eintrag = zeilen(ziel)[0]
    assert eintrag["dir"] == "in"
    assert eintrag["topic"] == "geeko/cmd/heatPump"
    assert eintrag["payload"] == '{"action":"on"}'
    assert "qos" not in eintrag
    assert "retain" not in eintrag


def test_tc_tr_01_03_timestamp_is_utc(tmp_path: Path) -> None:
    """R-TR-01: The timestamp includes the UTC offset."""
    trace, ziel = trace_in(tmp_path)

    trace.record("out", "geeko/status/availability", "online")

    assert zeilen(ziel)[0]["ts"] == "2026-09-30T11:40:23.934774+00:00"


def test_tc_tr_01_04_one_line_per_message_keeps_order(tmp_path: Path) -> None:
    """R-TR-01: Each message occupies one line and order is preserved."""
    trace, ziel = trace_in(tmp_path)

    trace.record("in", "geeko/cmd/heatPump", '{"action":"on"}')
    trace.record("out", "geeko/cmd/heatPump/result", '{"success":true}', qos=1, retain=False)
    trace.record("out", "geeko/status/heatPump/state", '{"state":"running"}', qos=1, retain=True)

    datei = ziel / "mqtt-2026-09-30.jsonl"
    roh = datei.read_text(encoding="utf-8").splitlines()
    assert len(roh) == 3
    assert [eintrag["topic"] for eintrag in zeilen(ziel)] == [
        "geeko/cmd/heatPump",
        "geeko/cmd/heatPump/result",
        "geeko/status/heatPump/state",
    ]


def test_tc_tr_01_05_empty_payload_is_kept(tmp_path: Path) -> None:
    """R-TR-01: An empty payload is recorded as an empty payload."""
    trace, ziel = trace_in(tmp_path)

    trace.record("in", "geeko/auth/login", "")

    assert zeilen(ziel)[0]["payload"] == ""


# --------------------------------------------------------------------------
# R-TR-01-03 Day change
# --------------------------------------------------------------------------


def test_tc_tr_01_06_day_change_opens_a_new_file(tmp_path: Path) -> None:
    """R-TR-02: After midnight UTC, the next line goes into the new file."""
    uhr = Uhr(TAG)
    trace, ziel = trace_in(tmp_path, uhr)

    trace.record("out", "geeko/status/heatPump/state", "a", qos=1, retain=True)
    uhr.springe(tage=1)
    trace.record("out", "geeko/status/heatPump/state", "b", qos=1, retain=True)

    assert (ziel / "mqtt-2026-09-30.jsonl").read_text(encoding="utf-8").count("\n") == 1
    assert (ziel / "mqtt-2026-10-01.jsonl").read_text(encoding="utf-8").count("\n") == 1
    assert [eintrag["payload"] for eintrag in zeilen(ziel)] == ["a", "b"]


def test_tc_tr_01_07_day_file_follows_the_utc_date(tmp_path: Path) -> None:
    """R-TR-02: The daily file follows the clock's UTC date."""
    uhr = Uhr(datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc))
    trace, ziel = trace_in(tmp_path, uhr)

    trace.record("out", "geeko/status/heatPump/state", "a", qos=1, retain=True)

    assert (ziel / "mqtt-2026-09-30.jsonl").exists()


# --------------------------------------------------------------------------
# R-TR-01-04 Retention
# --------------------------------------------------------------------------


def test_tc_tr_01_08_retention_removes_files_beyond_the_window(tmp_path: Path) -> None:
    """R-TR-03: Files older than retention are deleted."""
    uhr = Uhr(TAG)
    trace, ziel = trace_in(tmp_path, uhr, retention_days=7)
    ziel.mkdir(parents=True, exist_ok=True)
    for name in (
        "mqtt-2026-09-01.jsonl",
        "mqtt-2026-09-22.jsonl",
        "mqtt-2026-09-23.jsonl",
        "mqtt-2026-09-30.jsonl",
        "notiz.txt",
    ):
        (ziel / name).write_text("{}\n", encoding="utf-8")

    trace.record("out", "geeko/status/availability", "online")

    assert sorted(datei.name for datei in ziel.iterdir()) == [
        "mqtt-2026-09-23.jsonl",
        "mqtt-2026-09-30.jsonl",
        "notiz.txt",
    ]


def test_tc_tr_01_09_retention_runs_on_every_day_change(tmp_path: Path) -> None:
    """R-TR-03: Retention also applies on a day change."""
    uhr = Uhr(TAG)
    trace, ziel = trace_in(tmp_path, uhr, retention_days=1)
    ziel.mkdir(parents=True, exist_ok=True)
    (ziel / "mqtt-2026-09-29.jsonl").write_text("{}\n", encoding="utf-8")

    trace.record("out", "geeko/status/availability", "online")
    assert (ziel / "mqtt-2026-09-29.jsonl").exists()

    uhr.springe(tage=1)
    trace.record("out", "geeko/status/availability", "online")

    assert not (ziel / "mqtt-2026-09-29.jsonl").exists()


def test_tc_tr_01_10_zero_retention_keeps_every_file(tmp_path: Path) -> None:
    """R-TR-03: Retention 0 keeps all daily files."""
    uhr = Uhr(TAG)
    trace, ziel = trace_in(tmp_path, uhr, retention_days=0)
    ziel.mkdir(parents=True, exist_ok=True)
    (ziel / "mqtt-2020-01-01.jsonl").write_text("{}\n", encoding="utf-8")

    trace.record("out", "geeko/status/availability", "online")
    uhr.springe(tage=1)
    trace.record("out", "geeko/status/availability", "online")

    assert (ziel / "mqtt-2020-01-01.jsonl").exists()
    assert len(list(ziel.glob("mqtt-*.jsonl"))) == 3


# --------------------------------------------------------------------------
# R-TR-01-05 Disable
# --------------------------------------------------------------------------


@pytest.mark.parametrize("wert", ["", "   ", "off", "OFF", "Off"])
def test_tc_tr_01_11_disabled_writes_nothing(tmp_path: Path, wert: str) -> None:
    """R-TR-04: Empty or off disables the trace without creating a file."""
    ziel = tmp_path / "mqtt-trace"
    trace = MqttTrace(wert, 7, now=Uhr(TAG))

    trace.record("out", "geeko/status/availability", "online", qos=1, retain=True)
    trace.close()

    assert not ziel.exists()


def test_tc_tr_01_12_none_disables_the_trace(tmp_path: Path) -> None:
    """R-TR-04: None also disables the trace."""
    trace = MqttTrace(None, 7, now=Uhr(TAG))

    trace.record("out", "geeko/status/availability", "online", qos=1, retain=True)
    trace.close()

    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------
# R-TR-01-06 Redaction
# --------------------------------------------------------------------------


def test_tc_tr_01_13_auth_response_payload_is_redacted(tmp_path: Path) -> None:
    """R-TR-05: The OAuth code from auth/response is not stored in plain text."""
    trace, ziel = trace_in(tmp_path)

    trace.record("in", "geeko/auth/response", "https://app.test/redirect?code=SECRET&state=abc")

    eintrag = zeilen(ziel)[0]
    assert eintrag["topic"] == "geeko/auth/response"
    assert eintrag["payload"] == "<redacted>"
    assert eintrag["bytes"] == len("https://app.test/redirect?code=SECRET&state=abc")
    assert "SECRET" not in (ziel / "mqtt-2026-09-30.jsonl").read_text(encoding="utf-8")


def test_tc_tr_01_14_other_topics_stay_readable(tmp_path: Path) -> None:
    """R-TR-05: Only auth/response is redacted."""
    trace, ziel = trace_in(tmp_path)

    trace.record("out", "geeko/auth/challenge", '{"authorize_url":"https://x.test/a?state=s1"}')
    trace.record("out", "geeko/status/heatPump/reassert", '{"reason":"zone reported inactive"}')

    nutzlasten = [eintrag["payload"] for eintrag in zeilen(ziel)]
    assert "state=s1" in nutzlasten[0]
    assert "zone reported inactive" in nutzlasten[1]
    assert all("bytes" not in eintrag for eintrag in zeilen(ziel))


# --------------------------------------------------------------------------
# R-TR-01-07 Errors
# --------------------------------------------------------------------------


def test_tc_tr_01_15_write_error_never_raises(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """R-TR-06: An uncreatable directory absorbs the error."""
    blockierer = tmp_path / "blocker"
    blockierer.write_text("not a directory\n", encoding="utf-8")
    trace = MqttTrace(str(blockierer / "mqtt-trace"), 7, now=Uhr(TAG))

    with caplog.at_level("WARNING"):
        trace.record("out", "geeko/status/availability", "online", qos=1, retain=True)
        trace.record("in", "geeko/cmd/heatPump", '{"action":"on"}')
    trace.close()

    assert blockierer.read_text(encoding="utf-8") == "not a directory\n"
    warnungen = [eintrag for eintrag in caplog.records if eintrag.levelname == "WARNING"]
    assert len(warnungen) == 1, "The error was reported multiple times"


def test_tc_tr_01_16_unwritable_file_does_not_break_publishing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """R-TR-06: An existing file that cannot be appended to does not block `record`.

    Directory creation can succeed at startup while appending later fails, for
    example when the disk is full.
    """
    ziel = tmp_path / "mqtt-trace"
    ziel.mkdir(parents=True, exist_ok=True)
    (ziel / "mqtt-2026-09-30.jsonl").mkdir()
    trace = MqttTrace(str(ziel), 7, now=Uhr(TAG))

    with caplog.at_level("WARNING"):
        trace.record("out", "geeko/status/availability", "online", qos=1, retain=True)

    assert (ziel / "mqtt-2026-09-30.jsonl").is_dir()


def test_tc_tr_01_17_record_after_close_reopens_the_file(tmp_path: Path) -> None:
    """R-TR-02: After `close`, the trace writes to the daily file again."""
    trace, ziel = trace_in(tmp_path)

    trace.record("out", "geeko/status/availability", "online")
    trace.close()
    trace.record("out", "geeko/status/availability", "online")
    trace.close()

    assert len(zeilen(ziel)) == 2


def test_tc_tr_01_18_close_is_idempotent(tmp_path: Path) -> None:
    """R-TR-02: Repeated `close` is allowed and writes nothing extra."""
    trace, ziel = trace_in(tmp_path)

    trace.record("out", "geeko/status/availability", "online")
    trace.close()
    trace.close()

    assert len(zeilen(ziel)) == 1


# --------------------------------------------------------------------------
# R-TR-01-08 Concurrency
# --------------------------------------------------------------------------


def test_tc_tr_01_19_concurrent_records_produce_intact_lines(tmp_path: Path) -> None:
    """R-TR-07: Concurrent calls do not split a line.

    `_on_message` runs in the paho thread, and `publish` runs in the event-loop thread.
    """
    trace, ziel = trace_in(tmp_path)
    spur: list[threading.Thread] = []
    erwartet: list[str] = []
    for nummer in range(4):
        erwartet.extend(f'{{"n":{nummer * 100 + index}}}' for index in range(25))

        def schreibe(nummer: int = nummer) -> None:
            for index in range(25):
                trace.record("out", "geeko/status/heatPump/state", f'{{"n":{nummer * 100 + index}}}', qos=1, retain=True)

        thread = threading.Thread(target=schreibe)
        spur.append(thread)
        thread.start()
    for thread in spur:
        thread.join()

    datei = ziel / "mqtt-2026-09-30.jsonl"
    roh = datei.read_text(encoding="utf-8").splitlines()
    assert len(roh) == 100
    assert all(isinstance(json.loads(zeile), dict) for zeile in roh)
    assert sorted(json.loads(zeile)["payload"] for zeile in roh) == sorted(erwartet)


def test_tc_tr_01_20_directory_is_created_on_demand(tmp_path: Path) -> None:
    """R-TR-07: The target directory is created on initialization."""
    ziel = tmp_path / "tief" / "mqtt-trace"

    trace = MqttTrace(str(ziel), 7, now=Uhr(TAG))
    trace.record("out", "geeko/status/availability", "online")
    trace.close()

    assert os.path.isdir(ziel)
