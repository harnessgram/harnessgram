import json
from datetime import datetime, timezone
from types import SimpleNamespace

from ctb import owner_requests, panel, panel_pin


def ts(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


def config(folder, timezone_name="UTC"):
    return SimpleNamespace(
        brief={"ledger_dir": str(folder)}, radio={}, timezone=timezone_name,
        bot_token="123:fake", ui_language="he",
    )


def write_ledger(folder, items):
    (folder / "owner.json").write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")


def item(ident, text, status, wait, created, updated=None):
    return {"id": ident, "text": text, "status": status, "wait": wait,
            "created": created, "updated": created if updated is None else updated}


def widget(monkeypatch, cfg, now):
    monkeypatch.setattr(panel, "radio_player_on", lambda unused: False)
    monkeypatch.setattr("ctb.waiting.waiting_count", lambda unused: 4)
    monkeypatch.setattr(panel_pin, "known_name", lambda unused: "OwnerBot")
    monkeypatch.setattr(owner_requests, "_LINK_SECRET", b"k" * 32)
    rows = owner_requests.read(now, owner_requests.ledger_dir(cfg), history=True,
                               dormant_after=owner_requests.dormant_seconds(cfg))
    return panel.widget_view(cfg, {"now": now, "ownerToday": panel._widget_today(cfg, now, rows)})


def test_today_shape_limits_and_existing_widget_fields(monkeypatch, tmp_path):
    now = ts("2026-11-17T12:00:00")
    long_hebrew = "רעיון חשוב מאוד " * 8
    write_ledger(tmp_path, [
        item(1, "הושלם היום", "done", "team", now - 8000, now - 60),
        item(2, long_hebrew, "open", "owner", now - 60),
        item(3, "שני", "open", "owner", now - 120),
        item(4, "שלישי", "open", "owner", now - 180),
        item(5, "רביעי", "open", "owner", now - 240),
        item(6, "נוסף היום", "open", "team", now - 300),
    ])

    out = widget(monkeypatch, config(tmp_path), now)

    assert out["today"]["done_today"] == 1
    assert out["today"]["total_today"] == 6
    assert [row["title"] for row in out["today"]["waiting"]][1:] == ["שני", "שלישי"]
    assert all(set(row) == {"title", "age_minutes", "url"} for row in out["today"]["waiting"])
    assert len(out["today"]["waiting"][0]["title"]) <= 60
    assert out["today"]["waiting"][0]["title"].startswith("רעיון חשוב")
    assert out["today"]["waiting"][0]["url"].startswith("https://t.me/OwnerBot?startapp=r")
    assert {key: type(out[key]) for key in out if key != "today"} == {
        "ok": bool, "now": float, "bot_id": None.__class__, "waiting_count": int,
        "appSwitches": dict, "decisions": list, "sessions": list, "cards": list, "radio_player": dict,
    }


def test_today_uses_configured_local_day_boundary(monkeypatch, tmp_path):
    now = ts("2026-11-17T00:30:00")
    write_ledger(tmp_path, [
        item(1, "UTC yesterday, local today", "done", "team", now - 3600, ts("2026-11-16T23:45:00")),
        item(2, "Local yesterday", "done", "team", now - 90000, ts("2026-11-16T21:59:00")),
        item(3, "Added before UTC midnight", "open", "team", ts("2026-11-16T23:30:00")),
    ])

    today = widget(monkeypatch, config(tmp_path, "Asia/Jerusalem"), now)["today"]

    assert today == {"done_today": 1, "total_today": 2, "waiting": []}


def test_today_empty_ledger_is_present(monkeypatch, tmp_path):
    out = widget(monkeypatch, config(tmp_path), ts("2026-11-17T12:00:00"))
    assert out["today"] == {"done_today": 0, "total_today": 0, "waiting": []}


def test_owner_request_deep_link_uses_the_shared_panel_helper(monkeypatch):
    monkeypatch.setattr(owner_requests, "_LINK_SECRET", b"k" * 32)
    seen = []
    monkeypatch.setattr(panel_pin, "app_url", lambda cfg, param: seen.append(param) or "panel-link")
    assert owner_requests.item_url(object(), "גשר", 367) == "panel-link"
    assert seen == ["r" + owner_requests.item_key("גשר", 367)]


def test_link_keys_need_the_installation_secret(monkeypatch, tmp_path):
    """Without the installation's own key a link key cannot be matched to a guessed project name."""
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(owner_requests, "_LINK_SECRET", None)
    first = owner_requests.item_key("גשר", 367)
    key_file = tmp_path / "state" / "request-links.key"
    assert key_file.stat().st_mode & 0o077 == 0
    monkeypatch.setattr(owner_requests, "_LINK_SECRET", None)
    assert owner_requests.item_key("גשר", 367) == first  # the same key on the next start
    monkeypatch.setattr(owner_requests, "_LINK_SECRET", b"x" * 32)
    assert owner_requests.item_key("גשר", 367) != first
    import hashlib
    assert first != hashlib.sha256("גשר\0367".encode()).hexdigest()[:16]


def test_two_processes_creating_the_key_agree(monkeypatch, tmp_path):
    """The key appears whole or not at all: a second start reads the first one's key, never a half-written file."""
    monkeypatch.setenv("CTB_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(owner_requests, "_LINK_SECRET", None)
    first = owner_requests._link_secret()
    monkeypatch.setattr(owner_requests, "_LINK_SECRET", None)
    assert owner_requests._link_secret() == first
    assert sorted(p.name for p in (tmp_path / "state").iterdir() if "request-links" in p.name) == ["request-links.key"]


def test_a_request_link_opens_the_home_view():
    from ctb import panel_page
    # once: the owner can still switch tabs afterwards, and the row is opened when it is drawn
    assert 'requestTargetPending===true){view="all";requestTargetPending="shown";resolveRequestTarget();}' in panel_page.PAGE
    assert 'post("/api/request-target",{key:requestTargetKey})' in panel_page.PAGE
    assert "data-request-deep" not in panel_page.PAGE
    # project names may hold quotes or backslashes: the row is found by comparing attributes, never via a selector
    assert 'n.getAttribute("data-request")===requestTargetRow' in panel_page.PAGE
    assert "[data-request=\"'+requestTargetRow" not in panel_page.PAGE


def test_list_rows_carry_no_link_key_and_the_server_resolves_it(monkeypatch):
    """The bounded list payload stays as it was; the one row a link names is found on the server."""
    monkeypatch.setattr(owner_requests, "_LINK_SECRET", b"k" * 32)
    rows = [dict(project="גשר", id=i, title="t", status="open", wait="owner", age="1h", updated=0, created=0)
            for i in (366, 367)]
    assert all("deep" not in r for r in owner_requests.slim(rows, 10.0))
    key = owner_requests.item_key("גשר", 367)
    assert owner_requests.resolve_key(rows, key) == ("גשר", 367)
    for bad in ("0" * 16, key.upper(), key[:-1], None, 367, "r" + key):
        assert owner_requests.resolve_key(rows, bad) is None


def test_request_target_is_an_owner_read_route():
    from ctb import panel
    assert panel.ACTIONS["/api/request-target"] == "_owner_request_target"
    assert panel.ROUTES["/api/request-target"] <= 256
