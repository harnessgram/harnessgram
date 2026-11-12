"""The radio's jitter buffer: tags reach the ingest at their own pace even when the encoder emits them in bursts."""
import os
import threading
import time

from ctb import radio_rtmp

HEADER = b"FLV\x01\x05\x00\x00\x00\x09\x00\x00\x00\x00"


def tag(kind, timestamp, payload=b"abc"):
    head = bytes([kind]) + len(payload).to_bytes(3, "big") + (timestamp & 0xffffff).to_bytes(3, "big")
    head += bytes([timestamp >> 24]) + b"\x00\x00\x00"
    return head + payload + (11 + len(payload)).to_bytes(4, "big")


class BurstyEncoder:
    """A pipe that yields the header and first tags at once, stalls, then delivers the rest in one burst."""
    def __init__(self, first: bytes, rest: bytes, stall: float):
        self.parts = [first, rest]
        self.stall = stall
        self.buf = b""
        self.started = False

    def read(self, n):
        if not self.buf:
            if not self.parts:
                return b""
            if self.started:
                time.sleep(self.stall)
            self.started = True
            self.buf = self.parts.pop(0)
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


def publisher(lead):
    pub = radio_rtmp.Publisher("rtmp://example.invalid/live", "key")
    pub.lead = lead
    sent = []
    pub.message = lambda csid, kind, data, ts, stream: sent.append((time.monotonic(), kind, ts))
    return pub, sent


def test_paced_sends_follow_timestamps_through_an_encoder_stall():
    # 0-100 ms of media arrive at once, then a 400 ms stall, then 120-400 ms in one burst.
    first = HEADER + b"".join(tag(8, t) for t in range(0, 101, 20))
    rest = b"".join(tag(8, t) for t in range(120, 401, 20))
    pub, sent = publisher(0.6)
    t0 = time.monotonic()
    pub.send_flv(BurstyEncoder(first, rest, 0.4), threading.Event())
    assert [ts for _, _, ts in sent] == list(range(0, 401, 20))
    # Never early: each tag leaves no sooner than start + lead + its timestamp (timers may run late on a busy
    # or throttled test runner, never early), and the burst is spread over the media's own 400 ms.
    for at, _, ts in sent:
        assert at - t0 >= 0.6 + ts / 1000.0 - 0.005, (ts, at - t0)
    assert sent[-1][0] - t0 >= 0.6 + 0.4 - 0.005
    assert pub.late_resyncs == 0


def test_no_lead_keeps_the_immediate_forwarding():
    pub, sent = publisher(0.0)
    data = HEADER + tag(8, 0) + tag(9, 1000)
    import io
    t0 = time.monotonic()
    pub.send_flv(io.BytesIO(data))
    assert [ts for _, _, ts in sent] == [0, 1000]
    assert time.monotonic() - t0 < 0.2  # nothing waits for timestamps


def test_audio_and_video_order_is_kept():
    import io
    pub, sent = publisher(0.05)
    data = HEADER + tag(9, 0) + tag(8, 0) + tag(8, 21) + tag(9, 33) + tag(8, 42)
    pub.send_flv(io.BytesIO(data), threading.Event())
    assert [(k, ts) for _, k, ts in sent] == [(9, 0), (8, 0), (8, 21), (9, 33), (8, 42)]


def test_stop_ends_a_paced_send_promptly():
    import io
    pub, sent = publisher(5.0)
    stop = threading.Event()
    t = threading.Thread(target=pub.send_flv, args=(io.BytesIO(HEADER + tag(8, 0) + tag(8, 60000)), stop))
    t.start()
    time.sleep(0.1)
    stop.set()
    t.join(2)
    assert not t.is_alive() and sent == []


def test_far_behind_encoder_restarts_the_timeline_instead_of_bursting():
    import io
    pub, sent = publisher(0.05)
    clock = [100.0]
    pub.clock = lambda: clock[0]
    real_message = pub.message

    def message(*args):
        real_message(*args)
        clock[0] += 5.0  # each send finds the encoder five seconds further behind
    pub.message = message
    pub.send_flv(io.BytesIO(HEADER + tag(8, 0) + tag(8, 20) + tag(8, 40)), threading.Event())
    assert pub.late_resyncs >= 1 and len(sent) == 3


def test_lead_setting_is_clamped(monkeypatch):
    monkeypatch.setenv("CTB_RADIO_LEAD_SECONDS", "99")
    assert radio_rtmp.lead_seconds() == 5.0
    monkeypatch.setenv("CTB_RADIO_LEAD_SECONDS", "-1")
    assert radio_rtmp.lead_seconds() == 0.0
    monkeypatch.setenv("CTB_RADIO_LEAD_SECONDS", "x")
    assert radio_rtmp.lead_seconds() == radio_rtmp.LEAD_SECONDS
    monkeypatch.delenv("CTB_RADIO_LEAD_SECONDS")
    assert radio_rtmp.lead_seconds() == 1.5


def test_tap_copies_the_outgoing_stream_privately(tmp_path, monkeypatch):
    import io
    import stat
    from ctb import radio
    monkeypatch.setattr(radio, "folder", lambda: tmp_path)
    monkeypatch.setenv("CTB_RADIO_TAP_SECONDS", "30")
    body = tag(8, 0) + tag(8, 21)
    pub, sent = publisher(0.05)
    pub.send_flv(io.BytesIO(HEADER + body), threading.Event())
    taps = list(tmp_path.glob("radio-tap-*.flv"))
    assert len(taps) == 1 and taps[0].read_bytes() == HEADER + body
    assert stat.S_IMODE(taps[0].stat().st_mode) == 0o600
    monkeypatch.delenv("CTB_RADIO_TAP_SECONDS")
    pub.send_flv(io.BytesIO(HEADER + body), threading.Event())
    assert len(list(tmp_path.glob("radio-tap-*.flv"))) == 1  # off by default


def test_only_the_newest_tap_files_are_kept(tmp_path, monkeypatch):
    from ctb import radio, radio_rtmp
    monkeypatch.setattr(radio, "folder", lambda: tmp_path)
    monkeypatch.setenv("CTB_RADIO_TAP_SECONDS", "5")
    for i in range(6):
        f = tmp_path / ("radio-tap-%d.flv" % (1000 + i))
        f.write_bytes(b"x")
        os.utime(f, (1000 + i, 1000 + i))
    tap = radio_rtmp._open_tap()
    assert tap is not None
    tap[1].close()
    assert len(list(tmp_path.glob("radio-tap-*.flv"))) == radio_rtmp.TAP_KEEP


def test_the_stream_carries_a_steady_frame_rate_with_two_second_keyframes(monkeypatch):
    from ctb import radio
    monkeypatch.delenv("CTB_RADIO_OUT_FPS", raising=False)
    args = radio.encoder_args("libx264")
    assert "fps=15,format=yuv420p" in args and args[args.index("-g") + 1] == "30"
    monkeypatch.setenv("CTB_RADIO_OUT_FPS", "99")
    assert radio.out_fps() == 30
