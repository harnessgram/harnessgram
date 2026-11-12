"""Owner rule: every relevant menu offers a way out (Close, Back or Cancel).

The guard reads the source: every function that builds buttons (it names "callback_data") must also
name a way out, or be listed in NO_WAY_OUT with the reason. A new menu without one fails here."""
import ast
import re
from pathlib import Path

import pytest

from conftest import GOOD_TOKEN
from ctb import i18n, inbox, menus
from ctb.config import Config

SRC = Path(inbox.__file__).parent
# Exemptions describe individual construction sites and must match the audit.
EXEMPT = {
    ("alerts.py", "_button", 0): "channel post link, not a menu.",
    ("alerts.py", "_button", 1): "channel post link, not a menu.",
    ("board.py", "_sync", 0): "permanent pinned board; closing would remove its controls.",
    ("board.py", "_sync", 1): "permanent pinned board; closing would remove its controls.",
    ("panel_pin.py", "_markup", 0): "permanent pinned topic link, not a menu.",
    ("cockpit_notice.py", "update", 0): "one link to the open Cockpit question; the notice closes itself once answered.",
    ("telegram.py", "full_width", 0): "reformats an existing keyboard without creating a menu.",
}
WAY_OUT_CALLBACKS = {"more:dismiss", "more:unbuttons", "more:back", "close:no", "say:no", "d:x", "k:esc", "sm:l"}
WAY_OUT_PATTERNS = (r"mb:[id]:\{[^}]+\}(?::\{[^}]+\})?", r"dg:e:\{[^}]+\}",
                    r"g:\{[^}]+\}:[bx]", r"n:\{[^}]+\}:x", r"mv:\{[^}]+\}:x",
                    r"(?:dl|vd):cancel:\{[^}]+\}", r"agclose:\{[^}]+\}")
WAY_OUT_HELPERS = {"menus.close_row", "menus.not_now_row", "menus.close_button",
                   "menus.unbuttons_row", "dismiss_button", "inbox.dismiss_button"}


def has_way_out(nodes):
    for root in nodes:
        for node in ast.walk(root):
            if isinstance(node, ast.Call) and ast.unparse(node.func) in WAY_OUT_HELPERS:
                return True
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if not isinstance(key, ast.Constant) or key.value != "callback_data":
                    continue
                if isinstance(value, ast.Constant) and value.value in WAY_OUT_CALLBACKS:
                    return True
                if isinstance(value, ast.Attribute) and ast.unparse(value) == "menus.CLOSE":
                    return True
                if (isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "_cb"
                        and value.args and isinstance(value.args[0], ast.Constant) and value.args[0].value == "l"):
                    return True
                if isinstance(value, ast.JoinedStr):
                    template = "".join(part.value if isinstance(part, ast.Constant) else "{" + ast.unparse(part.value) + "}"
                                       for part in value.values)
                    if any(re.fullmatch(pattern, template) for pattern in WAY_OUT_PATTERNS):
                        return True
    return False



def keyboard_sites():
    """Enumerate every nonempty inline_keyboard expression, even URL-only ones."""
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        counters = {}
        sites = []
        for node in ast.walk(tree):
            pairs = []
            if isinstance(node, ast.Dict):
                pairs = zip(node.keys, node.values)
            elif isinstance(node, ast.Call):
                pairs = ((ast.Constant(value=k.arg), k.value) for k in node.keywords if k.arg)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                pairs = ((t.slice, node.value) for t in targets if isinstance(t, ast.Subscript))
            for key, value in pairs:
                if not isinstance(key, ast.Constant) or key.value != "inline_keyboard":
                    continue
                if isinstance(value, (ast.List, ast.Tuple)) and not value.elts:
                    continue
                parents = [f for f in functions if f.lineno <= node.lineno <= f.end_lineno]
                if parents:
                    sites.append((node.lineno, max(parents, key=lambda f: f.lineno), value))
        for _, function, value in sorted(sites, key=lambda x: x[0]):
            index = counters.get(function.name, 0)
            counters[function.name] = index + 1
            yield path, function, index, value, functions


