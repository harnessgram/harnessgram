"""Project groups, resolution and setup: a new session is placed once (folder, git, title), and the
`project` command and the doctor look after the groups."""
import json
import os
import subprocess
from pathlib import Path

import pytest

from conftest import FakeBot
from ctb import cli
from ctb import config as C
from ctb import doctor
from ctb import projects as P
from ctb.config import state_dir
from ctb.telegram import Bot, TelegramError
from ctb.topics import TopicMap

OWNER, HOME, ORBIT, PANTRY = 7, -1001234567890, -1001111111111, -1002222222222
DOCS = "Docu" + "ments"  # the generic folder; built from pieces for the public-hygiene guard
BASE = {"bot_token": "1:" + "a" * 35, "owner_chat_id": OWNER, "chat_id": HOME}
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@example.test", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, env=dict(os.environ, **GIT_ENV))


def make_cfg(*projects):
    return C.from_dict(dict(BASE, projects=list(projects)))


def session_in(folder, session="s1"):
    """A Claude transcript that says the session started in `folder`."""
    log = Path.home() / ".claude" / "projects" / "x" / f"{session}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(json.dumps({"type": "user", "cwd": str(folder)}) + "\n", encoding="utf-8")
    return session


@pytest.fixture
def home(tmp_path):
    return Path.home()


def test_a_worktree_maps_to_the_project_of_its_repo(home):
    repo = home / "work" / "orbit"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    (repo / "f").write_text("x")
    git(repo, "add", "f")
    git(repo, "commit", "-qm", "x")
    tree = home / "elsewhere" / "feature-tree"
    git(repo, "worktree", "add", "-q", "-b", "feat", str(tree))
    by_path = make_cfg({"name": "Orbit", "chat_id": ORBIT, "match": {"paths": ["~/work/orbit"]}})
    assert P.resolve(by_path, session_in(tree), "") == (by_path.projects[0], "path")  # via the main repo's folder
    by_repo = make_cfg({"name": "Orbit", "chat_id": ORBIT, "match": {"repos": ["Orbit"]}})
    assert P.resolve(by_repo, session_in(tree), "") == (by_repo.projects[0], "repo")
    git(repo, "remote", "add", "origin", "git@example.test:org/gamestudio.git")
    by_origin = make_cfg({"name": "G", "chat_id": ORBIT, "match": {"repos": ["gamestudio"]}})
    assert P.resolve(by_origin, session_in(tree), "")[1] == "repo"
    other = make_cfg({"name": "Other", "chat_id": ORBIT, "match": {"repos": ["pantry"], "paths": ["~/nowhere"]}})
    assert P.resolve(other, session_in(tree), "") == (None, "")


def test_a_path_rule_matches_the_folder_and_below_and_patterns(home):
    folder = home / "code" / "orbit-feature" / "src"
    folder.mkdir(parents=True)
    plain = make_cfg({"name": "W", "chat_id": ORBIT, "match": {"paths": ["~/code/orbit-feature"]}})
    assert P.resolve(plain, session_in(folder), "")[1] == "path"
    pattern = make_cfg({"name": "W", "chat_id": ORBIT, "match": {"paths": ["~/code/orbit-*"]}})
    assert P.resolve(pattern, session_in(folder), "") == (pattern.projects[0], "path")
    sibling = make_cfg({"name": "W", "chat_id": ORBIT, "match": {"paths": ["~/code/orbit"]}})
    assert P.resolve(sibling, session_in(folder), "") == (None, "")  # a longer name is not "below"


def test_documents_alone_is_home(home):
    docs = home / DOCS
    docs.mkdir()
    cfg = make_cfg({"name": "W", "chat_id": ORBIT, "match": {"paths": [f"~/{DOCS}/Orbit"], "repos": [DOCS.lower()]},
                    "folders": [f"~/{DOCS}/Orbit"]})
    assert P.resolve(cfg, session_in(docs), "") == (None, "")
    with pytest.raises(C.ConfigError):
        make_cfg({"name": "W", "chat_id": ORBIT, "match": {"paths": [f"~/{DOCS}"]}})


