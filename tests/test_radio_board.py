"""The radio's project show: the per-project data layer, the slides (an overview column per project, a wall of
notes per project, the owner's desk, the ticker), the staff edition's names-and-counts-only rule, the secret
filter, the painter's layouts, the show's rotation, caches and steady clock, and the runtime switch.
All data is invented."""
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

import pytest

from ctb import cards, panel_settings, radio, radio_board as B, radio_motion as M, radio_paint as P, radio_show as S
from ctb import radio_staff
from test_decisions import ORBIT, ORBIT_S, THREAD, cfg, queued, rig, roomy_budget, server  # noqa: F401

NOW = 1_800_000_000.0
NAMES = {"alef": "פרויקט אלף", "bet": "פרויקט בית", "gimel": "פרויקט גימל"}
HAS_SWIFT = bool(shutil.which("swiftc")) and os.uname().sysname == "Darwin"


def sources(**over):
    src = {
        "ledger": {"alef": [{"id": "1", "text": "אישור פרסום גרסת בדיקה", "status": "open", "wait": "owner", "updated": NOW - 2900},
                            {"id": "2", "text": "מסך כניסה חדש", "status": "done", "wait": "", "updated": NOW - 700},
                            {"id": "3", "text": "סקירת קוד אחרונה", "status": "open", "wait": "", "updated": NOW - 5000},
                            {"id": "4", "text": "ישן מאוד", "status": "done", "wait": "", "updated": NOW - 3 * 86400}],
                   "bet": [{"id": "1", "text": "Fix the checkout total", "status": "done", "wait": "", "updated": NOW - 1500}]},
        "board": [("alef", {"k": "check", "i": 1, "s": "בדיקות אוטומטיות", "x": True, "at": NOW - 2000}),
                  ("alef", {"k": "check", "i": 2, "s": "הכנת גרסת בדיקה", "x": False, "at": NOW - 4000}),
                  ("gimel", {"k": "remind", "i": 3, "s": "לצלם תמונות לפי המדריך", "at": NOW - 9000})],
        "cards": [("alef", {"id": "c1", "steps": ["עיצוב לוח הבקרה", "תרגום ההגדרות", "שיפור מהירות"], "at": 1,
                            "step_at": [NOW - 300], "updated": NOW - 300})],
        "sessions": [{"id": "s1", "state": "working", "project": "alef"}, {"id": "s2", "state": "idle", "project": "alef"},
                     {"id": "s3", "state": "working", "project": "gimel"}, {"id": "s4", "state": "idle", "project": "dalet"}],
        "agents": {"s1": 2},
        "decisions": [{"project": "alef", "text": "בחירה בין שני קונספטים", "at": NOW - 720}],
        "questions": [{"project": "gimel", "text": "סשן עיצוב", "at": NOW - 60}],
    }
    src.update(over)
    return src


# --- the data layer -------------------------------------------------------------------------------------

def test_collect_merges_every_source_per_project():
    ps = {p["key"]: p for p in B.collect(sources(), NAMES, NOW)}
    a = ps["alef"]
    assert a["name"] == "פרויקט אלף"
    assert [d["text"] for d in a["done"]] == ["עיצוב לוח הבקרה", "מסך כניסה חדש", "בדיקות אוטומטיות"]  # newest first, 24 h only
    assert [d["text"] for d in a["doing"]] == ["תרגום ההגדרות"]               # the progress card's current step
    assert {d["text"] for d in a["todo"]} == {"סקירת קוד אחרונה", "הכנת גרסת בדיקה", "שיפור מהירות"}
    assert {o["kind"] for o in a["owner"]} == {"approval", "decision"}
    assert (a["sessions"], a["working"], a["agents"]) == (2, 1, 2)
    assert a["status"] == "owner" and a["pct"] == round(100 * 3 / 7)
    assert [d["text"] for d in a["landed"]] == ["עיצוב לוח הבקרה", "מסך כניסה חדש", "בדיקות אוטומטיות"]
    assert ps["bet"]["status"] == "landed" and ps["bet"]["pct"] == 100
    assert {o["kind"] for o in ps["gimel"]["owner"]} == {"reminder", "question"}
    assert "dalet" not in ps  # an idle session alone does not make a project active


def test_collect_order_aliases_and_hebrew_wording():
    order = [p["key"] for p in B.collect(sources(), NAMES, NOW)]
    assert order[:2] == ["alef", "gimel"] and order[-1] == "bet"  # waiting on the owner and working first
    # a project group named like a display name is the same project
    src = sources(sessions=[{"id": "s9", "state": "working", "project": B.project_key("פרויקט אלף")}])
    alef = [p for p in B.collect(src, NAMES, NOW) if p["key"] == "alef"][0]
    assert alef["working"] == 1
    bet = [p for p in B.collect(sources(), NAMES, NOW, hebrew=lambda t: "תיקון הסכום" if t.startswith("Fix") else t)
           if p["key"] == "bet"][0]
    assert bet["done"][0]["text"] == "תיקון הסכום"