def evidence(expression, function, functions, before, seen=None):
    """Follow only values feeding this keyboard, including their list mutations and local helpers."""
    seen = set() if seen is None else seen
    parts = [expression]
    for node in ast.walk(expression):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            name = node.id
            marker = (function.lineno, name, before)
            if marker in seen:
                continue
            seen.add(marker)
            assignments, mutations = [], []
            for statement in ast.walk(function):
                if getattr(statement, "lineno", before) >= before:
                    continue
                if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                    targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                    if any(isinstance(x, ast.Name) and x.id == name for target in targets for x in ast.walk(target)):
                        assignments.append(statement)
                elif (isinstance(statement, ast.Call) and isinstance(statement.func, ast.Attribute)
                      and isinstance(statement.func.value, ast.Name) and statement.func.value.id == name
                      and statement.func.attr in ("append", "extend", "insert")):
                    mutations.append(statement)
            # A later reassignment starts a distinct keyboard; do not borrow an
            # earlier keyboard's close row just because the variable name matches.
            assignment = max(assignments, key=lambda x: x.lineno, default=None)
            if assignment is not None:
                parts.extend(evidence(assignment.value, function, functions, assignment.lineno, seen))
            for mutation in mutations:
                if assignment is None or mutation.lineno >= assignment.lineno:
                    parts.extend(evidence(mutation, function, functions, mutation.lineno, seen))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            for helper in functions:
                if helper.name == node.func.id and helper is not function:
                    for ret in (n for n in ast.walk(helper) if isinstance(n, ast.Return) and n.value):
                        parts.extend(evidence(ret.value, helper, functions, ret.lineno + 1, seen))
    return parts


def test_every_inline_keyboard_has_its_own_way_out_or_exemption():
    sites = list(keyboard_sites())
    assert len(sites) > 50
    missing, observed = [], set()
    audit = (SRC.parent.parent / "MENU_AUDIT.md").read_text(encoding="utf-8")
    for path, function, index, value, functions in sites:
        key = (path.name, function.name, index)
        if key in EXEMPT:
            observed.add(key)
            assert EXEMPT[key] in audit
            continue
        text = evidence(value, function, functions, value.lineno + 1)
        if not has_way_out(text):
            missing.append(f"{path.name}:{value.lineno} {function.name}[{index}]: {ast.unparse(value)[:100]}")
    assert observed == set(EXEMPT), f"stale exemptions: {set(EXEMPT) - observed}"
    assert not missing, "keyboards without a way out:\n" + "\n".join(missing)


@pytest.mark.parametrize("lang", ["en", "he"])
def test_the_way_out_is_one_wording_and_one_callback(lang):
    close, later = menus.close_row(lang), menus.not_now_row(lang)
    assert [b["callback_data"] for b in close + later] == [menus.CLOSE, menus.CLOSE]
    assert close[0]["text"] == i18n.t("menu_btn_dismiss", lang=lang) != later[0]["text"]
    assert inbox.dismiss_button(lang) == close[0]


class Bot:
    def __init__(self):
        self.calls = []

    def call(self, method, params=None, **kw):
        self.calls.append((method, params))
        return {}


@pytest.mark.parametrize("lang", ["en", "he"])
def test_status_shortcut_can_be_closed(monkeypatch, lang):
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=1, chat_id=1, ui_language=lang)
    bot = Bot()
    monkeypatch.setattr(inbox.status, "rows", lambda *args, **kwargs: [])
    monkeypatch.setattr(inbox.status, "render", lambda *args: "Status")

    class Topics:
        def all(self):
            return {}

    inbox.send_status(bot, cfg, 1, {}, Topics())
    rows = bot.calls[-1][1]["reply_markup"]["inline_keyboard"]
    assert rows[-1] == menus.close_row(lang)