def test_a_title_prefix_at_creation_places_the_session_and_stays_sticky(bot):
    cfg = make_cfg({"name": "Orbit", "chat_id": ORBIT, "match": {"title_prefixes": ["ווינגס", "orbit"]}})
    topics = TopicMap(state_dir() / "topics.json", bot, HOME, cfg=cfg)
    assert P.resolve(cfg, "s", " ORBIT : fix") == (cfg.projects[0], "title")
    assert P.resolve(cfg, "s", "orbit fix") == (None, "")  # a prefix needs the colon
    topics.ensure("a", "Orbit: fix the game")
    assert topics.place_of("a").chat == ORBIT
    entry = json.loads((state_dir() / "topics-projects.json").read_text())["a"]
    assert (entry["project"], entry["why"]) == ("Orbit", "title")
    assert bot.methods("createForumTopic")[-1]["chat_id"] == ORBIT
    topics.ensure("a", "Something else entirely")  # a title written later never moves it
    assert topics.place_of("a").chat == ORBIT
    topics.ensure("b", "Notes")
    assert topics.place_of("b").home
    topics.ensure("b", "Orbit: now")  # nor does a later prefix pull a home session in
    assert topics.place_of("b").home


def test_path_beats_title_and_why_is_recorded(bot, home):
    (home / "m").mkdir()
    cfg = make_cfg({"name": "Orbit", "chat_id": ORBIT, "match": {"title_prefixes": ["Orbit"]}},
                   {"name": "Pantry", "chat_id": PANTRY, "match": {"paths": ["~/m"]}})
    topics = TopicMap(state_dir() / "topics.json", bot, HOME, cfg=cfg)
    topics.ensure(session_in(home / "m", "p"), "Orbit: but really pantry")
    assert topics.place_of("p").chat == PANTRY
    assert json.loads((state_dir() / "topics-projects.json").read_text())["p"]["why"] == "path"


def test_explicit_chat_is_recorded_as_explicit(bot):
    cfg = make_cfg({"name": "Orbit", "chat_id": ORBIT, "match": {"title_prefixes": ["Pantry"]}})
    TopicMap(state_dir() / "topics.json", bot, HOME, cfg=cfg).ensure("a", "Pantry: x", chat=ORBIT)
    assert json.loads((state_dir() / "topics-projects.json").read_text())["a"]["why"] == "explicit"


def test_unresolved_and_git_trouble_go_home(bot, home, monkeypatch):
    cfg = make_cfg({"name": "Orbit", "chat_id": ORBIT, "match": {"repos": ["orbit"]}})
    folder = home / "plain"
    folder.mkdir()
    assert P.resolve(cfg, session_in(folder), "Notes") == (None, "")  # not a repository
    assert P.resolve(cfg, "no-transcript", "Notes") == (None, "")

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("git", 2)
    monkeypatch.setattr(P.subprocess, "run", boom)
    assert P.resolve(cfg, session_in(folder), "Notes") == (None, "")
    monkeypatch.setattr(P.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("git")))
    assert P.resolve(cfg, session_in(folder), "Notes") == (None, "")
    topics = TopicMap(state_dir() / "topics.json", bot, HOME, cfg=cfg)
    topics.ensure(session_in(folder, "h"), "Notes")
    assert topics.place_of("h").home
    # a title prefix still counts when git fails
    cfg2 = make_cfg({"name": "Orbit", "chat_id": ORBIT, "match": {"repos": ["orbit"], "title_prefixes": ["Orbit"]}})
    assert P.resolve(cfg2, session_in(folder), "Orbit: x")[1] == "title"


