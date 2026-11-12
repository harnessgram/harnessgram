"""Project groups, config and helpers: `projects` is validated like `team`, and without it nothing changes."""
import json
import os

import pytest

from ctb import config as C
from ctb import projects as P
from ctb.config import Config

OWNER, HOME, TEAM = 7, -1001234567890, -1001999999999
ORBIT, PANTRY = -1001111111111, -1002222222222
BASE = {"bot_token": "1:" + "a" * 35, "owner_chat_id": OWNER, "chat_id": HOME}


def raw(*projects, **extra):
    return dict(BASE, projects=list(projects), **extra)


def orbit(**extra):
    return dict({"name": "Orbit", "chat_id": ORBIT}, **extra)


def test_missing_or_empty_projects_means_home_only():
    for cfg in (C.from_dict(BASE), C.from_dict(raw())):
        assert cfg.projects == []
        assert P.owner_chats(cfg) == frozenset({HOME})
        assert P.project_by_chat(cfg, ORBIT) is None


def test_private_mode_is_home_only():
    cfg = Config(owner_chat_id=OWNER)
    assert P.owner_chats(cfg) == frozenset({OWNER})


def test_owner_chats_and_project_by_chat():
    cfg = C.from_dict(raw(orbit(), {"name": "Pantry", "chat_id": PANTRY}))
    assert P.owner_chats(cfg) == frozenset({HOME, ORBIT, PANTRY})
    assert P.project_by_chat(cfg, PANTRY).name == "Pantry"
    assert P.project_by_chat(cfg, HOME) is None  # the home group is not a project


def test_resolve_without_projects_is_home():
    cfg = C.from_dict(BASE)
    assert P.resolve(cfg, "session", "Orbit: fix the game") == (None, "")


def test_place_key():
    assert P.Place(HOME, 5).key == "5"  # the old keys keep working at home
    assert P.Place(ORBIT, 5, home=False).key == f"{ORBIT}:5"
    assert P.Place(HOME, 5).key != P.Place(ORBIT, 5, home=False).key


def test_full_project_is_parsed():
    cfg = C.from_dict(raw(orbit(folders=["~/dev/orbit"],
                                match={"paths": ["~/dev/orbit", "~/trees/orbit-*"],
                                       "repos": ["orbit"], "title_prefixes": ["ווינגס", "Orbit"]})))
    project = cfg.projects[0]
    assert (project.name, project.chat_id) == ("Orbit", ORBIT)
    assert project.folders == ["~/dev/orbit"]
    assert project.paths == ["~/dev/orbit", "~/trees/orbit-*"]
    assert project.repos == ["orbit"] and project.title_prefixes == ["ווינגס", "Orbit"]


@pytest.mark.parametrize("bad", [
    {"name": "Orbit", "chat_id": 1001111111111},       # positive
    {"name": "Orbit", "chat_id": -555},                # not a supergroup id
    {"name": "Orbit", "chat_id": "-1001111111111"},    # not a number
    {"name": "Orbit", "chat_id": True},
    {"name": "Orbit", "chat_id": HOME},                # the home group
    {"name": "Orbit", "chat_id": TEAM},                # the team group
    {"name": "", "chat_id": ORBIT},
    {"name": "[]<>", "chat_id": ORBIT},                # nothing left after cleaning
    {"chat_id": ORBIT},
    {"name": "Orbit", "chat_id": ORBIT, "match": []},
    {"name": "Orbit", "chat_id": ORBIT, "match": {"paths": "~/dev/orbit"}},
    {"name": "Orbit", "chat_id": ORBIT, "match": {"title_prefixes": [""]}},
    {"name": "Orbit", "chat_id": ORBIT, "match": {"title_prefixes": [3]}},
    {"name": "Orbit", "chat_id": ORBIT, "match": {"repos": [" "]}},
    {"name": "Orbit", "chat_id": ORBIT, "folders": ["dev/orbit"]},  # relative
    {"name": "Orbit", "chat_id": ORBIT, "match": {"paths": ["orbit-*"]}},
])
def test_invalid_project_is_rejected(bad):
    with pytest.raises(C.ConfigError):
        C.from_dict(raw(bad, team={"chat_id": TEAM, "members": {}}))


def test_projects_must_be_a_list_of_objects():
    for bad in ({"name": "Orbit"}, ["Orbit"], "Orbit"):
        with pytest.raises(C.ConfigError):
            C.from_dict(dict(BASE, projects=bad))


def test_duplicates_and_too_many_are_rejected():
    with pytest.raises(C.ConfigError):
        C.from_dict(raw(orbit(), {"name": "Other", "chat_id": ORBIT}))
    with pytest.raises(C.ConfigError):
        C.from_dict(raw(orbit(), {"name": "orbit", "chat_id": PANTRY}))  # names compare ignoring case
    many = [{"name": f"P{i}", "chat_id": -1003000000000 - i} for i in range(11)]
    with pytest.raises(C.ConfigError):
        C.from_dict(raw(*many))
    C.from_dict(raw(*many[:10]))


