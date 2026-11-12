import io
import json
import stat
import threading
import time

from ctb import radio_diag, radio_rtmp
from ctb.cli import main


HEADER = b"FLV\x01\x05\x00\x00\x00\x09\x00\x00\x00\x00"


def tag(kind, timestamp, payload=b"abc"):
    head = bytes([kind]) + len(payload).to_bytes(3, "big") + (timestamp & 0xffffff).to_bytes(3, "big")
    head += bytes([timestamp >> 24]) + b"\x00\x00\x00"
    return head + payload + (11 + len(payload)).to_bytes(4, "big")


def publisher(diag=None):
    pub = radio_rtmp.Publisher("rtmp://example.invalid/live", "key")
    pub.diag = radio_diag.Guard(diag) if diag else None
    pub.message = lambda *args: None
    return pub


def test_capture_stream_window_and_permissions(tmp_path):
    capture = radio_diag.Capture(tmp_path, 0.1)
    original = HEADER + tag(18, 0) + tag(8, 23) + tag(9, 45)
    publisher(capture).send_flv(io.BytesIO(original))
    capture.lag("audio", 0.25, time.monotonic())
    capture.thread.join(2)
    assert not capture.thread.is_alive()
    paths = list(tmp_path.glob("radio-diag-*.json"))
    assert len(paths) == 1
    doc = json.loads(paths[0].read_text())
    assert [r["type"] for r in doc["rows"]] == ["script", "audio", "video", "writer"]
    assert all(r["send_end_monotonic"] >= r["send_start_monotonic"] for r in doc["rows"][:3])
    assert [r["size_bytes"] for r in doc["rows"][:3]] == [3, 3, 3]
    assert stat.S_IMODE(paths[0].stat().st_mode) == 0o600
    assert not list(tmp_path.glob("*.flv"))


def test_rows_are_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(radio_diag, "MAX_ROWS", 1)
    capture = radio_diag.Capture(tmp_path, 0.1)
    publisher(capture).send_flv(io.BytesIO(HEADER + tag(8, 0) + tag(8, 23)))
    capture.thread.join(2)
    doc = json.loads(next(tmp_path.glob("radio-diag-*.json")).read_text())
    assert len(doc["rows"]) == 1 and doc["rows_dropped"] == 1


def test_contended_capture_does_not_block_media(tmp_path):
    capture = radio_diag.Capture(tmp_path, 0.2)
    sent = []
    pub = publisher(capture)
    pub.message = lambda *args: sent.append(args)
    with capture.lock:
        thread = threading.Thread(target=pub.send_flv, args=(io.BytesIO(HEADER + tag(8, 0) + tag(9, 1)),))
        thread.start()
        thread.join(1)
        assert not thread.is_alive()
        assert len(sent) == 2
    capture.thread.join(2)


def test_connections_write_separate_files(tmp_path):
    first = radio_diag.Capture(tmp_path, 0.1)
    second = radio_diag.Capture(tmp_path, 0.1)
    first.lag("audio", 1, time.monotonic())
    second.lag("video", 2, time.monotonic())
    first.thread.join(2)
    second.thread.join(2)
    paths = list(tmp_path.glob("radio-diag-*.json"))
    assert len(paths) == 2
    assert {json.loads(p.read_text())["rows"][0]["writer"] for p in paths} == {"audio", "video"}


def test_raising_recorder_is_disabled_and_forwarding_continues():
    class Broken:
        def tag(self, *args):
            raise RuntimeError("private detail")
    pub = publisher(Broken())
    sent = []
    pub.message = lambda *args: sent.append(args)
    pub.send_flv(io.BytesIO(HEADER + tag(8, 0) + tag(9, 1)))
    assert len(sent) == 2 and pub.diag.capture is None
    logs = []
    pub.diag.log_error(logs.append)
    pub.diag.log_error(logs.append)
    assert logs == ["radio diagnostic disabled: RuntimeError"]


