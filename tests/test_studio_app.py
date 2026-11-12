"""The Studio app without a browser: its words, its API command (read-only but for the owner's draft marks), its
install as a Cockpit app, and the rules its page must keep (logical CSS only, no markup from data, no network, no plus
or cross shapes)."""
import io
import json
import os
import re
import stat
from pathlib import Path

import pytest

from ctb import cockpit_apps, panel_page, policy
from ctb.studio import api, install, marks, page, provider, safefs, strings

STUDIO = Path(__file__).resolve().parent.parent / "src" / "ctb" / "studio"
JS = (STUDIO / "app.js").read_text(encoding="utf-8")
CSS = (STUDIO / "kit.css").read_text(encoding="utf-8")
HEBREW = re.compile(r"[֐-׿]")
LATIN = re.compile(r"[A-Za-z]")


def strings_of(lang):
    out = []

    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        else:
            out.append(re.sub(r"\{\w+\}", "", v) if isinstance(v, str) else v)  # a {slot} name is not text
    walk(strings.table()[lang])
    return out


# --- the words ------------------------------------------------------------------------------------------------

def test_every_key_exists_in_both_languages():
    t = strings.table()
    assert strings.keys(t["he"]) == strings.keys(t["en"])


def test_hebrew_and_english_never_share_a_line():
    assert not [s for s in strings_of("he") if LATIN.search(s)]
    assert not [s for s in strings_of("en") if HEBREW.search(s)]


def test_the_script_uses_only_keys_the_table_has():
    keys = strings.keys(strings.table()["he"])
    used = set(re.findall(r'\bt(?:n)?\("([a-zA-Z.]+)"', JS))
    prefixes = {k for k in used if k.endswith(".")}
    exact = {k for k in used if not k.endswith(".")}
    plain = {k for k in exact if k in keys or any(x.startswith(k + ".") for x in keys)}
    assert exact <= plain, exact - plain
    for pre in prefixes:
        assert any(x.startswith(pre) for x in keys), pre
    for stage in provider.STAGES:
        assert "home.stage." + stage in keys
    for status in provider.STATUSES:
        assert "home.status." + status in keys


def test_copy_has_no_digits_beside_hebrew_words_in_fixed_lines():
    # a count is a slot ({n}) filled with words by the script, never a digit typed into a Hebrew sentence
    assert not [s for s in strings_of("he") if isinstance(s, str) and re.search(r"\d", s)]
    assert not [s for s in strings_of("he") if isinstance(s, str) and re.search(r"[A-Za-z]", s)]


# --- the data and the command -------------------------------------------------------------------------------------

def test_the_sample_follows_its_documented_shape():
    d = provider.desk()
    assert d["sample"] is True
    scenes = set(page.scene_ids())
    series = {s["id"] for s in d["series"]}
    for w in d["waiting"]:
        assert w["series"] in series and w["scene"] in scenes and w["length_s"] > 0
    for j in d["working"]:
        assert j["stage"] in provider.STAGES and 0 <= j["pct"] <= 1 and j["scene"] in scenes
    for it in d["shelf"]:
        assert it["status"] in provider.STATUSES and it["series"] in series and it["scene"] in scenes
        assert ("ago_s" in it) == (it["status"] == "published")
    for tp in d["templates"]:
        assert tp["format"] in provider.FORMATS and tp["scene"] in scenes
    # texts are {he, en} or plain strings, never anything else
    for text in [w["title"] for w in d["waiting"]] + [s["name"] for s in d["series"]]:
        assert set(text) == {"he", "en"}
    ids = [x["id"] for k in ("waiting", "working", "shelf") for x in d[k]]
    assert len(ids) == len(set(ids)) and not [i for i in ids if re.search(r"[0-9a-f]{8}-", i)]


def run(body):
    out = io.StringIO()
    api.main(io.StringIO(body if isinstance(body, str) else json.dumps(body)), out)
    return json.loads(out.getvalue())