def test_sent_answer_receipt_has_its_own_close_row():
    # This keyboard sits in a function that builds other keyboards; examine the
    # keyboard expression itself so another menu's close row cannot hide a gap.
    tree = ast.parse((SRC / "inbox.py").read_text(encoding="utf-8"))
    keyboards = [value for node in ast.walk(tree) if isinstance(node, ast.Dict)
                 for key, value in zip(node.keys, node.values)
                 if isinstance(key, ast.Constant) and key.value == "inline_keyboard"]
    receipt = [node for node in keyboards if "sent_to_session" in ast.unparse(node)]
    assert len(receipt) == 1
    assert "menus.unbuttons_row" in ast.unparse(receipt[0])


@pytest.mark.parametrize("lang", ["en", "he"])
def test_agent_result_card_has_its_own_close_row(lang):
    from ctb import agents_card

    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=1, chat_id=1, ui_language=lang)
    card = {"seq": 1, "agents": [{"full": "sample result", "status": "done"}]}
    _, markup = agents_card.render(cfg, card, 0)
    assert markup["inline_keyboard"][-1][0]["callback_data"] == "agclose:1"


@pytest.mark.parametrize("lang", ["en", "he"])
def test_question_keyboard_can_be_deferred(lang):
    from types import SimpleNamespace
    from ctb.mirror import Mirror

    obj = SimpleNamespace(cfg=Config(bot_token=GOOD_TOKEN, owner_chat_id=1, chat_id=1,
                                    ui_language=lang))
    assert Mirror._yes_no_keyboard(obj)["inline_keyboard"][-1] == menus.unbuttons_row(lang, later=True)


def test_an_offer_in_the_owners_own_chat_with_the_bot_closes_also_when_the_home_is_a_group(tmp_path):
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=5, chat_id=-1001)
    bot = Bot()
    tap = {"id": "q", "data": menus.CLOSE, "from": {"id": 5},
           "message": {"message_id": 9, "chat": {"id": 5, "type": "private"}}}

    class Topics:
        def session_at(self, chat, thread):
            return None
    assert inbox.handle_button(tap, cfg, bot, Topics(), None) == "dismissed"
    assert ("deleteMessage", {"chat_id": 5, "message_id": 9}) in bot.calls
    stranger = dict(tap, **{"from": {"id": 6}})
    bot.calls.clear()
    assert inbox.handle_button(stranger, cfg, bot, Topics(), None) != "dismissed"
    assert not [c for c in bot.calls if c[0] == "deleteMessage"]


@pytest.mark.parametrize("kind", ["history", "members"])
def test_edit_helpers_keep_empty_keyboards_and_close_nonempty_ones(kind):
    from ctb import members

    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=1, chat_id=1, ui_language="en")
    bot = Bot()
    row = [{"text": "Sample", "callback_data": "sample"}]
    def edit(rows):
        if kind == "history":
            inbox._history_edit(bot, cfg, {"message_id": 1}, "Sample", rows)
        else:
            members._edit(bot, cfg, {"message": {"message_id": 1}}, "Sample", rows)
        return bot.calls[-1][1]["reply_markup"]["inline_keyboard"]
    assert edit([]) == []
    assert edit([row]) == [row, menus.close_row("en")]
    assert edit([row, menus.not_now_row("en")]) == [row, menus.not_now_row("en")]


def test_reusing_rows_does_not_borrow_an_earlier_close():
    tree = ast.parse("""
def sample():
    rows = [[button], menus.close_row('en')]
    first = {'inline_keyboard': rows}
    rows = [[other]]
    second = {'inline_keyboard': rows}
""")
    function = tree.body[0]
    values = [v for n in ast.walk(function) if isinstance(n, ast.Dict)
              for k, v in zip(n.keys, n.values)
              if isinstance(k, ast.Constant) and k.value == "inline_keyboard"]
    assert has_way_out(evidence(values[0], function, [function], values[0].lineno + 1))
    assert not has_way_out(evidence(values[1], function, [function], values[1].lineno + 1))