def test_no_sources_means_no_projects():
    assert B.collect({}, NAMES, NOW) == []
    sl = B.slides([], NOW)
    assert [s["kind"] for s in sl] == ["overview"] and sl[0]["cols"] == []  # the empty overview only
    assert B.ticker([], NOW) == []


def test_card_steps_keep_their_finish_time(rig):
    rec = cards.publish(ORBIT_S, ORBIT, THREAD, "progress", "בנייה", steps=["א", "ב", "ג"], at=1, now=100.0)
    assert rec["step_at"] == [100.0]
    rec = cards.publish(ORBIT_S, ORBIT, THREAD, "progress", "", ident=rec["id"], at=2, now=200.0)
    assert rec["step_at"] == [100.0, 200.0]
    rec = cards.publish(ORBIT_S, ORBIT, THREAD, "progress", "", ident=rec["id"], steps=["א", "שונה", "ג"], at=2, now=300.0)
    assert rec["step_at"] == [100.0, 300.0]  # a reworded step is stamped again
    assert cards.open_cards()[0]["step_at"] == [100.0, 300.0]


# --- the slides --------------------------------------------------------------------------------------------

def many(n, items=1):
    return [{"key": f"p{i}", "name": f"פרויקט {i}", "done": [], "doing": [{"text": f"משימה {i}-{j}"} for j in range(items)],
             "todo": [], "owner": [], "landed": [], "sessions": 1, "working": 1, "agents": 0, "status": "go",
             "pct": 0, "total": items, "last": float(i)} for i in range(n)]


def test_overview_pages_and_a_wall_per_project_most_recent_first():
    sl = B.slides(many(10, items=4), NOW)
    overviews = [s for s in sl if s["kind"] == "overview"]
    assert [len(o["cols"]) for o in overviews] == [8, 2] and overviews[1]["pages"] == 2
    walls = [s for s in sl if s["kind"] == "wall"]
    assert [w["name"] for w in walls] == [f"פרויקט {i}" for i in range(9, -1, -1)]
    assert all(len(w["cols"]["doing"]) == B.WALL_NOTES and w["more"]["doing"] == 1 for w in walls)
    assert all(s["key"] == B.digest(s) for s in sl)


def test_little_to_show_means_one_combined_wall():
    sl = B.slides(many(3), NOW)
    walls = [s for s in sl if s["kind"] == "wall"]
    assert len(walls) == 1 and walls[0]["name"] == ""
    assert {n["tag"] for n in walls[0]["cols"]["doing"]} == {"פרויקט 0", "פרויקט 1", "פרויקט 2"}


def test_the_desk_only_when_something_waits_oldest_first():
    ps = B.collect(sources(), NAMES, NOW)
    w = [s for s in B.slides(ps, NOW) if s["kind"] == "waiting"][0]
    assert [r["age"] for r in w["rows"]] == ["2:30", "0:48", "0:12", "0:01"]
    assert w["rows"][0]["kind"] == "תזכורת"
    assert "waiting" not in [s["kind"] for s in B.slides(many(2), NOW)]


def test_the_ticker_lists_what_waits_first_then_landings_in_full():
    entries = B.ticker(B.collect(sources(), NAMES, NOW), NOW)
    marks = [e["mark"] for e in entries]
    assert marks == ["waiting"] * 4 + ["landed"] * 4
    assert all(set(e) <= {"mark", "time", "project", "kind", "text"} for e in entries)
    assert all("time" not in e for e in entries if e["mark"] == "waiting")   # no age: it would change each minute


# --- the staff edition: names and counts only ---------------------------------------------------------------

SECRET_ITEM = "סוד פנימי של הבעלים"


def test_no_item_text_can_reach_a_staff_frame():
    snap = {"at": NOW, "done": [{"key": "x:1", "project": "alef", "text": SECRET_ITEM, "updated": NOW - 60}],
            "ledger": [{"key": "l:1", "project": "alef", "text": SECRET_ITEM, "at": NOW}],
            "decisions": [{"key": "d:1", "text": SECRET_ITEM, "topic": "alef", "at": NOW}],
            "team_open_by": {"alef": 3}}
    prof = radio_staff.profile({"enabled": True, "projects": {"alef": "פרויקט אלף"}}, "he")
    view = radio_staff.staff_view(snap, prof)
    view["projects"][0]["text"] = SECRET_ITEM          # even a field slipped into the view is never read
    view["extra"] = SECRET_ITEM
    sl = B.staff_slide(view)
    assert sl["rows"] == [{"name": "פרויקט אלף", "done": 1, "open": 3}]
    ops = json.dumps(P.layout(sl, "/nowhere/x.png").data(), ensure_ascii=False)
    assert SECRET_ITEM not in ops and "פרויקט אלף" in ops