def test_the_command_reads_the_desk_and_the_projects_and_refuses_everything_else():
    r = run({"op": "desk.get"})
    assert r["ok"] is True and r["desk"]["sample"] is True
    r = run({"op": "project.get", "project": "weekly-12"})
    assert r["ok"] is True and r["project"]["id"] == "weekly-12" and r["marks"] == {}
    assert run({"op": "project.get", "project": "weekly-99"}) == {"ok": False, "error": "not_found"}
    for bad in ({"op": "render.start"}, {"op": "publish.prepare"}, {"op": "job.get"}, {"op": "review.approve_all"},
                {"op": ["desk.get"]}, {"nope": 1}, [], "x", "not json", "", "{" * 20,
                json.dumps({"op": "desk.get", "pad": "x" * api.MAX_IN}),
                {"op": "project.get"}, {"op": "project.get", "project": "../etc"}, {"op": "project.get", "project": 12}):
        r = run(bad)
        assert r["ok"] is False and r["error"] in ("bad_request", "unknown_op"), bad


def test_a_failing_provider_answers_with_a_plain_code(monkeypatch):
    def boom():
        raise RuntimeError("/private/path/secret")
    monkeypatch.setattr(provider, "desk", boom)
    r = api.handle({"op": "desk.get"})
    assert r == {"ok": False, "error": "unavailable"}


def test_the_command_writes_only_the_draft_marks_and_only_through_the_safe_writer():
    src = (STUDIO / "api.py").read_text() + (STUDIO / "provider.py").read_text()
    assert not re.search(r"\b(open\(|write_text|write_bytes|subprocess|os\.system|requests|urllib|socket)\b", src)
    m = (STUDIO / "marks.py").read_text()
    assert not re.search(r"\b(write_text|write_bytes|subprocess|os\.system|requests|urllib|socket|shutil|unlink|rmtree|"
                         r"os\.remove|os\.rename|O_TRUNC|O_WRONLY)\b|(?<![\w.])open\(", m)
    assert m.count("safefs.write_file(") == 1
    # every os.open of the store refuses links
    for call in re.findall(r"os\.open\(([^\n]+)", m):
        assert "O_NOFOLLOW" in call, call


# --- the draft marks: the one write -----------------------------------------------------------------------------

def mark(folder, **body):
    return api.handle({"op": "marks.set", **body}, str(folder) if folder else None)


def test_a_mark_is_stored_in_the_apps_own_state_folder_and_read_back(tmp_path):
    tmp_path = tmp_path / "app"
    tmp_path.mkdir()
    r = mark(tmp_path, project="weekly-12", i=5, mark="tighten", text="  בסתיו   הימים מתקצרים. ")
    assert r == {"ok": True, "marks": {"5": {"mark": "tighten", "text": "בסתיו הימים מתקצרים."}}}
    assert mark(tmp_path, project="weekly-12", i=2, mark="remove")["marks"]["2"] == {"mark": "remove"}
    assert mark(tmp_path, project="weekly-13", i=1, mark="keep")["marks"] == {"1": {"mark": "keep"}}
    got = api.handle({"op": "project.get", "project": "weekly-12"}, str(tmp_path))["marks"]
    assert got == {"5": {"mark": "tighten", "text": "בסתיו הימים מתקצרים."}, "2": {"mark": "remove"}}
    assert mark(tmp_path, project="weekly-12", i=2, mark="clear")["marks"] == {"5": got["5"]}
    state = tmp_path / "state"
    assert sorted(p.name for p in state.iterdir()) == [".lock", "marks.json"]
    assert stat.S_IMODE(state.stat().st_mode) == 0o700 and stat.S_IMODE((state / "marks.json").stat().st_mode) == 0o600
    assert not [p for p in tmp_path.iterdir() if p.name != "state"]  # nothing else in the app folder