@pytest.mark.parametrize("later", [False, True])
def test_reply_close_removes_only_buttons(later):
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=1, chat_id=1)
    bot = Bot()
    class Topics:
        def session_at(self, chat, thread):
            return "sample"
    button = menus.unbuttons_row("en", later=later)[0]
    tap = {"id": "sample", "data": button["callback_data"], "from": {"id": 1},
           "message": {"message_id": 9, "text": "Sample reply?", "chat": {"id": 1, "type": "private"}}}
    assert inbox.handle_button(tap, cfg, bot, Topics(), None) == "unbuttons"
    assert ("editMessageReplyMarkup", {"chat_id": 1, "message_id": 9,
                                       "reply_markup": {"inline_keyboard": []}}) in bot.calls
    assert not any(method in ("deleteMessage", "editMessageText") for method, _ in bot.calls)
    assert tap["message"]["text"] == "Sample reply?"


def test_deferred_member_can_request_again(tmp_path, monkeypatch):
    from ctb import members
    monkeypatch.setattr(members, "state_dir", lambda: tmp_path)
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=1, chat_id=1)
    bot = Bot()
    msg = {"from": {"id": 2, "first_name": "Sample"}, "text": "Hello"}
    assert members.request(msg, cfg, bot, now=100) == "requested"
    row = bot.calls[-1][1]["reply_markup"]["inline_keyboard"][-1]
    tap = {"id": "sample", "data": row[0]["callback_data"], "message": {"message_id": 9}}
    assert members.button(tap, cfg, bot) == "dismissed"
    assert members.request(msg, cfg, bot, now=101) == "requested"


@pytest.mark.parametrize("action", ["a", "d"])
def test_old_member_card_cannot_change_a_new_request(tmp_path, monkeypatch, action):
    from ctb import members, team
    monkeypatch.setattr(members, "state_dir", lambda: tmp_path)
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=1, chat_id=1, ui_language="en")
    bot = Bot()
    msg = {"from": {"id": 2, "first_name": "Sample"}, "text": "Hello"}
    assert members.request(msg, cfg, bot, now=100) == "requested"
    old_rows = bot.calls[-1][1]["reply_markup"]["inline_keyboard"]
    old_data = next(b["callback_data"] for row in old_rows for b in row
                    if b["callback_data"].startswith(f"mb:{action}:"))
    assert members.request(msg, cfg, bot, now=100 + members.PENDING_TTL + 1) == "requested"
    new_rows = bot.calls[-1][1]["reply_markup"]["inline_keyboard"]
    new_data = next(b["callback_data"] for row in new_rows for b in row
                    if b["callback_data"].startswith(f"mb:{action}:"))
    assert old_data != new_data and len(new_data.encode()) < 64
    path = tmp_path / "member-requests.json"
    before = path.read_bytes()
    added = []
    monkeypatch.setattr(team, "add_member", lambda *args: added.append(args))
    tap = {"id": "sample", "data": old_data, "message": {"message_id": 9}}
    assert members.button(tap, cfg, bot) == "ignored"
    assert path.read_bytes() == before and not added
    assert bot.calls[-1][0] == "answerCallbackQuery"
    assert bot.calls[-1][1]["text"] == "This request is no longer open"
    assert not any(method == "editMessageText" for method, _ in bot.calls)


