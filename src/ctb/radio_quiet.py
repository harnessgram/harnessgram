"""A short silent audio segment for the moment between pressing Play and the radio's first real audio.

AVPlayer (the iPhone app's HarnessgramRadioPlayer) and Safari can fail on a live playlist that lists no segment, so
while the radio is starting the listen link serves a live playlist of silent segments instead: `quiet-<n>.ts`,
2.005 seconds of AAC-LC 48 kHz stereo silence (94 frames), the same audio format as the radio's own segments. There is
no encoder, no file and no speech: the segment is built here in memory from a fixed silent frame, once per sequence
number (the result is cached), and its timestamps depend only on the number, so consecutive segments are contiguous.

The window slides with the clock (a new number every 2 seconds), so the player keeps pulling new segments until real
ones exist. `bridge` then rewrites the first real playlists so the player switches over with an EXT-X-DISCONTINUITY
(the real audio has its own timeline) and a matching discontinuity sequence.
"""
from __future__ import annotations

import functools
import re
from typing import Optional, Tuple

FRAMES = 94                      # ADTS frames per segment (94 * 1024 samples at 48 kHz = 2.00533 s)
FRAME_TICKS = 1920               # 90 kHz ticks per frame (1024 / 48000 s)
SEGMENT_SECONDS = 2.0            # how fast the sequence number moves; each segment lasts 2.005 s
WINDOW = 6                       # entries in the live window, as in the radio's own playlist
EXTINF = "2.005"
TS_PACKET = 188
PMT_PID, AUDIO_PID = 0x1000, 0x100
BASE_TICKS = 900000              # the timeline starts at 10 s, so PCR (a little earlier than PTS) is never negative
PCR_LEAD = 9000                  # PCR runs 0.1 s ahead of presentation
MOD33 = 1 << 33

QUIET = re.compile(r"quiet-([0-9]{1,20})\.ts")