@pytest.mark.parametrize("body,code", [
    ({"project": "weekly-12", "i": 5, "mark": "approve"}, "bad_request"),
    ({"project": "weekly-12", "i": 0, "mark": "keep"}, "bad_request"),
    ({"project": "weekly-12", "i": 13, "mark": "keep"}, "bad_request"),
    ({"project": "weekly-12", "i": "5", "mark": "keep"}, "bad_request"),
    ({"project": "weekly-12", "i": True, "mark": "keep"}, "bad_request"),
    ({"project": "weekly-12", "i": 5.0, "mark": "keep"}, "bad_request"),
    ({"project": "weekly-12", "i": 5, "mark": "keep", "text": "x"}, "bad_request"),
    ({"project": "weekly-12", "i": 5, "mark": "remove", "text": "x"}, "bad_request"),
    ({"project": "weekly-12", "i": 5, "mark": "tighten", "text": "x" * 241}, "bad_request"),
    ({"project": "weekly-12", "i": 5, "mark": "tighten", "text": "a\u0000b"}, "bad_request"),
    ({"project": "weekly-12", "i": 5, "mark": "tighten", "text": "   "}, "bad_request"),
    ({"project": "weekly-12", "i": 5, "mark": "tighten", "text": 7}, "bad_request"),
    ({"project": "../../etc", "i": 1, "mark": "keep"}, "bad_request"),
    ({"project": "Weekly-12", "i": 1, "mark": "keep"}, "bad_request"),
    ({"project": "weekly-99", "i": 1, "mark": "keep"}, "not_found"),
    ({"i": 1, "mark": "keep"}, "bad_request"),
])
def test_a_mark_the_store_refuses_answers_a_plain_code_and_writes_nothing(tmp_path, body, code):
    assert mark(tmp_path, **body) == {"ok": False, "error": code}
    assert not (tmp_path / "state" / "marks.json").exists()


def test_without_its_app_folder_the_command_stays_read_only(tmp_path, monkeypatch):
    tmp_path = tmp_path / "app"
    tmp_path.mkdir()
    monkeypatch.chdir(tmp_path)
    assert mark(None, project="weekly-12", i=1, mark="keep") == {"ok": False, "error": "unavailable"}
    assert run({"op": "marks.set", "project": "weekly-12", "i": 1, "mark": "keep"}) == {"ok": False, "error": "unavailable"}
    assert list(tmp_path.iterdir()) == []