def test_private_mode_rejects_projects():
    with pytest.raises(C.ConfigError):
        C.from_dict({"bot_token": BASE["bot_token"], "owner_chat_id": OWNER, "projects": [orbit()]})
    C.from_dict({"bot_token": BASE["bot_token"], "owner_chat_id": OWNER, "projects": []})


@pytest.mark.parametrize("where", ["folders", "paths"])
@pytest.mark.parametrize("generic", ["~", "~/", "~/Docu" + "ments", "~/Docu" + "ments/", "~/*", "/", "/*", "$ROOT"])
def test_generic_folder_rules_are_rejected(where, generic):
    generic = generic.replace("$ROOT", os.path.expanduser("~/work"))
    project = orbit(folders=[generic]) if where == "folders" else orbit(match={"paths": [generic]})
    with pytest.raises(C.ConfigError):
        C.from_dict(raw(project, projects_root="~/work"))


def test_a_specific_folder_under_a_generic_one_is_fine():
    C.from_dict(raw(orbit(folders=["~/dev/orbit"], match={"paths": ["~/work/orbit", "/srv/orbit-*"]}),
                    projects_root="~/work"))


def test_config_round_trip_keeps_the_projects(tmp_path):
    projects = [orbit(folders=["~/dev/orbit"], match={"repos": ["orbit"]}), {"name": "Pantry", "chat_id": PANTRY}]
    path = C.save_raw(raw(*projects), tmp_path / "c.json")
    assert json.loads(path.read_text(encoding="utf-8"))["projects"] == projects
    cfg = C.load(path)
    assert [p.to_raw() for p in cfg.projects] == projects
    assert P.owner_chats(cfg) == frozenset({HOME, ORBIT, PANTRY})


def test_save_changes_keeps_projects_when_another_key_changes(tmp_path):
    path = C.save_raw(raw(orbit()), tmp_path / "c.json")
    before = json.loads(path.read_text(encoding="utf-8"))
    after = dict(before, target_language="en")
    C.save_changes(before, after, path)
    assert C.load(path).projects[0].chat_id == ORBIT
    assert C.load(path).target_language == "en"


@pytest.mark.parametrize("chat", [-100, -1005, -10012345678, 1001111111111, -1000000000, -999999999999])
def test_project_chat_id_needs_a_full_supergroup_id(chat):
    with pytest.raises(C.ConfigError):
        C.from_dict(raw({"name": "Orbit", "chat_id": chat}))


def test_a_nine_digit_supergroup_id_is_accepted():
    assert C.from_dict(raw({"name": "Orbit", "chat_id": -100123456789})).projects[0].chat_id == -100123456789


def test_folder_rules_may_not_reach_above_the_generic_folders(tmp_path):
    home = os.path.expanduser("~")
    for rule in ("~/..", os.path.dirname(home), os.path.dirname(os.path.dirname(home))):
        with pytest.raises(C.ConfigError):
            C.from_dict(raw(orbit(match={"paths": [rule]})))
    root = os.path.expanduser("~/work/deep")
    with pytest.raises(C.ConfigError):
        C.from_dict(raw(orbit(folders=["~/work"]), projects_root=root))  # an ancestor of projects_root
    os.makedirs(root)
    link = tmp_path / "link"
    link.symlink_to(home)
    with pytest.raises(C.ConfigError):  # a symlink to the home folder is the home folder
        C.from_dict(raw(orbit(match={"paths": [str(link)]})))
    C.from_dict(raw(orbit(match={"paths": ["~/work/deep/orbit"]}), projects_root=root))


def test_overlapping_folder_rules_of_two_projects_are_rejected():
    pantry = {"name": "Pantry", "chat_id": PANTRY}
    for first, second in (
        (["~/dev/app"], ["~/dev/app"]),
        (["~/dev/app"], ["~/dev/app/sub"]),
        (["~/dev/app/sub"], ["~/dev/app"]),
        (["~/dev/app-*"], ["~/dev/app-one"]),
        (["~/dev/app-one"], ["~/dev/app-*"]),
        (["~/dev/app-*"], ["~/dev/app-o*"]),
        (["~/dev"], ["~/dev/app-*"]),
    ):
        with pytest.raises(C.ConfigError):
            C.from_dict(raw(orbit(match={"paths": first}), dict(pantry, folders=second)))


def test_separate_folder_rules_are_fine_and_one_project_may_repeat_itself():
    pantry = {"name": "Pantry", "chat_id": PANTRY, "match": {"paths": ["~/dev/pantry-*", "/srv/pantry"]}}
    C.from_dict(raw(orbit(folders=["~/dev/orbit"], match={"paths": ["~/dev/orbit", "~/dev/orbit-*"]}), pantry))
    C.from_dict(raw(orbit(match={"paths": ["~/dev/app"]}), dict(pantry, match={"paths": ["~/dev/app2"]})))
