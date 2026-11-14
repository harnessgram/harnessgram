"""Hook drift monitoring and owner-initiated, hooks-only restoration.

No settings values/commands are exposed to the panel or Telegram. State is durable so a
heartbeat restart does not repeat an alert. Reading health never repairs settings.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import time

from . import doctor, install
from .config import private_dir, state_dir, write_private
from .i18n import t

EVERY = 300


@contextlib.contextmanager
def locked():
    base = private_dir(state_dir())
    with open(base / "hook-health.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = base / "hook-health.json"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        yield data
        write_private(path, json.dumps(data))


def snapshot():
    try:
        raw = install.settings_path().read_bytes()
    except FileNotFoundError:
        raw = b"{}"
    try:
        settings = json.loads(raw)
        if not isinstance(settings, dict):
            raise ValueError("settings object required")
        drift = doctor.hook_drift(settings)
        # All hook fields count, but unrelated preferences do not create another incident.
        relevant = json.dumps([settings.get("hooks", {}), settings.get("disableAllHooks")], sort_keys=True).encode()
    except (ValueError, TypeError):
        drift, relevant = list(install.desired_hooks()), raw
    fingerprint = hashlib.sha256(relevant).hexdigest() if drift else "healthy"
    return {"ok": not drift, "events": drift, "fingerprint": fingerprint}


def view():
    try:
        health = snapshot()
    except OSError:
        health = {"ok": False, "events": []}
    with locked() as data:
        return {"ok": health["ok"], "events": health["events"],
                "restored": bool(data.get("restored")), "reload": data.get("reload")}


def check(cfg, bot, now=None):
    now = time.time() if now is None else now
    with locked() as data:
        if now - data.get("checked", -EVERY) < EVERY:
            return
        data["checked"] = now
        health = snapshot()
        changed = data.get("fingerprint") != health["fingerprint"]
        data["fingerprint"] = health["fingerprint"]
        if not health["ok"]:
            data["restored"] = False
        if not changed or health["ok"]:
            return
        # Claim before sending: a crash after Telegram accepts must not spam the owner.
        write_private(state_dir() / "hook-health.json", json.dumps(data))
        from . import panel_pin
        name = panel_pin.known_name(cfg) or panel_pin._bot_name(cfg, bot)
        import re
        link = f"https://t.me/{name}?startapp" if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_]{3,64}", name) else ""
        lang = getattr(cfg, "ui_language", "en")
        params = {"chat_id": cfg.owner_chat_id, "text": t("hook_health_alert", lang=lang)}
        if link:
            from . import menus
            params["reply_markup"] = {"inline_keyboard": [[{"text": t("hook_health_restore", lang=lang), "url": link}], menus.close_row(lang)]}
        # Private owner message only; never inject a command into a session topic or shared chat.
        bot.call("sendMessage", params, timeout=5)


def restore():
    """Exactly install's hook merge/write, without statusLine, services or schedules."""
    with locked() as data:
        path = install.settings_path()
        try:
            old = install._read_settings(path)
        except SystemExit:
            raise ValueError("settings_unreadable") from None
        if old.get("disableAllHooks") is True:
            raise ValueError("hooks_disabled")  # changing another preference is outside this action
        try:
            new, _ = install.merge_settings(old)
        except (TypeError, AttributeError):
            raise ValueError("settings_unreadable") from None
        if old != new:
            install._write_settings(path, new)
        health = snapshot()
        if not health["ok"]:
            raise ValueError("restore_failed")
        data.update(fingerprint="healthy", restored=True, checked=0)
        return {"ok": True}