def test_a_staff_label_that_fails_the_gate_is_left_out():
    sl = B.staff_slide({"lang": "he", "sections": ["done_today", "in_work"], "at": NOW,
                        "projects": [{"label": "Project with English", "done": [], "open": 2},
                                     {"label": "פרויקט בית", "done": [{"key": "a"}], "open": True}]})
    assert sl["rows"] == [{"name": "פרויקט בית", "done": 1, "open": 0}]


def test_the_staff_overview_shows_all_eight_listed_projects(tmp_path):
    names = ["פרויקט " + w for w in ("אלף", "בית", "גימל", "דלת", "הא", "וו", "זין", "חית")]
    view = {"lang": "he", "sections": ["done_today", "in_work"], "at": NOW,
            "projects": [{"label": n, "done": [], "open": 1} for n in names]}
    sl = B.staff_slide(view)
    assert len(sl["rows"]) == 8
    frame = P.layout(sl, str(tmp_path / "s.png"))
    texts = {op["s"] for op in frame.ops if op["op"] == "text"}
    assert set(names) <= texts
    cards_ = [op for op in frame.ops if op["op"] == "fill" and op.get("c") == P.CARD + "E8"]
    assert len(cards_) == 8 and max(op["y"] + op["h"] for op in cards_) <= P.FOOT_Y


# --- the layouts ------------------------------------------------------------------------------------------

CROSSES = set("+×✚✕✖✗✘➕⨯")


def all_frames(tmp_path):
    ps = B.collect(sources(), NAMES, NOW)
    out = [P.layout(s, str(tmp_path / f"{i}.jpg")) for i, s in enumerate(B.slides(ps, NOW))]
    out += [P.layout(s, str(tmp_path / f"m{i}.jpg")) for i, s in enumerate(B.slides(many(8, 4), NOW))]
    out += [P.layout(s, str(tmp_path / f"e{i}.jpg")) for i, s in enumerate(B.slides([], NOW))]
    out.append(P.layout(B.staff_slide({"lang": "he", "sections": ["done_today", "in_work"], "at": NOW, "projects":
                                       [{"label": "פרויקט אלף", "done": [], "open": 1}]}), str(tmp_path / "s.png")))
    out.append(P.ticker_strip(B.ticker(ps, NOW), str(tmp_path / "t.jpg")))
    return out


def test_layouts_never_mix_hebrew_and_english_in_one_text_or_draw_crosses(tmp_path):
    frames = all_frames(tmp_path)
    texts = [op["s"] for f in frames for op in f.ops if op["op"] == "text"]
    assert texts
    for t in texts:
        bare = re.sub("⁨[^⁩]*⁩", "", t)  # a short isolated name is allowed inside a Hebrew line
        assert not (B._HEBREW.search(bare) and B._LATIN.search(bare)), t
        assert not CROSSES & set(t), t
    english = [op for f in frames for op in f.ops if op["op"] == "text" and op["s"] == "Fix the checkout total"]
    assert english and all(op["dir"] == "ltr" for op in english)  # an English item keeps its own direction


def test_every_overview_size_fits_above_the_footer(tmp_path):
    for n in (1, 3, 5, 8):
        frame = P.layout(B.slides(many(n), NOW)[0], str(tmp_path / f"o{n}.jpg"))
        cards_ = [op for op in frame.ops if op["op"] == "fill" and op.get("c") == P.CARD + "E8"]
        assert len(cards_) == n
        assert all(40 <= op["x"] and op["x"] + op["w"] <= P.R + 1 and op["y"] + op["h"] <= P.FOOT_Y for op in cards_)


def test_a_wall_marks_done_notes_and_stamps_the_fresh_one(tmp_path):
    p = B.collect(sources(), NAMES, NOW)[0]
    wall = [s for s in B.slides([p], NOW) if s["kind"] == "wall"][0]
    assert wall["fresh"] == ["עיצוב לוח הבקרה"]
    still = P.layout(wall, str(tmp_path / "w.jpg"))
    rings = [op for op in still.ops if op["op"] == "circle" and op.get("sc") == "#2E9E5BDD"]
    assert len(rings) == len(wall["cols"]["done"])        # one check per done note
    stamping = P.stamp_frames(wall, tmp_path, 3)
    big = [op["r"] for op in stamping[0].ops if op["op"] == "circle" and op.get("sc") == "#2E9E5BDD"]
    assert max(big) > 20                                   # the fresh check starts large and settles