def test_git_is_given_a_short_deadline(home, monkeypatch):
    seen = []

    def fake(cmd, **kw):
        seen.append(kw["timeout"])
        return subprocess.CompletedProcess(cmd, 1, "", "")
    monkeypatch.setattr(P.subprocess, "run", fake)
    cfg = make_cfg({"name": "W", "chat_id": ORBIT, "match": {"repos": ["w"]}})
    P.resolve(cfg, session_in(home), "")
    assert seen and all(0 < t <= P.GIT_TIMEOUT for t in seen)


def test_empty_projects_never_runs_git_or_reads_transcripts(bot, monkeypatch):
    monkeypatch.setattr(P.subprocess, "run", lambda *a, **k: pytest.fail("git ran"))
    cfg = C.from_dict(BASE)
    topics = TopicMap(state_dir() / "topics.json", bot, HOME, cfg=cfg)
    topics.ensure("a", "Orbit: x")
    assert topics.place_of("a").home
    assert not (state_dir() / "topics-projects.json").exists()
    assert "project" not in json.loads((state_dir() / "topics.json").read_text())["a"]


# --- the project command -------------------------------------------------------------------------

@pytest.fixture
def settings():
    C.save_raw(dict(BASE))
    return C.config_path()


def run_cli(*argv):
    return cli.main(["project", *argv])


def test_add_list_remove(settings, capsys):
    assert run_cli("add", "Orbit", "--chat", str(ORBIT), "--paths", "~/code/orbit-*", "--paths", "~/code/orbit",
                   "--repos", "orbit", "--title-prefix", "Orbit", "--title-prefix", "ווינגס",
                   "--folder", "~/code/orbit") == 0
    saved = json.loads(settings.read_text())["projects"]
    assert saved == [{"name": "Orbit", "chat_id": ORBIT, "folders": ["~/code/orbit"],
                      "match": {"paths": ["~/code/orbit-*", "~/code/orbit"], "repos": ["orbit"],
                                "title_prefixes": ["Orbit", "ווינגס"]}}]
    capsys.readouterr()
    assert run_cli("list") == 0
    out = capsys.readouterr().out
    assert "1 of 10" in out and f"Orbit: group {ORBIT}" in out and "repos: orbit" in out and "ווינגס" in out
    assert run_cli("remove", "orbit") == 0
    assert json.loads(settings.read_text())["projects"] == []
    assert run_cli("list") == 0 and "No project groups" in capsys.readouterr().out


def test_add_validates_like_the_settings(settings, capsys):
    before = settings.read_bytes()
    for argv in (("add", "Orbit", "--chat", "5"),                              # not a supergroup id
                 ("add", "Orbit", "--chat", str(HOME)),                        # the home group
                 ("add", "Orbit", "--chat", str(ORBIT), "--paths", f"~/{DOCS}"),   # too generic
                 ("add", "Orbit", "--chat", str(ORBIT), "--paths", "relative/dir"),
                 ("remove", "Nobody")):
        assert run_cli(*argv) == 1, argv
        assert settings.read_bytes() == before
    assert capsys.readouterr().err.count("Harnessgram:") == 5
    assert run_cli("add", "Orbit", "--chat", str(ORBIT)) == 0
    assert run_cli("add", "ORBIT", "--chat", str(PANTRY)) == 1     # same name
    assert run_cli("add", "Other", "--chat", str(ORBIT)) == 1      # same group
    assert "already exists" in capsys.readouterr().err


def test_at_most_ten_project_groups(settings, capsys):
    for i in range(10):
        assert run_cli("add", f"P{i}", "--chat", str(-1003000000000 - i)) == 0
    capsys.readouterr()
    assert run_cli("add", "P10", "--chat", str(-1003000000099)) == 1
    assert "at most 10" in capsys.readouterr().err
    assert len(json.loads(settings.read_text())["projects"]) == 10
    assert run_cli("remove", "P3") == 0
    assert run_cli("add", "P10", "--chat", str(-1003000000099)) == 0