def test_dismissed_agents_card_does_not_reappear(tmp_path, monkeypatch):
    from ctb import agents_card
    monkeypatch.setattr(agents_card, "state_dir", lambda: tmp_path)
    cfg = Config(bot_token=GOOD_TOKEN, owner_chat_id=1, chat_id=1)
    bot = Bot()
    card = {"seq": 1, "chat": 1, "thread": 2, "message_id": 3, "dirty": True,
            "agents": [{"full": "Sample", "status": "done"}]}
    with agents_card._locked("sample") as path:
        agents_card._save(path, {"cards": {"1": card}})
    tap = {"id": "sample", "data": "agclose:1",
           "message": {"message_id": 3, "message_thread_id": 2, "chat": {"id": 1}}}
    assert agents_card.dismiss(cfg, bot, tap, "sample") == "dismissed"
    with agents_card._locked("sample") as path:
        saved = agents_card._load(path)["cards"]["1"]
    saved.update(collapsed=True, announced=False, dirty=True)
    bot.calls.clear()
    agents_card._sync(cfg, bot, saved, 100)
    assert not bot.calls and not saved["dirty"]


@pytest.mark.parametrize("callback", ["more:/account add", "sample:cancel", "sample:x", "not-say:no"])
def test_guard_rejects_lookalike_callbacks(callback):
    node = ast.parse(repr({"inline_keyboard": [[{"text": "cancel", "callback_data": callback}]]}))
    assert not has_way_out([node])


COCKPIT_EXEMPT = {
    "date": "native date picker is dismissed by the operating system.",
    "projects": "always-visible inline settings, not a dismissible picker.",
    "model": "always-visible inline settings, not a dismissible picker.",
}
REPLY_EXEMPT = {
    "reply_keyboard": "fixed topic reply keyboard, not a transient menu.",
    "fixed_keyboard": "fixed topic reply keyboard, not a transient menu.",
}


def test_exemption_inventory_matches_audit():
    audit = (SRC.parent.parent / "MENU_AUDIT.md").read_text(encoding="utf-8")
    reasons = list(EXEMPT.values()) + list(COCKPIT_EXEMPT.values()) + list(REPLY_EXEMPT.values())
    assert sorted(re.findall(r"EXEMPT: (.*)", audit)) == sorted(reasons)
    assert "MISSING" not in audit
    tree = ast.parse((SRC / "mirror.py").read_text(encoding="utf-8"))
    fixed = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)
             and any(isinstance(k, ast.Constant) and k.value == "keyboard"
                     for d in ast.walk(n) if isinstance(d, ast.Dict) for k in d.keys)}
    assert fixed == set(REPLY_EXEMPT)


def test_cockpit_dialogs_sheets_and_pickers_have_a_way_out():
    from ctb import panel_page
    from test_panel_page import func, js

    code = js()
    # All native expandable sections are closed by their own summary control.
    details = re.findall(r'el\("details"', code)
    assert len(details) == 3
    for name, summary in (("fold", 'el("summary"'), ("sessionRow", 'el("summary"'),
                          ("oldGroup", 'el("summary"')):
        body = func(name)
        assert 'el("details"' in body and summary in body and 'det.appendChild(' in body
    # Refuse newly introduced dialog/sheet/popover APIs until this inventory is extended.
    assert not re.search(r'<dialog\b|el\(["\']dialog["\']|createElement\(["\']dialog["\']|\.showModal\s*\(|\.showPopover\s*\(|role["\']?\s*,\s*["\']dialog|el\(["\'][^"\']+["\']\s*,\s*["\'][^"\']*(?:modal|sheet|popover)', code, re.I)
    assert not re.search(r'el\(["\']select["\']|<select\b|["\']listbox["\']|\.type\s*=\s*["\'](?:time|datetime-local|month|week)["\']', code, re.I)
    assert len(re.findall(r'\.type\s*=\s*["\']date["\']', code)) == 1
    assert 'd.type="date"' in func("bankCard")
    assert 'S.ytCancel' in func("ytConfirm")
    assert 'ytArmed=null;load()' in func("ytConfirm")
    assert 'el("div","ytp")' in func("ytProjects")
    assert 'el("div","seg")' in func("modelCard")
    assert not re.search(r'\b(?:prompt|confirm)\s*\(', code)