@pytest.mark.skipif(not HAS_SWIFT, reason="needs swiftc (macOS)")
def test_the_painter_draws_every_slide_and_a_transition(tmp_path):
    frames = all_frames(tmp_path)
    assert P.paint(tmp_path / "bin", frames)
    for f in frames:
        assert Path(f.out).stat().st_size > 3_000
    move = P.transition(frames[0].out, frames[1].out, tmp_path, n=3)
    assert P.paint(tmp_path / "bin", move) and all(Path(f.out).exists() for f in move)


# --- the show -----------------------------------------------------------------------------------------------

def _show_dir(tmp_path):
    d = tmp_path / "board"
    d.mkdir(exist_ok=True)
    return d


class FakePainter:
    """Writes a small distinct file per frame instead of drawing (the show's logic only)."""

    def __init__(self):
        self.calls = 0

    def __call__(self, folder, frames, timeout=60):
        self.calls += 1
        for f in frames:
            Path(f.out).write_bytes(("frame:" + Path(f.out).name).encode())
        return True


def test_the_show_rotates_caches_moves_and_redraws_only_on_change(tmp_path, monkeypatch):
    fake = FakePainter()
    monkeypatch.setattr(P, "paint", fake)
    clock = [0.0]
    show = S.BoardShow(_show_dir(tmp_path), tmp_path / "bin", clock=lambda: NOW, mono=lambda: clock[0])
    show.update(B.collect(sources(), NAMES, NOW))
    show.step()
    assert show.on_air["kind"] == "overview" and show.frame().startswith(b"frame:slide-")
    assert show.strip() == b"frame:ticker-0.jpg"
    drawn = show.drawn
    show.step()                                   # nothing changed: nothing drawn
    assert show.drawn == drawn
    clock[0] += S.DWELL["overview"] + 5
    show.step()
    assert show.on_air["kind"] == "wall" and len(show.queue) == S.MOVE_FRAMES
    played = [show.frame() for _ in range(S.MOVE_FRAMES)]
    assert all(p.startswith(b"frame:0") for p in played) and show.frame().startswith(b"frame:slide-")
    calls = fake.calls                            # the same pair again moves from the cache
    show.index, show.on_air, show.until = 0, show.slides[0], 0
    show.step()
    assert fake.calls == calls and len(show.queue) == S.MOVE_FRAMES


def test_a_new_landing_plays_the_sting_then_stamps_its_wall(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "paint", FakePainter())
    motion = tmp_path / "motion"
    for unit in M.UNITS:
        (motion / unit).mkdir(parents=True)
        for i in range(3):
            (motion / unit / f"{i:04d}.jpg").write_bytes(f"{unit}{i}".encode())
    (motion / M.MANIFEST).write_text(json.dumps({"digest": M.source_digest(), "fps": M.FPS,
                                                 "frames": {u: 3 for u in M.UNITS}}))
    show = S.BoardShow(_show_dir(tmp_path), tmp_path / "bin", motion_dir=motion, clock=lambda: NOW, mono=lambda: 0.0)
    show.update(B.collect(sources(), NAMES, NOW))
    show.step()
    assert [show.frame() for _ in range(3)] == [b"ident0", b"ident1", b"ident2"]  # the station ident first
    assert len(show.queue) == S.MOVE_FRAMES   # then the overview moves in over the ident's last frame
    while show.queue:
        show.frame()
    later = B.collect(sources(ledger={"bet": [{"id": "9", "text": "נחת עכשיו", "status": "done", "wait": "",
                                               "updated": NOW - 5}]}), NAMES, NOW)
    show.update(later)
    show.step()
    assert show.on_air["kind"] == "wall" and show.on_air["fresh"]
    assert [show.frame() for _ in range(3)] == [b"landed0", b"landed1", b"landed2"]
    rest = [show.frame() for _ in range(S.MOVE_FRAMES + S.STAMP_FRAMES)]
    assert all(r.startswith(b"frame:stamp-") for r in rest[-S.STAMP_FRAMES:])


def test_prepared_units_must_match_the_source(tmp_path):
    (tmp_path / "ident").mkdir()
    (tmp_path / "ident" / "0000.jpg").write_bytes(b"x")
    (tmp_path / M.MANIFEST).write_text(json.dumps({"digest": "old", "fps": M.FPS, "frames": {"ident": 1}}))
    assert M.load(tmp_path) == {}
    assert M.load(None) == {}
    assert "puppeteer-core" in M.prepare(tmp_path / "out", str(tmp_path), "/no/chrome")