def test_projects_need_group_mode(capsys):
    C.save_raw({k: v for k, v in BASE.items() if k != "chat_id"})
    assert run_cli("add", "Orbit", "--chat", str(ORBIT)) == 1
    assert "group mode" in capsys.readouterr().err


def test_removed_project_sessions_rehome_on_next_activity(settings, bot):
    run_cli("add", "Orbit", "--chat", str(ORBIT), "--title-prefix", "Orbit")
    topics = TopicMap(state_dir() / "topics.json", bot, HOME, cfg=C.load())
    topics.ensure("a", "Orbit: x")
    assert topics.place_of("a").chat == ORBIT
    run_cli("remove", "Orbit")
    gone = TopicMap(state_dir() / "topics.json", bot, HOME, cfg=C.load())
    assert gone.place_of("a") is None
    gone.ensure("a", "Orbit: x")
    assert gone.place_of("a").home


class GroupBot(FakeBot):
    def __init__(self, group=ORBIT, forum=True, status="administrator", sender=OWNER, **rights):
        super().__init__()
        self.group, self.forum, self.status, self.sender = group, forum, status, sender
        self.rights = dict({"can_manage_topics": True, "can_pin_messages": True, "can_delete_messages": True}, **rights)
        self.members = 2
        self.updates = [{"update_id": 1, "message": {"date": 2000000000, "text": "/start@demo_bot",
                                                     "chat": {"id": group, "type": "supergroup"},
                                                     "from": {"id": sender}}}]

    def call(self, method, params=None, timeout=15):
        self.calls.append((method, params or {}))
        if method == "getMe":
            return {"id": 1, "username": "demo_bot"}
        if method == "getChat":
            return {"id": params["chat_id"], "is_forum": self.forum, "title": "Orbit"}
        if method == "getChatMember":
            return dict({"status": self.status}, **self.rights)
        if method == "getChatMemberCount":
            return self.members
        if method == "getUpdates":
            out, self.updates = self.updates, []
            return out
        return {}


def ticks():
    return iter(range(2000000000, 2000001000)).__next__


def test_pair_saves_the_group_the_owner_started(settings):
    cfg = C.load()
    msg = P.pair(cfg, GroupBot(), "Orbit", paths=["~/code/orbit"], now=ticks())
    assert "Orbit" in msg
    assert C.load().projects[0].chat_id == ORBIT and C.load().projects[0].paths == ["~/code/orbit"]
    # pairing an existing project points it at the new group and keeps its rules
    P.pair(C.load(), GroupBot(group=PANTRY), "orbit", now=ticks())
    project = C.load().projects[0]
    assert (len(C.load().projects), project.chat_id, project.paths) == (1, PANTRY, ["~/code/orbit"])


def test_pair_refuses_strangers_missing_topics_and_missing_rights(settings):
    cfg = C.load()
    for bot, needle in ((GroupBot(sender=99), "no /start"), (GroupBot(forum=False), "Topics"),
                        (GroupBot(status="member"), "admin"),
                        (GroupBot(can_manage_topics=False), "admin")):
        with pytest.raises(C.ConfigError, match=needle):
            P.pair(cfg, bot, "Orbit", timeout=2, now=ticks())
    assert C.load().projects == []
    private = C.Config(owner_chat_id=OWNER)
    with pytest.raises(C.ConfigError, match="group mode"):
        P.pair(private, GroupBot(), "Orbit")


def test_pair_command_saves_and_asks_for_a_restart(settings, monkeypatch, capsys):
    bot = GroupBot()
    monkeypatch.setattr(Bot, "from_config", classmethod(lambda cls, cfg, strict=False: bot))
    assert run_cli("pair", "Orbit") == 0
    assert "Restart the listener" in capsys.readouterr().out
    assert C.load().projects[0].chat_id == ORBIT


# --- doctor --------------------------------------------------------------------------------------

def doctor_cfg():
    return make_cfg({"name": "Orbit", "chat_id": ORBIT}, {"name": "Pantry", "chat_id": PANTRY})