def test_a_symlinked_state_folder_is_refused_and_nothing_is_written_through_it(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    app = tmp_path / "app"
    app.mkdir()
    (app / "state").symlink_to(victim, target_is_directory=True)
    assert mark(app, project="weekly-12", i=1, mark="keep") == {"ok": False, "error": "unavailable"}
    assert list(victim.iterdir()) == []
    assert api.handle({"op": "project.get", "project": "weekly-12"}, str(app))["marks"] == {}  # reading refuses it too


def test_a_symlinked_app_folder_is_refused(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (tmp_path / "app").symlink_to(victim, target_is_directory=True)
    assert mark(tmp_path / "app", project="weekly-12", i=1, mark="keep") == {"ok": False, "error": "unavailable"}
    assert list(victim.iterdir()) == []


def test_planted_links_in_the_state_folder_never_reach_a_file_outside(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me")
    state = tmp_path / "app" / "state"
    state.mkdir(parents=True)
    (state / "marks.json").symlink_to(outside)
    # reading through the planted link is refused: the video opens without marks, the file outside is never read
    assert api.handle({"op": "project.get", "project": "weekly-12"}, str(tmp_path / "app"))["marks"] == {}
    assert mark(tmp_path / "app", project="weekly-12", i=1, mark="keep") == {"ok": False, "error": "unavailable"}
    (state / "marks.json").unlink()
    (state / ".lock").unlink(missing_ok=True)
    (state / ".lock").symlink_to(outside)
    assert mark(tmp_path / "app", project="weekly-12", i=1, mark="keep") == {"ok": False, "error": "unavailable"}
    assert outside.read_text() == "keep me"
    (state / ".lock").unlink()
    assert mark(tmp_path / "app", project="weekly-12", i=1, mark="keep")["ok"] is True
    assert outside.read_text() == "keep me" and not (state / "marks.json").is_symlink()


def test_a_damaged_or_oversized_marks_file_starts_over_instead_of_blocking(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "marks.json").write_text("{not json")
    assert api.handle({"op": "project.get", "project": "weekly-12"}, str(tmp_path))["marks"] == {}
    assert mark(tmp_path, project="weekly-12", i=1, mark="keep")["marks"] == {"1": {"mark": "keep"}}
    (state / "marks.json").write_text(json.dumps({"weekly-12": {"1": {"mark": "keep"}}, "pad": "x" * marks.MAX_FILE}))
    assert api.handle({"op": "project.get", "project": "weekly-12"}, str(tmp_path))["marks"] == {}
    # foreign or broken entries are dropped on read, never echoed back
    (state / "marks.json").write_text(json.dumps({"weekly-12": {"1": {"mark": "publish"}, "99": {"mark": "keep"},
                                                                "2": {"mark": "keep", "text": "x"}, "x": 1}}))
    assert api.handle({"op": "project.get", "project": "weekly-12"}, str(tmp_path))["marks"] == {"2": {"mark": "keep"}}


def test_the_store_keeps_a_bounded_number_of_videos(tmp_path, monkeypatch):
    monkeypatch.setattr(marks, "MAX_PROJECTS", 1)
    assert mark(tmp_path, project="weekly-13", i=1, mark="keep")["ok"]
    assert mark(tmp_path, project="weekly-12", i=1, mark="keep")["ok"]
    data = json.loads((tmp_path / "state" / "marks.json").read_text())
    assert list(data) == ["weekly-12"]


def test_two_quick_marks_never_lose_one_another(tmp_path):
    import threading
    errors = []

    def tap(i):
        r = mark(tmp_path, project="weekly-12", i=i, mark="remove" if i % 2 else "keep")
        if not r["ok"]:
            errors.append(r)
    threads = [threading.Thread(target=tap, args=(i,)) for i in range(1, 13)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert not errors
    got = api.handle({"op": "project.get", "project": "weekly-12"}, str(tmp_path))["marks"]
    assert sorted(int(k) for k in got) == list(range(1, 13))


def test_the_safe_reader_refuses_a_link_and_a_folder(tmp_path):
    (tmp_path / "real").write_text("x")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    (tmp_path / "dir").mkdir()
    fd = os.open(str(tmp_path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert safefs.read_file(fd, "real", 10) == "x"
        assert safefs.read_file(fd, "missing", 10) is None
        assert safefs.read_file(fd, "real", 0) is None  # over the limit: never half-read
        with pytest.raises(OSError):
            safefs.read_file(fd, "link", 10)
        with pytest.raises(OSError):
            safefs.read_file(fd, "dir", 10)
    finally:
        os.close(fd)


# --- one video: the shape the desk page reads ----------------------------------------------------------------------

def test_every_waiting_video_opens_as_a_project_that_follows_its_documented_shape():
    scenes = set(page.scene_ids())
    d = provider.desk()
    for w in d["waiting"]:
        p = provider.project(w["id"])
        assert p and p["id"] == w["id"] and p["sample"] is True and p["series"] == w["series"] and p["draft"] == w["draft"]
        assert p["length_s"] == w["length_s"] == p["sentences"][-1]["end"]
        assert sum(1 for s in p["sentences"] if s["suggest"]) == w["suggestions"]  # the home card's count is the desk's
        assert [s["i"] for s in p["sentences"]] == list(range(1, len(p["sentences"]) + 1))
        assert [s["i"] for s in p["scenes"]] == list(range(1, len(p["scenes"]) + 1))
        prev = 0
        for s in p["sentences"]:
            assert s["start"] == prev and s["end"] > s["start"]
            prev = s["end"]
            assert 0 <= s["scene_i"] < len(p["scenes"]) and s["scene"] in scenes
            sc = p["scenes"][s["scene_i"]]
            assert sc["start"] <= s["start"] and s["end"] <= sc["end"]
            assert s["suggest"] in (None,) + provider.SUGGESTIONS and s["flag"] in (None,) + provider.FLAGS
            assert (s["short"] is not None) == (s["suggest"] == "tighten") and (s["why"] is not None) == bool(s["suggest"])
            if s["short"]:  # in the sample the Hebrew shorter wording ends or opens what was said: the page strikes the rest
                said, short = s["said"]["he"], s["short"]["he"]
                assert said.endswith(short) or said.startswith(short.rstrip(".!?")), short
                assert s["short_s"] < s["end"] - s["start"]
            assert set(s["said"]) == {"he", "en"} and s["frame"] is None and s["audio"] is None
        for sc in p["scenes"]:
            assert sc["scene"] in scenes and set(sc["title"]) == {"he", "en"} and sc["frame"] is None
    assert provider.project("nope") is None and provider.project(None) is None



# --- installed as a Cockpit app -------------------------------------------------------------------------------------

def test_install_makes_a_valid_cockpit_app_whose_command_answers(tmp_path):
    folder = install.install()
    assert folder.name == "studio"
    app = cockpit_apps.load_app(folder, panel_page.ICON_NAMES)
    assert app.title_he == "סטודיו" and app.icon == "film" and app.media_root is None
    code, data = cockpit_apps.run_command(app, b'{"op": "desk.get"}')
    assert code == 200 and data["ok"] is True and data["desk"]["waiting"]
    code, data = cockpit_apps.run_command(app, b'{"op": "render.start"}')
    assert code == 200 and data == {"ok": False, "error": "unknown_op"}
    code, data = cockpit_apps.run_command(app, b'{"op": "marks.set", "project": "weekly-12", "i": 2, "mark": "remove"}')
    assert code == 200 and data == {"ok": True, "marks": {"2": {"mark": "remove"}}}
    code, data = cockpit_apps.run_command(app, b'{"op": "project.get", "project": "weekly-12"}')
    assert code == 200 and data["marks"] == {"2": {"mark": "remove"}}
    assert (folder / "state" / "marks.json").is_file()  # the draft stays inside the app folder
    assert "studio" in [a["name"] for a in cockpit_apps.listing()]
    before = (folder / "index.html").read_text()
    install.install()  # again: replaced in place, nothing left over, the owner's draft marks kept
    assert (folder / "index.html").read_text() == before
    assert sorted(p.name for p in folder.iterdir()) == ["api.py", "index.html", "manifest.json", "state"]
    code, data = cockpit_apps.run_command(app, b'{"op": "project.get", "project": "weekly-12"}')
    assert data["marks"] == {"2": {"mark": "remove"}}


def test_the_page_fits_the_panels_limit_and_embeds_safely():
    html = page.render_page()
    assert len(html.encode()) < cockpit_apps.MAX_PAGE
    assert "__STRINGS__" not in html and "__SCENES__" not in html
    body = html.split("<script>", 1)[1]
    assert "</script>" not in body[:-len("</script></body></html>\n")]
    assert 'dir="rtl"' in html and 'name="color-scheme"' in html


# --- the rules the page keeps ----------------------------------------------------------------------------------------

def test_css_and_script_use_logical_properties_only():
    assert not re.search(r"(?<![\w-])(?:margin|padding)-(?:left|right)", CSS + JS)
    assert not re.search(r"(?<![\w-])(?:left|right)\s*:", CSS + JS)
    assert not re.search(r"text-align\s*:\s*(?:left|right)|float\s*:\s*(?:left|right)|border-(?:left|right)|-(?:left|right)-radius", CSS + JS)


def test_direction_comes_from_the_root_and_no_field_is_auto_while_empty():
    assert 'dir="auto"' not in page.render_page() and "dir=auto" not in page.render_page()
    # the one text field is made by one helper, which gives an empty field the page's direction (behaviour: node tests)
    fields = re.findall(r"createElement\(\"(?:input|textarea)\"|h\(\"(?:input|textarea)\"", JS)
    assert len(fields) == 1
    helper = JS[JS.index("function textField("):]
    helper = helper[:helper.index("\n}\n")]
    assert 'h("textarea"' in helper and '.trim() ? "auto" : st.dir' in helper
    assert JS.count('"auto"') == 1


def test_data_is_never_drawn_as_markup_and_the_page_makes_no_network_call():
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|eval\(|new Function|srcdoc", JS)
    assert not re.search(r"\bfetch\(|XMLHttpRequest|WebSocket|importScripts|\bimport\(|EventSource|sendBeacon", JS)
    assert not re.search(r"https?://", JS.replace("http://www.w3.org/2000/svg", ""))
    assert not re.search(r"localStorage|sessionStorage|document\.cookie", JS)


def test_no_plus_or_cross_shapes_in_the_icons():
    sprite = (STUDIO / "sprite.svg").read_text()
    names = re.findall(r'<symbol id="i-([a-z-]+)"', sprite)
    assert names and not [n for n in names if n in ("plus", "cross", "close", "x", "add", "minus", "cancel")]
    # the empty-state drawing and the kit's CSS contain no "+" glyph content
    assert "content:\"+\"" not in CSS and "content:'+'" not in CSS
    assert not re.search(r'["\']\s*[+×✕✖✚]\s*["\']', JS)


def test_the_cockpit_app_registration_uses_known_panel_messages_only():
    sent = set(re.findall(r'cockpit:\s*"([a-z_]+)"', JS))
    assert sent <= {"hello", "api", "focus", "back", "badge", "back_done", "open_chat", "layout"}


def test_a_stopped_bridge_never_runs_the_command(monkeypatch):
    import threading
    from conftest import GOOD_TOKEN
    from ctb import panel
    from ctb.config import Config
    from test_panel import OWNER, call, signed
    install.install()
    monkeypatch.setattr(policy, "on", lambda: False)
    srv = panel.PanelServer(("127.0.0.1", 0), Config(bot_token=GOOD_TOKEN, owner_chat_id=OWNER, target_language="he"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        code, _, data = call(srv, path="/app/studio/api", init=signed(), body=b'{"op": "desk.get"}')
        assert code == 503 and json.loads(data) == panel.MIRROR_OFF
        code, _, _ = call(srv, method="GET", path="/app/studio/", init=signed(), body=b"")
        assert code == 503
    finally:
        srv.shutdown()
        srv.server_close()


def test_the_install_command_works_even_while_the_bridge_is_off(monkeypatch, capsys):
    from ctb import cli
    monkeypatch.setattr(policy, "on", lambda: False)
    assert cli.main(["studio", "install"]) == 0
    assert "Studio installed" in capsys.readouterr().out
    assert (cockpit_apps.apps_dir() / "studio" / "manifest.json").is_file()


# --- the install never follows a link out of the Cockpit apps folder ---------------------------------------------------------

def test_a_symlinked_studio_folder_is_refused_and_nothing_is_written_through_it(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    base = tmp_path / "apps"
    base.mkdir()
    (base / "studio").symlink_to(victim, target_is_directory=True)
    with pytest.raises(install.InstallError):
        install.install(base)
    assert list(victim.iterdir()) == []


def test_a_symlinked_apps_folder_is_refused(tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (tmp_path / "apps").symlink_to(victim, target_is_directory=True)
    with pytest.raises(install.InstallError):
        install.install(tmp_path / "apps")
    assert list(victim.iterdir()) == []


def test_a_planted_link_in_the_folder_never_reaches_a_file_outside(tmp_path):
    base = tmp_path / "apps"
    folder = base / "studio"
    folder.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me")
    for planted in ("index.html.tmp", "api.py.tmp", "manifest.json.tmp", "index.html", "api.py", "manifest.json"):
        (folder / planted).symlink_to(outside)
    install.install(base)
    assert outside.read_text() == "keep me"
    for name in ("index.html", "api.py", "manifest.json"):
        assert not (folder / name).is_symlink() and (folder / name).stat().st_size > 20
    assert not [p for p in folder.iterdir() if p.name.endswith(".tmp") and not p.is_symlink()]  # no temp file left behind


def test_the_installed_files_stay_inside_the_apps_folder_and_leave_nothing_behind(tmp_path):
    folder = install.install(tmp_path / "apps")
    assert folder.resolve().parent == (tmp_path / "apps").resolve()
    assert sorted(p.name for p in folder.iterdir()) == ["api.py", "index.html", "manifest.json"]
    assert [p.name for p in (tmp_path / "apps").iterdir()] == ["studio"]


def test_the_install_command_reports_a_refusal_instead_of_a_traceback(monkeypatch, capsys, tmp_path):
    from ctb import cli
    victim = tmp_path / "victim"
    victim.mkdir()
    (cockpit_apps.apps_dir() / "studio").parent.mkdir(parents=True, exist_ok=True)
    (cockpit_apps.apps_dir() / "studio").symlink_to(victim, target_is_directory=True)
    assert cli.main(["studio", "install"]) == 1
    assert "link" in capsys.readouterr().err
    assert list(victim.iterdir()) == []