def test_a_drawing_failure_is_reported_for_the_card_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "paint", lambda *a, **k: False)
    show = S.BoardShow(_show_dir(tmp_path), tmp_path / "bin", clock=lambda: NOW)
    show.update([])
    quit = threading.Event()
    show.run(quit, every=0.01)              # returns at once: the painter failed
    assert show.failed == "RuntimeError"


# --- the stream and the switch -------------------------------------------------------------------------------

def test_board_encoder_settings_ticker_overlay_and_no_secret_on_argv():
    args = radio.encoder_args("h264_videotoolbox", 3, fps=S.FPS, codec=S.CODEC, bitrate=radio.BOARD_BITRATE,
                              strip_fd=4, strip=(1100, 662, 6))
    joined = " ".join(args)
    assert "-c:v mjpeg -framerate 10" in joined and "-b:v 600k" in joined and "rtmp" not in joined
    assert "-i pipe:4" in joined and "crop=w=1100:h=ih:x='iw-1100-mod(n*6,iw/2)'" in joined
    assert "overlay=x=0:y=662" in joined and "-map [v]" in joined
    assert " ".join(radio.encoder_args("libx264", 3)).count("-c:v png -framerate 2") == 1  # the card is unchanged


def test_the_board_switch_is_a_runtime_setting_off_by_default():
    assert "radio_board" in panel_settings.SETTINGS
    assert panel_settings.read()["radio_board"] is False and radio.board_on() is False
    panel_settings.write("radio_board", True)
    assert radio.board_on() is True
    panel_settings.write("radio_board", False)
    assert radio.board_on() is False


def test_the_station_keeps_the_card_when_the_board_failed_recently(monkeypatch):
    from conftest import GOOD_TOKEN
    from ctb.config import Config
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=1, chat_id=-1001234, radio={"every_minutes": 20})
    monkeypatch.setattr(radio, "make_bed", lambda kind, db: bytes(4))
    st = radio.Station(cfg)
    radio.set_board(True)
    try:
        assert st.board_wanted()
        st.board_failed("RuntimeError")
        assert not st.board_wanted() and st.show is None
        st.board_failed_at -= radio.BOARD_RETRY
        assert st.board_wanted()
    finally:
        radio.set_board(False)


# --- numbers read left to right ------------------------------------------------------------------------------

def _tiles(frame, y0, y1, x0=0, x1=1280):
    """(x, text) of the one-character tiles drawn between y0 and y1, left to right."""
    return sorted((op["x"], op["s"]) for op in frame.ops if op["op"] == "text" and len(op["s"]) == 1
                  and y0 <= op["y"] <= y1 and x0 <= op["x"] <= x1)


def test_a_fraction_reads_done_then_total_from_left_to_right():
    x = P.Frame("/nowhere/f.jpg")
    P.fraction(x, 12, 40, 700, 0, 30, 50, 28)
    assert "".join(s for _, s in _tiles(x, -1, 1)) == "12/40"
    p = B.collect(sources(), NAMES, NOW)[0]
    wall = [s for s in B.slides([p], NOW) if s["kind"] == "wall"][0]
    head = P.layout(wall, "/nowhere/w.jpg")
    assert "".join(s for _, s in _tiles(head, 20, 40, 0, 400)) == f"{wall['done_n']}/{wall['total']}"


def test_every_number_group_reads_left_to_right():
    for text in ("14:52", "2:30", "43%"):
        x = P.Frame("/nowhere/n.jpg")
        P.digits(x, text, 900, 0, 30, 50)
        assert "".join(s for _, s in _tiles(x, -1, 1)) == text


def test_a_latin_project_name_in_the_footer_is_isolated():
    rows = many(1, items=12)
    rows[0]["name"] = "Orbit"
    sl = B.slides(rows, NOW)[0]
    line = P.next_line(sl, P.TEXT["he"])
    assert "⁨Orbit⁩" in line


# --- review fixes -----------------------------------------------------------------------------------------------

TOKEN = "123456789" + ":AA" + "Bc" * 17   # a synthetic bot-token-like string the credential filter removes