def test_doctor_has_nothing_to_say_without_projects():
    assert doctor.check_projects(C.from_dict(BASE), GroupBot()) == []


def test_doctor_passes_a_good_group():
    out = doctor.check_projects(doctor_cfg(), GroupBot())
    assert [level for level, _ in out] == ["ok"] * 4 and "project Orbit" in out[0][1]


def test_doctor_flags_each_problem():
    bad = doctor.check_projects(doctor_cfg(), GroupBot(forum=False))
    assert [level for level, _ in bad].count("FAIL") == 2 and "Topics" in bad[0][1]
    for kwargs, right in (({"can_manage_topics": False}, "Manage topics"), ({"can_pin_messages": False}, "Pin messages"),
                          ({"can_delete_messages": False}, "Delete messages")):
        out = doctor.check_projects(doctor_cfg(), GroupBot(**kwargs))
        assert any(level == "FAIL" and right in msg for level, msg in out), kwargs
    out = doctor.check_projects(doctor_cfg(), GroupBot(status="member"))
    assert any("Manage topics, Pin messages, Delete messages" in msg for _, msg in out)
    assert all(level == "ok" for level, _ in doctor.check_projects(doctor_cfg(), GroupBot(status="creator")))


def test_doctor_warns_when_others_are_in_the_group():
    bot = GroupBot()
    bot.members = 3
    out = doctor.check_projects(doctor_cfg(), bot)
    assert [level for level, _ in out].count("warn") == 2 and "3 members" in out[-1][1]


def test_doctor_reports_a_group_it_cannot_reach():
    class Broken(GroupBot):
        def call(self, method, params=None, timeout=15):
            if params and params.get("chat_id") == PANTRY and method == "getChat":
                raise TelegramError("Bad Request: chat not found")
            return super().call(method, params, timeout)
    out = doctor.check_projects(doctor_cfg(), Broken())
    assert any(level == "FAIL" and "Pantry" in msg and "chat not found" in msg for level, msg in out)
    assert any(level == "ok" and "Orbit" in msg for level, msg in out)


def test_patterns_cannot_reach_below_the_guarded_folders():
    for rule in (f"~/{DOCS}/*", "~/*/*", os.path.dirname(str(Path.home())) + "/*", f"~/{DOCS}/?*/x"):
        with pytest.raises(C.ConfigError):
            make_cfg({"name": "W", "chat_id": ORBIT, "match": {"paths": [rule]}})
    assert make_cfg({"name": "W", "chat_id": ORBIT, "match": {"paths": [f"~/{DOCS}/orbit-*"]}}).projects


def test_a_borrowed_git_file_does_not_claim_another_projects_folder(home):
    repo = home / "secret"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / "f").write_text("x")
    git(repo, "add", "f")
    git(repo, "commit", "-qm", "x")
    git(repo, "worktree", "add", "-q", "-b", "feat", str(home / "real-tree"))
    planted = home / "downloads" / "clone"
    planted.mkdir(parents=True)
    (planted / ".git").write_text(f"gitdir: {repo}/.git/worktrees/real-tree\n")
    cfg = make_cfg({"name": "S", "chat_id": ORBIT, "match": {"paths": ["~/secret"]}})
    assert P.resolve(cfg, session_in(planted), "") == (None, "")
    direct = home / "downloads" / "direct"
    direct.mkdir()
    (direct / ".git").write_text(f"gitdir: {repo}/.git\n")
    assert P.resolve(cfg, session_in(direct), "") == (None, "")
    assert P.resolve(cfg, session_in(home / "real-tree"), "")[1] == "path"  # the genuine worktree still maps


def test_two_projects_cannot_share_a_repo_or_title_prefix():
    for key in ("repos", "title_prefixes"):
        with pytest.raises(C.ConfigError):
            make_cfg({"name": "A", "chat_id": ORBIT, "match": {key: ["work"]}},
                     {"name": "B", "chat_id": PANTRY, "match": {key: ["Work "]}})