class _Bits:
    def __init__(self) -> None:
        self.value, self.count = 0, 0

    def put(self, value: int, width: int) -> None:
        self.value = (self.value << width) | (value & ((1 << width) - 1))
        self.count += width

    def bytes(self) -> bytes:
        pad = -self.count % 8
        return (self.value << pad).to_bytes((self.count + pad) // 8, "big")


@functools.lru_cache(maxsize=1)
def silent_frame() -> bytes:
    """One ADTS frame of AAC-LC stereo silence: a channel pair element whose two channels have no scale factor bands
    (max_sfb = 0), so there is no spectral data at all."""
    b = _Bits()
    b.put(1, 3)        # id_syn_ele: ID_CPE
    b.put(0, 4)        # element_instance_tag
    b.put(0, 1)        # common_window = 0: each channel carries its own info
    for _ in range(2):
        b.put(0, 8)    # global_gain
        b.put(0, 1)    # ics_reserved_bit
        b.put(0, 2)    # window_sequence: ONLY_LONG_SEQUENCE
        b.put(0, 1)    # window_shape
        b.put(0, 6)    # max_sfb = 0
        b.put(0, 1)    # predictor_data_present
        b.put(0, 1)    # pulse_data_present
        b.put(0, 1)    # tns_data_present
        b.put(0, 1)    # gain_control_data_present
    b.put(7, 3)        # ID_END
    raw = b.bytes()
    length = 7 + len(raw)
    h = _Bits()
    h.put(0xFFF, 12)   # syncword
    h.put(0, 1)        # MPEG-4
    h.put(0, 2)        # layer
    h.put(1, 1)        # no CRC
    h.put(1, 2)        # profile: AAC-LC
    h.put(3, 4)        # 48000 Hz
    h.put(0, 1)        # private
    h.put(2, 3)        # channel configuration: stereo
    h.put(0, 4)        # original, home, copyright id and start
    h.put(length, 13)  # frame length
    h.put(0x7FF, 11)   # buffer fullness: variable
    h.put(0, 2)        # one raw data block
    return h.bytes() + raw


def _crc32(data: bytes) -> int:
    """MPEG-2 CRC-32 (polynomial 0x04C11DB7, start 0xFFFFFFFF, no reflection, no final xor)."""
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if crc & 0x80000000 else (crc << 1) & 0xFFFFFFFF
    return crc


def _packet(pid: int, cc: int, payload: bytes, start: bool, adaptation: bytes = b"") -> bytes:
    """One 188-byte TS packet. Short payloads are padded with stuffing in the adaptation field."""
    room = TS_PACKET - 4
    if adaptation or len(payload) < room:
        field = bytearray(adaptation)
        space = room - 1 - len(payload)          # bytes for the adaptation field after its length byte
        if not field:
            field = bytearray(b"\x00")           # flags byte only
        field.extend(b"\xff" * (space - len(field)))
        adaptation = bytes([space]) + bytes(field)
        control = 3
    else:
        adaptation, control = b"", 1
    head = bytes([0x47, (0x40 if start else 0) | (pid >> 8), pid & 0xFF, (control << 4) | (cc & 15)])
    return head + adaptation + payload


def _section(table_id: int, body_after_length: bytes) -> bytes:
    length = len(body_after_length) + 4
    section = bytes([table_id, 0xB0 | (length >> 8), length & 0xFF]) + body_after_length
    return b"\x00" + section + _crc32(section).to_bytes(4, "big")   # pointer_field, then the section with its CRC


def _pts(value: int, marker: int = 2) -> bytes:
    value %= MOD33
    return bytes([(marker << 4) | ((value >> 30) & 7) << 1 | 1, (value >> 22) & 0xFF, ((value >> 15) & 0x7F) << 1 | 1,
                  (value >> 7) & 0xFF, (value & 0x7F) << 1 | 1])


@functools.lru_cache(maxsize=16)
def segment(seq: int) -> bytes:
    """The silent segment for this sequence number: PAT, PMT, then 94 PES packets of one ADTS frame each."""
    frame = silent_frame()
    first = BASE_TICKS + seq * FRAMES * FRAME_TICKS
    pat = _section(0, bytes([0, 1, 0xC1, 0, 0, 0, 1, 0xE0 | (PMT_PID >> 8), PMT_PID & 0xFF]))
    pmt = _section(2, bytes([0, 1, 0xC1, 0, 0, 0xE0 | (AUDIO_PID >> 8), AUDIO_PID & 0xFF, 0xF0, 0,
                             0x0F, 0xE0 | (AUDIO_PID >> 8), AUDIO_PID & 0xFF, 0xF0, 0]))
    out = [_packet(0, seq, pat, True), _packet(PMT_PID, seq, pmt, True)]
    cc = (seq * FRAMES) % 16
    for i in range(FRAMES):
        pts = first + i * FRAME_TICKS
        pes = b"\x00\x00\x01\xc0" + (3 + 5 + len(frame)).to_bytes(2, "big") + b"\x80\x80\x05" + _pts(pts) + frame
        base = (pts - PCR_LEAD) % MOD33
        pcr = bytes([0x50 if i == 0 else 0x10]) + ((base << 15) | (0x3F << 9)).to_bytes(6, "big")  # base, reserved, ext 0
        out.append(_packet(AUDIO_PID, cc, pes, True, pcr))
        cc = (cc + 1) % 16
    return b"".join(out)


def playlist(now: float) -> bytes:
    """A live playlist of the last WINDOW silent segments; the numbers advance with the clock."""
    top = int(now // SEGMENT_SECONDS)
    first = max(0, top - WINDOW + 1)
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:2", f"#EXT-X-MEDIA-SEQUENCE:{first}"]
    for n in range(first, top + 1):
        lines += [f"#EXTINF:{EXTINF},", f"quiet-{n}.ts"]
    return ("\n".join(lines) + "\n").encode()


def parse_name(name: str) -> Optional[int]:
    m = QUIET.fullmatch(name)
    return int(m.group(1)) if m else None


def _tags_start(lines: list, at: int) -> int:
    """Where the tags of the segment on line `at` begin (its EXTINF and the like come before its file name)."""
    k = at
    while k > 0 and lines[k - 1].split(":", 1)[0] in ("#EXTINF", "#EXT-X-PROGRAM-DATE-TIME", "#EXT-X-BYTERANGE",
                                                       "#EXT-X-DISCONTINUITY"):
        k -= 1
    return k


def _drop_sequence(lines: list) -> Tuple[list, int]:
    """The lines without #EXT-X-DISCONTINUITY-SEQUENCE, and its value (0 when absent)."""
    out, value = [], 0
    for l in lines:
        if l.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
            try:
                value = int(l.split(":", 1)[1])
            except ValueError:
                pass
        else:
            out.append(l)
    return out, value


def _sequence(lines: list) -> Tuple[list, int]:
    """The lines without #EXT-X-MEDIA-SEQUENCE, and its value (0 when absent); the caller puts the new one back."""
    out, value = [], 0
    for l in lines:
        if l.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                value = int(l.split(":", 1)[1])
            except ValueError:
                pass
        else:
            out.append(l)
    return out, value


def bridge(text: str, record: Optional[dict], top: Optional[int] = None) -> Tuple[str, dict]:
    """Rewrite a real playlist so a player that was fed silent segments switches over cleanly. The first real segment
    the bridge sees gets an EXT-X-DISCONTINUITY before it (unless the playlist already has one there) for as long as
    it stays in the window; once it has left, the discontinuity sequence counts it. Returns (text, record); the record
    is {"first": <that segment's file name>, "mine": whether the tag is ours, "offset": the media sequence shift}.

    The silence is numbered by the clock (2 s steps) and the radio's segments by microseconds, so the numbers would
    jump by orders of magnitude at the switch. The listen link therefore serves ONE monotonic sequence: at the first
    real playlist `offset` is chosen so that its first segment continues right after the last silent number served
    (`top`), and every later real playlist is shifted by the same offset. Segment file names are not touched."""
    lines = text.splitlines()
    segs = [i for i, l in enumerate(lines) if l and not l.startswith("#")]
    if not segs:
        return text, record or {}
    lines, base = _drop_sequence(lines)
    lines, first_number = _sequence(lines)
    segs = [i for i, l in enumerate(lines) if l and not l.startswith("#")]
    if record and isinstance(record.get("first"), str):
        record = dict(record)
    else:   # the first real playlist after the silence: remember its first segment, and whether the tag was already there
        k = _tags_start(lines, segs[0])
        record = {"first": lines[segs[0]], "mine": "#EXT-X-DISCONTINUITY" not in lines[k:segs[0]],
                  "offset": (top + 1 - first_number) if top is not None else 0}
    at = next((i for i in segs if lines[i] == record["first"]), None)
    if at is None:
        seq = base + (1 if record.get("mine") else 0)   # the tag has left the window; the sequence counts our tag
    else:
        seq = base
        k = _tags_start(lines, at)
        if "#EXT-X-DISCONTINUITY" not in lines[k:at]:
            lines.insert(k, "#EXT-X-DISCONTINUITY")
    offset = record.get("offset") if type(record.get("offset")) is int else 0
    head = 1 if lines and lines[0] == "#EXTM3U" else 0
    lines.insert(head, f"#EXT-X-MEDIA-SEQUENCE:{first_number + offset}")
    lines.insert(head, f"#EXT-X-DISCONTINUITY-SEQUENCE:{seq}")
    return "\n".join(lines) + "\n", record