def test_no_secret_reaches_a_paint_command(tmp_path):
    src = sources()
    src["ledger"]["alef"].append({"id": "7", "text": "פריט עם " + TOKEN, "status": "open", "wait": "", "updated": NOW})
    src["board"].append(("alef", {"k": "check", "i": 9, "s": "בדיקה " + TOKEN, "x": True, "at": NOW - 10}))
    src["cards"].append(("gimel", {"id": "c9", "steps": ["שלב " + TOKEN, "הבא"], "at": 1, "step_at": [NOW - 5],
                                   "updated": NOW}))
    src["decisions"].append({"project": "bet", "text": TOKEN + " החלטה ארוכה מאוד " * 6, "at": NOW})
    src["questions"].append({"project": "gimel", "text": "שאלה " + TOKEN, "at": NOW})
    names = dict(NAMES, bet="בית " + TOKEN)
    ps = B.collect(src, names, NOW, hebrew=lambda t: "תרגום " + TOKEN if t.startswith("Fix") else t)
    ops = [json.dumps(P.layout(s, str(tmp_path / f"{i}.jpg")).data(), ensure_ascii=False)
           for f_i in range(len(ps)) for i, s in enumerate(B.slides(ps, NOW, focus_index=f_i))]
    ops.append(json.dumps(P.ticker_strip(B.ticker(ps, NOW), str(tmp_path / "t.jpg")).data(), ensure_ascii=False))
    joined = "".join(ops)
    assert TOKEN not in joined and TOKEN[:14] not in joined and "AABcBc" not in joined


def test_a_malformed_manifest_means_no_units_and_the_station_keeps_the_card(tmp_path, monkeypatch):
    (tmp_path / "ident").mkdir()
    (tmp_path / "ident" / "0000.jpg").write_bytes(b"x")
    for frames in ([1, 2], "x", {"ident": "1"}, {"ident": True}):
        (tmp_path / M.MANIFEST).write_text(json.dumps({"digest": M.source_digest(), "fps": M.FPS, "frames": frames}))
        assert M.load(tmp_path) == {}
    from conftest import GOOD_TOKEN
    from ctb.config import Config
    monkeypatch.setattr(radio, "make_bed", lambda kind, db: bytes(4))
    st = radio.Station(Config(bot_token=GOOD_TOKEN, owner_chat_id=1, chat_id=-1001234, radio={"every_minutes": 20}))
    monkeypatch.setattr(P, "available", lambda folder: True)

    def broken(*a, **k):
        raise AttributeError("bad manifest")
    monkeypatch.setattr(S, "BoardShow", broken)
    assert st.start_board() is None and st.board_failed_at > 0 and st.show is None


def test_old_pictures_and_landings_do_not_pile_up(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "paint", FakePainter())
    board = tmp_path / "board"
    board.mkdir()
    for i in range(100):
        (board / f"slide-{i:016x}.jpg").write_bytes(b"old")
    mono = [0.0]
    show = S.BoardShow(board, tmp_path / "bin", clock=lambda: NOW, mono=lambda: mono[0])
    show.update(B.collect(sources(), NAMES, NOW))
    show.step()
    assert len(list(board.glob("slide-*.jpg"))) <= S.KEEP_PICTURES
    for n in range(50):  # landings come and go: what is remembered stays bounded
        mono[0] += S.SEEN_FOR / 10
        show.update([dict(many(1)[0], landed=[{"key": f"k{n}", "text": "x", "at": NOW}])])
    assert len(show.landed_seen) <= 12


def test_a_legacy_card_does_not_invent_new_landings(rig):
    import json as _json
    from ctb.config import state_dir
    rec = cards.publish(ORBIT_S, ORBIT, THREAD, "progress", "ישן", steps=["א", "ב", "ג"], at=1, now=100.0)
    path = state_dir() / cards.STATE_FILE
    data = _json.loads(path.read_text())
    data[rec["id"]].pop("step_at")          # written before finish times existed
    path.write_text(_json.dumps(data))
    rec = cards.publish(ORBIT_S, ORBIT, THREAD, "progress", "", ident=rec["id"], at=2, now=NOW)
    assert rec["step_at"] == [0.0, NOW]     # the old step's time stays unknown
    p = B.collect({"cards": [("alef", rec)]}, NAMES, NOW)[0]
    assert [d["text"] for d in p["landed"]] == ["ב"] and [d["text"] for d in p["done"]] == ["ב", "א"]


def test_the_rotation_follows_a_steady_clock_not_the_wall_clock(tmp_path, monkeypatch):
    monkeypatch.setattr(P, "paint", FakePainter())
    wall, mono = [NOW], [0.0]
    show = S.BoardShow(_show_dir(tmp_path), tmp_path / "bin", clock=lambda: wall[0], mono=lambda: mono[0])
    show.update(B.collect(sources(), NAMES, NOW))
    show.step()
    first = show.on_air["kind"]
    with show.lock:
        show.queue.clear()
    wall[0] -= 3600                          # the wall clock jumps back an hour
    mono[0] += S.DWELL["overview"] + 3
    show.step()
    assert show.on_air["kind"] != first


def test_a_mixed_name_keeps_each_run_in_reading_order():
    assert P.visual("בית ABC") == list("ABC") + [" "] + list("תיב")
    assert P.visual("פרויקט אלף") == list("ףלא טקיורפ")
    assert P.visual("Orbit 2") == list("Orbit 2")
    x = P.Frame("/nowhere/m.jpg")
    P.flaps(x, "בית ABC", 1000, 0, 30, 50, 9)
    shown = "".join(s for _, s in _tiles(x, -1, 1) if s.strip())
    assert shown == "ABCתיב"