def test_off_path_does_not_call_diagnostic(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("diagnostics called while off")
    monkeypatch.setattr(radio_diag.Guard, "call", forbidden)
    pub = publisher()
    sent = []
    pub.message = lambda *args: sent.append(args)
    pub.send_flv(io.BytesIO(HEADER + tag(8, 0) + tag(9, 1)))
    assert len(sent) == 2


def test_unset_is_off(tmp_path, monkeypatch):
    monkeypatch.delenv("CTB_RADIO_DIAG_SECONDS", raising=False)
    assert radio_diag.configured_seconds() == 0
    publisher().send_flv(io.BytesIO(HEADER + tag(8, 0)))
    assert not list(tmp_path.glob("radio-diag.*"))
    monkeypatch.setenv("CTB_RADIO_DIAG_SECONDS", "500")
    assert radio_diag.configured_seconds() == 120
    monkeypatch.setenv("CTB_RADIO_DIAG_SECONDS", "bad")
    assert radio_diag.configured_seconds() == 0


def test_report_gap_burst_and_lag(tmp_path, capsys):
    rows = [{"type": "audio", "flv_timestamp_ms": ts, "read_monotonic": read,
             "send_start_monotonic": read, "send_end_monotonic": read + duration}
            for ts, read, duration in [(0, 10, 0.01), (60, 10.2, 0.2), (80, 10.201, 0.01)]]
    rows.append({"type": "writer", "writer": "audio", "lag_seconds": 0.25})
    path = tmp_path / "input.json"
    path.write_text(json.dumps({"rows": rows}))
    assert main(["radio", "diag-report", str(path)]) == 0
    result = capsys.readouterr().out
    assert "at 60 ms: 60 ms" in result
    assert "Max audio read gap: 200.0 ms (FLV gap 60 ms)" in result
    assert "Largest read burst: read gap 1.0 ms (FLV gap 20 ms)" in result
    assert "Send durations > 100 ms: 1" in result
    assert "Writer lag max (audio): 250.0 ms" in result


def test_writer_survives_diagnostic_exception(monkeypatch):
    from ctb import radio

    class Broken:
        def lag(self, *args):
            raise RuntimeError()

    failed = threading.Event()
    pipe = radio.Pipeline("rtmp://example.invalid/live", "key", None, "encoder",
                          radio.Mixer(bytes(4)), lambda pid: None)
    pipe.diag = radio_diag.Guard(Broken())
    writes = []

    class Stdin:
        def write(self, data):
            writes.append(len(data))

        def flush(self):
            failed.set()

    pipe.proc = type("Process", (), {"stdin": Stdin()})()
    pipe._writer(failed)
    assert writes and pipe.diag.capture is None


def test_failed_diagnostic_start_does_not_fail_connection(tmp_path, monkeypatch):
    from ctb import radio, policy
    monkeypatch.setattr(policy, "on", lambda: True)
    monkeypatch.setenv("CTB_RADIO_DIAG_SECONDS", "1")
    monkeypatch.setattr(radio, "folder", lambda: tmp_path)
    logs = []
    monkeypatch.setattr(radio, "log", logs.append)
    monkeypatch.setattr(radio_diag, "Capture", lambda *a: (_ for _ in ()).throw(RuntimeError()))
    connected = []

    class Publisher:
        def __init__(self, *args):
            pass
        def connect(self):
            connected.append(True)
        def close(self):
            pass

    monkeypatch.setattr(radio_rtmp, "Publisher", Publisher)
    monkeypatch.setattr(radio.subprocess, "Popen", lambda *a, **kw: (_ for _ in ()).throw(OSError()))
    pipe = radio.Pipeline("rtmp://example.invalid/live", "key", None, "encoder",
                          radio.Mixer(bytes(4)), lambda pid: None)
    assert pipe.run() == "pipeline error: OSError"
    assert connected and logs == ["radio diagnostic disabled: RuntimeError"]


def test_guard_turns_off_once_the_window_is_over(tmp_path):
    capture = radio_diag.Capture(tmp_path, 0.05)
    guard = radio_diag.Guard(capture)
    assert guard
    capture.thread.join(2)
    assert not guard  # the stream threads stop calling in after the window


def test_busy_skips_are_counted(tmp_path):
    capture = radio_diag.Capture(tmp_path, 0.1)
    with capture.lock:
        capture.lag("audio", 0.1, time.monotonic())
        capture.tag("audio", 0, time.monotonic(), 3)
    capture.thread.join(2)
    doc = json.loads(next(tmp_path.glob("radio-diag-*.json")).read_text())
    assert doc["rows_busy_dropped"] == 2 and doc["rows"] == []


def test_many_rows_are_written_in_chunks_as_valid_json(tmp_path, monkeypatch):
    monkeypatch.setattr(radio_diag, "CHUNK", 7)
    capture = radio_diag.Capture(tmp_path, 0.1)
    for i in range(50):
        capture.lag("audio", i / 1000, time.monotonic())
    capture.thread.join(2)
    doc = json.loads(next(tmp_path.glob("radio-diag-*.json")).read_text())
    assert len(doc["rows"]) == 50 and doc["version"] == 1


def test_only_the_newest_captures_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(radio_diag, "KEEP_FILES", 2)
    for _ in range(4):
        capture = radio_diag.Capture(tmp_path, 0.01)
        capture.thread.join(2)
        time.sleep(0.01)
    assert len(list(tmp_path.glob("radio-diag-*.json"))) == 2