# --- review round 2 ---------------------------------------------------------------------------------------------

def _no_connect(monkeypatch):
    from ctb import radio_rtmp
    made = []

    class Never:
        def __init__(self, *a, **k):
            made.append(a)
            raise AssertionError("connected after the gate said no")
    monkeypatch.setattr(radio_rtmp, "Publisher", Never)
    return made


def test_the_gate_before_connecting_honours_bridge_off_and_stop(monkeypatch, tmp_path):
    from ctb import policy
    made = _no_connect(monkeypatch)
    card = tmp_path / "card.png"
    monkeypatch.setattr(policy, "on", lambda: False)        # switched off while the first picture was prepared
    pipe = radio.Pipeline("rtmps://example.org/s/", "key", card, "libx264", radio.Mixer(bytes(4)), lambda pid: None)
    assert pipe.run() == radio.MIRROR_OFF and made == []
    monkeypatch.setattr(policy, "on", lambda: True)
    quit = threading.Event()
    quit.set()                                               # a stop arrived meanwhile
    pipe = radio.Pipeline("rtmps://example.org/s/", "key", card, "libx264", radio.Mixer(bytes(4)), lambda pid: None,
                          quit=quit)
    assert pipe.run() == "" and made == []


def _station(monkeypatch):
    from conftest import GOOD_TOKEN
    from ctb.config import Config
    monkeypatch.setattr(radio, "make_bed", lambda kind, db: bytes(4))
    monkeypatch.setattr(P, "available", lambda folder: True)
    monkeypatch.setattr(P, "paint", FakePainter())
    st = radio.Station(Config(bot_token=GOOD_TOKEN, owner_chat_id=1, chat_id=-1001234, radio={"every_minutes": 20}))
    monkeypatch.setattr(st, "feed_board", lambda now: None)
    return st


def test_two_fixed_show_folders_used_in_turn_and_only_own_files_cleared(monkeypatch):
    st = _station(monkeypatch)
    first = st.start_board()
    (first.dir / "notes.txt").write_text("not ours")          # a file the show did not write
    second = st.start_board()
    assert first.dir.name == "show-a" and second.dir.name == "show-b"
    third = st.start_board()
    assert third.dir == first.dir and (first.dir / "notes.txt").read_text() == "not ours"
    st.stop_board()


def test_a_show_still_drawing_keeps_its_folder(monkeypatch):
    st = _station(monkeypatch)
    st.start_board()
    busy = threading.Event()
    st.show_threads["show-a"] = threading.Thread(target=busy.wait, args=(5,), daemon=True)
    st.show_threads["show-a"].start()
    st.show_threads["show-b"] = threading.Thread(target=busy.wait, args=(5,), daemon=True)
    st.show_threads["show-b"].start()
    assert st.free_slot(wait=0.05) is None and st.start_board() is None   # both busy: the card stays
    busy.set()


def test_a_linked_show_folder_is_refused_and_nothing_outside_is_touched(monkeypatch, tmp_path):
    from ctb.config import state_dir
    st = _station(monkeypatch)
    outside = tmp_path / "outside"
    (outside / "show-a").mkdir(parents=True)
    keep = outside / "show-a" / "slide-0123456789abcdef.jpg"
    keep.write_bytes(b"someone else's")
    board = state_dir() / "radio" / "board"
    board.parent.mkdir(parents=True, exist_ok=True)
    if board.exists():
        import shutil as _sh
        _sh.rmtree(board)
    board.symlink_to(outside, target_is_directory=True)
    assert st.start_board() is None and st.board_failed_at is not None   # the card stays
    assert keep.read_bytes() == b"someone else's" and sorted(p.name for p in outside.iterdir()) == ["show-a"]


BAD = ["תיקון login במסך", "א + ב", "מחיקת ✕ ישן", "עדכון the whole long English sentence here במסך", "C++ ו ✚ †"]


def test_paintable_enforces_the_language_and_shape_rules():
    for raw in BAD:
        out = B.paintable(raw)
        assert not (CROSSES | set("✚†✝✞")) & set(out), out
        bare = re.sub("⁨[^⁩]*⁩", "", out)
        assert not (B._HEBREW.search(bare) and B._LATIN.search(bare)), out
    assert B.paintable("תיקון login במסך") == "תיקון ⁨login⁩ במסך"   # a short name stays, isolated
    assert "whole long English" not in B.paintable(BAD[3])                      # a long run leaves the Hebrew line
    assert B.paintable(B.paintable(BAD[0])) == B.paintable(BAD[0])              # stable when applied again
    assert B.paintable("Fix the checkout total") == "Fix the checkout total"    # one script: unchanged


def test_bad_text_never_reaches_the_painter_even_through_a_translation(tmp_path):
    src = sources()
    src["ledger"]["alef"] += [{"id": f"b{i}", "text": t, "status": "open", "wait": "", "updated": NOW} for i, t in enumerate(BAD)]
    ps = B.collect(src, NAMES, NOW, hebrew=lambda t: "תרגום עם + ו login" if t.startswith("Fix") else t)
    frames = [P.layout(s, str(tmp_path / f"{i}.jpg")) for i, s in enumerate(B.slides(ps, NOW))]
    frames.append(P.ticker_strip(B.ticker(ps, NOW), str(tmp_path / "t.jpg")))
    for f in frames:
        for op in f.ops:
            if op["op"] == "text":
                assert not (CROSSES | set("✚†")) & set(op["s"]), op["s"]
                bare = re.sub("⁨[^⁩]*⁩", "", op["s"])
                assert not (B._HEBREW.search(bare) and B._LATIN.search(bare)), op["s"]


def test_every_ticker_entry_fits_and_is_drawn():
    t = P.TEXT["he"]
    huge = {"mark": "landed", "time": "12:00", "project": "פרויקט " + "ארוך " * 20, "text": "עדכון ארוך מאוד " * 30}
    pieces = P.fit_entry(huge, t)
    assert P._span(pieces) <= P.ENTRY_BUDGET
    f = P.ticker_strip([huge] * 3, "/nowhere/t.jpg", "he", 0)
    texts = [op for op in f.ops if op["op"] == "text"]
    assert len(texts) >= 2 * len(pieces) - 2                  # both halves draw their entries, nothing is lost
    assert all(0 <= op["x"] and op["x"] + op["w"] <= P.STRIP_W for op in texts)


def test_within_one_rotation_every_entry_is_drawn_owner_request_first(tmp_path, monkeypatch):
    painted = []

    def paint(folder, frames, timeout=60):
        painted.extend(frames)
        for f in frames:
            Path(f.out).write_bytes(f.out.encode() + str(len(painted)).encode())
        return True
    monkeypatch.setattr(P, "paint", paint)
    long_ = "עדכון ארוך מאוד שחוזר על עצמו כדי למלא את הפס "
    entries = ([{"mark": "waiting", "project": "פרויקט בקשה", "kind": "החלטה", "text": "בקשה אחת לבעלים"}] +
               [{"mark": "landed", "time": "10:0" + str(i), "project": f"פרויקט ארוך {i}", "text": long_ * 3 + str(i)}
                for i in range(10)] +
               [{"mark": "landed", "time": "11:0" + str(i % 10), "project": f"פרויקט {i}", "text": f"נחיתה {i}"}
                for i in range(13)])
    monkeypatch.setattr(B, "ticker", lambda projects, now, lang="he": entries)
    show = S.BoardShow(_show_dir(tmp_path), tmp_path / "bin", clock=lambda: NOW, mono=lambda: 0.0)
    per_cycle = -(-P.HALF // S.TICKER_SPEED)
    windows = -(-len(entries) // P.TICKER_WINDOW)
    first = None
    for c in range(windows):
        show._ticker(NOW)
        if c == 3:  # a change of content in the middle never sends the windows back to the start
            entries[5] = dict(entries[5], text="שונה")
            show._ticker(NOW)
        for _ in range(per_cycle):
            show.strip()
        if first is None:
            first = [op["s"] for f in painted for op in f.ops if op["op"] == "text"]
    drawn = " ".join(op["s"] for f in painted for op in f.ops if op["op"] == "text")
    assert all(e["project"][:10] in drawn for e in entries)
    right_half = [op["s"] for op in painted[0].ops if op["op"] == "text" and op["x"] > P.HALF]
    assert "בקשה אחת לבעלים" in right_half                 # the owner's request opens the first window


def test_the_retry_after_a_failure_uses_a_steady_clock(monkeypatch):
    from conftest import GOOD_TOKEN
    from ctb.config import Config
    monkeypatch.setattr(radio, "make_bed", lambda kind, db: bytes(4))
    st = radio.Station(Config(bot_token=GOOD_TOKEN, owner_chat_id=1, chat_id=-1001234, radio={"every_minutes": 20}))
    radio.set_board(True)
    try:
        st.board_failed("RuntimeError")
        st.board_failed_at -= radio.BOARD_RETRY + 1
        real = time.time
        monkeypatch.setattr(time, "time", lambda: real() - 3600)   # the wall clock steps back an hour
        assert st.board_wanted()
    finally:
        radio.set_board(False)
