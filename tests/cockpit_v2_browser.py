"""Deterministic phone fixture; all requests are intercepted, never sent to a bridge."""
import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace

from ctb import cockpit_v2, panel_page
import panel_browser


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((Path(__file__).parent / "cockpit_v2_must_keep.json").read_text())


def sample_state(lang="he"):
    he = lang == "he"
    names = ["תכנון", "לימוד", "ספרים", "הצוות"] if he else ["Planning", "Learning", "Books", "Team"]
    questions = ["לאשר את התכנון לשבוע הבא?", "להמשיך בהכנת חומרי הלימוד?", "להזיז את הצילום למחר?"] if he else [
        "Approve next week’s plan?", "Continue preparing the learning materials?", "Move filming to tomorrow?"]
    now = int(time.time())
    entries = [SimpleNamespace(session="sample0" + str(i), chat=1, name=name + ": task") for i, name in enumerate(names)]
    state = {"ok": True, "now": now, "pending": 0, "settings": {"ui_v2": "preview"}, "ui_v2_enabled": True,
             "sessions": [], "requests": [], "reminders": [], "cards": [], "decisions": [], "apps": [], "deliveries": []}
    for i, name in enumerate(names):
        state["sessions"].append(dict(id=entries[i].session, name=entries[i].name, state="working", age=60,
                                      now="", summary="", url="", context=0, model="", account="", bg=0))
        state["cards"].append(dict(id="progress" + str(i), session=entries[i].session, topic=entries[i].name,
                                  kind="progress", title="Progress", age=60, detail="", percent=[68, 82, 56, 74][i],
                                  finished=False, stale=i == 3, ended=False))
    for i in range(3):
        state["decisions"].append(dict(id=(str(i + 1) * 16), topic=entries[i].name, title=questions[i],
                                       detail=("המלצה: " if he else "Recommendation: ") + (
                                           "להמשיך לפי התכנון שהוכן." if he else "Continue with the prepared plan."),
                                       yes="כן" if he else "Yes", no="לא" if he else "No", age=900 if i < 2 else 400000,
                                       old=i == 2, stale="old" if i == 2 else "", url="",
                                       link=f"https://t.me/c/123/7/{40 + i}"))
    titles = ["לבדוק את נוסח ההודעה", "להכין חומרי לימוד", "לעדכן את התכנון"] if he else [
        "Review the message", "Prepare learning materials", "Update the plan"]
    for i, title in enumerate(titles):
        state["requests"].append(dict(project=names[i], id=i + 1, title=title, status="open", wait="team",
                                      age=3600, updated=now, idle=0, long=False, older=False, note="", steps=[]))
    state["requests"].append(dict(project=names[0], id=4, title="בקשה ישנה" if he else "Older request", status="open",
                                  wait="owner", age=400000, updated=now - 400000, idle=400000, long=True, older=True, note="", steps=[]))
    state["requests"].append(dict(project=names[1], id=5, title="בקשה שהושלמה" if he else "Finished request", status="done",
                                  wait="team", age=200000, updated=now - 200000, idle=200000, long=False, older=False,
                                  note="", steps=[]))
    state["reminders"] = [dict(text="סקירת שבוע עם הצוות" if he else "Weekly team review", due="10:00", age=900,
                               topic=names[0], old=False, ref="12345678:1", url=""),
                          dict(text="תזכורת ישנה" if he else "Older reminder", due="", age=1500000,
                               topic=names[0], old=True, ref="12345678:2", url="")]
    cfg = SimpleNamespace(projects=[], brief={"project_art": dict(zip(names, ["plane", "book", "books", "visor"]))})
    state["cockpit_v2"] = cockpit_v2.presentation(cfg, state, entries, now)
    return state


def needs_state(lang="he"):
    """All four waiting sources exceed the five-row preview, with unrelated requests beside them."""
    state = sample_state(lang)
    state["decisions"] = [dict(state["decisions"][0], id=f"{i:016x}") for i in range(1, 7)]
    base = state["cards"][0]
    state["cards"] += [dict(base, id=f"{i:016x}", kind="ask" if i % 2 else "choice",
                            title="לאשר את הכיוון?" if lang == "he" else "Approve this direction?",
                            options=["כן", "לא"] if lang == "he" else ["Yes", "No"])
                       for i in range(20, 27)]
    state["reminders"] = [dict(state["reminders"][0], ref=f"12345678:{i}", old=i % 2 == 0)
                          for i in range(1, 9)]
    state["requests"] += [dict(state["requests"][0], id=i, wait="owner") for i in range(10, 19)]
    state["waiting_count"] = 30  # six decisions + seven asks/choices + eight reminders + nine owner requests
    return state


def rich_state(lang="he"):
    """The Now fixture plus what the Projects, project, Tools and Settings screens show.

    Generic sample content only; every field has the shape the classic response uses.
    """
    he = lang == "he"
    state = sample_state(lang)
    now = state["now"]
    names = [s["name"].split(":", 1)[0] for s in state["sessions"]]
    extra = ["הסטודיו", "הקטלוג"] if he else ["Studio", "Catalog"]
    entries = [SimpleNamespace(session=s["id"], chat=1, name=s["name"]) for s in state["sessions"]]
    for i, name in enumerate(extra):
        sid = "sample1" + str(i)
        entries.append(SimpleNamespace(session=sid, chat=1, name=name + ": task"))
        state["sessions"].append(dict(id=sid, name=name + ": task", state="idle", age=3000 + 900 * i, now="", summary="",
                                      url="https://t.me/c/1/" + str(20 + i), context=0, model="", account="", bg=0))
        state["cards"].append(dict(id="progress1" + str(i), session=sid, topic=name + ": task", kind="progress",
                                   title="Progress", age=3000, detail="", percent=[90, 64][i], finished=False,
                                   stale=False, ended=False))
    summaries = (["המערכת יציבה ומתפתחת לפי התכנון.", "שיעורי היום הוכנו וממתינים לאישור שלך.", "פיתוח המשימות השבועיות מתקדם.",
                  "שיפור ביצועים וגרסת ניסיון חדשה.", "סרטון שבועי מוכן להפקה סופית.", "סידור הקטלוג והרחבת היכולות."] if he else
                 ["The system is stable and moving to plan.", "Today's lessons are ready for your approval.",
                  "The weekly tasks are progressing.", "Performance work and a new trial version.",
                  "The weekly video is ready for final production.", "Sorting the catalogue and adding features."])
    for i, s in enumerate(state["sessions"]):
        s["now"] = summaries[i]
        s["url"] = s["url"] or "https://t.me/c/1/" + str(10 + i)
    state["sessions"].append(dict(id="sample20", name=names[0] + ": " + ("תכן וחינוך" if he else "content"), state="working",
                                  age=120, now=("חומרי לימוד, חידונים וחוויות" if he else "Learning materials and quizzes"),
                                  summary="", url="https://t.me/c/1/30", context=0, model="", account="", bg=0))
    entries.append(SimpleNamespace(session="sample20", chat=1, name=state["sessions"][-1]["name"]))
    state["sessions"].append(dict(id="sample30", name=names[2] + ": done", state="ended", age=90000, now="", summary="",
                                  url="", context=0, model="", account="", bg=0))
    entries.append(SimpleNamespace(session="sample30", chat=1, name=state["sessions"][-1]["name"]))
    steps = (["תכנון ואפיון", "עיצוב וממשק", "פיתוח והטמעה", "בדיקות ופיילוט", "השקה והרחבה"] if he else
             ["Plan", "Design", "Build", "Pilot", "Launch"])
    state["cards"][0].update(steps=steps, at=2, percent=None,
                             title="הפרויקט בשלב הפיתוח וההטמעה, לפי התכנון." if he else "The project is in the build stage, as planned.")
    for i, label in enumerate(["אב־טיפוס למסך החדש", "תכנון החידון השבועי"] if he else ["New screen prototype", "Weekly quiz plan"]):
        state["cards"].append(dict(id="link" + str(i), session="sample00", topic=state["sessions"][0]["name"], kind="link",
                                   title=label, label=label, url="https://example.com/" + str(i), age=600 + i, detail="",
                                   finished=False, stale=False, ended=False))
    topics = [("תכנון שבועי", "decided", "הוחלט: אושרו השינויים במסך התלמיד.", ""),
              ("משימות פיתוח", "working", "", "Next: פרסום החידון להורים (מחר)."),
              ("חידון שבועי", "owner", "", ""), ("חומרי לימוד", "working", "", "")] if he else [
              ("Weekly plan", "decided", "Decided: the student screen changes are approved.", ""),
              ("Development tasks", "working", "", "Next: publish the quiz to parents (tomorrow)."),
              ("Weekly quiz", "owner", "", ""), ("Learning materials", "working", "", "")]
    state["topic_cards"] = [dict(key=[names[0], "topic", "t" + str(i)], project=names[0], ids=[10 + i], title=t[0], state=t[1],
                                 updated=now - 600 * i, decision=t[2], next=t[3], next_explicit=bool(t[3]), notes=[], prs=[])
                            for i, t in enumerate(topics)]
    state["topic_cards"].append(dict(key=[names[0], "topic", "old"], project=names[0], ids=[20], title="Old topic" if not he else "נושא שהושלם",
                                     state="done", updated=now - 90000, decision="", next="", next_explicit=False, notes=[], prs=[]))
    state["quota"] = [dict(label="Claude", weekly=72), dict(label="Codex", weekly=68), dict(label="AGY", weekly=41)]
    state["settings"].update(activity="normal", voice_digest="important", progress=True, progress_details=False,
                             voice_confirm=True, delivery_end=True, auto_account_switch=True, quota_auto_continue=False,
                             session_auto_revive=True, away=False, auto_topic_names=True, auto_compact=300, cold_compact="on",
                             radio_every_reply=True, radio_board=False, radio_room=True)
    state["default_model"] = {"id": "m1", "choices": [{"id": "m1", "key": "m1", "label": "Model A"}, {"id": "m2", "key": "m2", "label": "Model B"}]}
    state["radio"] = {"on": True, "pending": False, "failed": False, "listening": True, "listening_detection": True,
                      "listening_source": "player", "tts_minutes_today": 3, "tts_daily_minutes": 20, "tts_effective_minutes": 20}
    state["room"] = {"enabled": True, "room": None, "lengths": [5, 10, 15], "tts_minutes_today": 0, "tts_daily_minutes": 20}
    state["podcast"] = {"feeds": ["Science Hour", "שעת מדע"], "paused": False, "running": True,
                        "queue": [{"id": "a" * 16, "title": "A rather long episode title that has to wrap inside the card" if lang == "en" else "פרק עם כותרת ארוכה מאוד שצריכה לעטוף בתוך הכרטיס", "feed": "Science Hour", "seconds": 1800},
                                  {"id": "b" * 16, "title": "Second" if lang == "en" else "פרק שני", "feed": "Science Hour", "seconds": 0}],
                        "now": {"state": "playing", "id": "a" * 16, "title": "First episode" if lang == "en" else "הפרק הראשון", "feed": "Science Hour", "position": 95, "total": 1800}}
    state["youtube"] = {"setup": {"client": True, "base": True}, "connected": True, "live": True, "since": now - 600,
                        "channel": "Sample", "enabled": True, "live_enabled": True, "hours_left": 3}
    state["hook_health"] = {"ok": True}
    state["apps"] = [{"name": "studio", "title": "Studio", "title_he": "סטודיו", "icon": "film"},
                     {"name": "sample", "title": "Sample", "title_he": "דוגמה", "icon": "box"}]
    cfg = SimpleNamespace(projects=[], brief={"project_art": dict(zip(names + extra, ["plane", "book", "books", "visor", "clapper", "catalog"]))})
    for d in state["decisions"]:
        for key in ("project_key", "project_name", "project_asset"):
            d.pop(key, None)
    state["cockpit_v2"] = cockpit_v2.presentation(cfg, state, entries, now)
    return state


def crowded_state(lang="he"):
    """The rich fixture plus what a busy owner really has: raw slug names, no art mapping, quiet projects.

    Raw session-title prefixes ("sample-809-audit-exception-renewal") become project names when no
    configured project matches; with no art mapping the bridge picks their scene itself. Generic text only.
    """
    state = rich_state(lang)
    now = state["now"]
    entries = [SimpleNamespace(session=s["id"], chat=1, name=s["name"]) for s in state["sessions"]]
    extra = [("sample-809-audit-exception-renewal", "working", 400, 35), ("app-store-review-watch", "idle", 5000, None),
             ("nightly_report_builder", "idle", 3 * 86400, None), ("old-landing-page", "idle", 5 * 86400, None),
             ("archive-cleanup", "idle", 9 * 86400, None), ("weekly-digest", "idle", 2 * 86400, 80)]
    for i, (name, st, age, pct) in enumerate(extra):
        sid = "sample4" + str(i)
        entries.append(SimpleNamespace(session=sid, chat=1, name=name + ": task"))
        state["sessions"].append(dict(id=sid, name=name + ": task", state=st, age=age, now="", summary="",
                                      url="https://t.me/c/1/" + str(40 + i), context=0, model="", account="", bg=0))
        if pct is not None:
            state["cards"].append(dict(id="progress4" + str(i), session=sid, topic=name + ": task", kind="progress",
                                       title="Progress", age=age, detail="", percent=pct, finished=False, stale=False, ended=False))
    names = [s["name"].split(":", 1)[0] for s in state["sessions"][:6]]
    cfg = SimpleNamespace(projects=[], brief={"project_art": dict(zip(names, ["plane", "book", "books", "visor", "clapper", "catalog"]))})
    for d in state["decisions"]:
        for key in ("project_key", "project_name", "project_asset"):
            d.pop(key, None)
    state["cockpit_v2"] = cockpit_v2.presentation(cfg, state, entries, now)
    return state


def knowledge_sample(lang="he"):
    """A deterministic "What the system knows" view (the shape knowledge.view returns). Generic sample text only."""
    he = lang == "he"
    now = int(time.time())
    T = (lambda h, e: h if he else e)
    rules = [
        (T("שיחה איתך", "Talking with you"), T("סיכום קצר בסוף", "Short summary at the end"),
         T("בסוף כל תשובה, משפט אחד או שניים במילים פשוטות", "One or two plain sentences at the end of every reply"), True),
        (T("שיחה איתך", "Talking with you"), T("שאלה אחת בכל פעם", "One question at a time"),
         T("כשצריך אותך, שאלה אחת וההמלצה ראשונה", "When the team needs you: one question, recommendation first"), True),
        (T("החלטות", "Decisions"), T("פועלים לפי ההמלצה", "Act on the recommendation"),
         T("הצוות מחליט בעצמו ומדווח לך מה נעשה", "The team decides and reports what it did"), True),
        (T("החלטות", "Decisions"), T("עבודה בלילה", "Night work"),
         T("הצוות ממשיך לעבוד גם כשאתה ישן", "The team keeps working while you sleep"), False),
        (T("קוד", "Code"), T("תצוגה לטלפון לפני עלייה", "Phone preview before release"),
         T("כל שינוי שרואים במסך נשלח אליך לבדיקה קודם", "Every visible change comes to your phone first"), True),
        (T("מחשב", "Computer"), T("בדיקת זיכרון לפני עבודה כבדה", "Check memory before heavy work"),
         T("לפני עבודה כבדה בודקים שיש מספיק מקום", "Check there is room before heavy work"), True),
        (T("תוכן", "Content"), T("גיוון בתכנים", "Vary the content"),
         T("כל תוכן חוזר מקבל צורה חדשה ויצירתית", "Every recurring piece gets a fresh, creative form"), True),
    ]
    locked = [(T("סודות לעולם לא בצ׳אט", "Secrets never in chat"), T("סיסמאות ומפתחות נשמרים רק בכספת של המחשב", "Passwords and keys stay in the computer's vault")),
              (T("אין הודעות לאנשים בלי אישור", "No messages to people without approval"), T("שום דבר לא נשלח בשמך בלי שאמרת כן", "Nothing is sent in your name without your yes")),
              (T("אין תשלום בלי אישור", "No payment without approval"), T("לא משתמשים בשירות בתשלום בלי הסכמה שלך", "No paid service without your consent")),
              (T("כל שרת שייך לפרויקט אחד", "One server per project"), T("כדי שתקלה בפרויקט אחד לא תפגע באחר", "So a fault in one project never hurts another")),
              (T("לא מחלישים הגנות", "Never weaken protections"), T("לא עוקפים הרשאות כדי להתקדם מהר", "Permissions are never bypassed to move faster")),
              (T("תוכן מתאים לילדים", "Child-suitable content"), T("כל מה שילדים והורים רואים נבדק קודם", "Everything children and parents see is checked first"))]
    items = []

    def add(kind, ident, title, summary, group, on=True, **extra):
        items.append(dict({"id": ident, "kind": kind, "title": title, "summary": summary, "text": summary + (
            T(". ההמלצה שלו מופיעה ראשונה.", ". Its recommendation comes first.") if ident.endswith("one-question") else ""),
            "group": group, "agents": ["claude", "codex", "agy"], "source": {"path": "~/.agents/RULES.md", "line": 12 + len(items),
            "section": group}, "date": now - 20 * 86400, "locked": False, "on": on, "connected": on}, **extra))
    for i, (group, title, summary, on) in enumerate(rules):
        add("rule", "rule:%d.%s" % (i, "one-question" if i == 1 else "r%d" % i), title, summary, group, on)
    for i, (title, summary) in enumerate(locked):
        add("rule", "rule:safety.l%d" % i, title, summary, T("בטיחות", "Safety"), locked=True)
    for i, (title, summary) in enumerate([(T("השבוע מתוכנן לפני שבת", "The week is planned before the weekend"), T("תכנון השבוע נסגר מראש", "The week's plan is closed ahead")),
                                          (T("תמונות מקוריות קודם", "Real photos first"), T("צילומים אמיתיים לפני תמונות שנוצרו", "Real photos before generated images")),
                                          (T("קיצור דרך לתצוגה", "Preview shortcut"), T("קישור התצוגה נשלח בכל שלב", "The preview link is sent at every step"))]):
        add("memory", "memory:sample/m%d" % i, title, summary, T("עבודה כללית", "General work"), i != 2, agents=["claude"])
    for i, name in enumerate(["Drive", "Sheets", "Calendar", "Maps"]):
        add("tool", "tool:" + name.lower(), name, "", "claude, codex", i != 3, connected=i != 3, native_off=[])
    for i, (title, summary) in enumerate([(T("מדריך שחרור גרסה", "Release guide"), T("איך גרסה עולה, צעד אחר צעד", "How a version ships, step by step")),
                                          (T("מדריך שרתים", "Server guide"), T("איפה כל שירות רץ", "Where each service runs"))]):
        add("doc", "doc:guides/d%d" % i, title, summary, "~/guides")
    add("skill", "skill:letter", T("מכתב רשמי בדקה", "A formal letter in a minute"),
        T("כותבים כמה מילים, והסוכן מחזיר מכתב מסודר בעברית תקנית.", "Write a few words and get back a tidy, correct letter."), "~/.codex/skills", False, internal=True)
    add("skill", "skill:summary", T("סיכום פגישה", "Meeting summary"), T("סיכום קצר של כל פגישה", "A short summary of every meeting"), "~/.codex/skills", internal=True)
    counts = {k: {"total": 0, "on": 0} for k in ("rule", "memory", "skill", "tool", "doc")}
    for it in items:
        counts[it["kind"]]["total"] += 1
        counts[it["kind"]]["on"] += 1 if it["on"] else 0
    history = [dict(id="a1b2c3d4e5f6", at=now - 1800, action="toggle", item="rule:3.r3", kind="rule", title=rules[3][1], actor="owner",
                    origin="cockpit", before=True, after=False, undone=False, undo_id=""),
               dict(id="a1b2c3d4e5f7", at=now - 2 * 3600, action="suggest", item="memory:sample/m0", kind="memory",
                    title=items[13]["title"], actor="owner", origin="cockpit", undone=False, undo_id=""),
               dict(id="a1b2c3d4e5f8", at=now - 86400 - 3600, action="toggle", item="rule:0.r0", kind="rule", title=rules[0][1],
                    actor="owner", origin="cockpit", before=False, after=True, undone=False, undo_id=""),
               dict(id="a1b2c3d4e5f9", at=now - 86400 - 7200, action="toggle", item="skill:letter", kind="skill",
                    title=items[-2]["title"], actor="owner", origin="cockpit", before=True, after=False, undone=False, undo_id=""),
               dict(id="a1b2c3d4e5fa", at=now - 4 * 86400, action="toggle", item="tool:maps", kind="tool", title="Maps",
                    actor="owner", origin="cli", before=True, after=False, undone=True, undo_id="a1b2c3d4e5fb")]
    return {"ok": True, "now": now, "scanned": now - 120, "items": items, "counts": counts, "locked": len(locked),
            "history": history, "week": 4, "overlay": "~/.agents/knowledge/off.md"}


def knowledge_notes_sample():
    """The view as the bridge sends it when the sources are English and an agent wrote plain-words notes: every item has
    a Hebrew and an English note except one rule ("Night work") and the tools, which show their raw words."""
    he, en = knowledge_sample("he"), knowledge_sample("en")
    advice = {"rule:3.r3": None, "rule:4.r4": "review", "rule:6.r6": "optional", "memory:sample/m2": "optional",
              "skill:letter": "review"}
    for h, e in zip(he["items"], en["items"]):
        if e["kind"] == "tool" or e["id"] == "rule:3.r3":
            continue
        why = {"keep": ("שומר על העבודה מסודרת ובטוחה", "Keeps the work orderly and safe"),
               "optional": ("נוח, אבל העבודה ממשיכה גם בלעדיו", "Handy, but the work goes on without it"),
               "review": ("כדאי לוודא שזה עדיין מתאים לך", "Worth checking it still fits you")}
        a = advice.get(e["id"]) or "keep"
        e["note"] = {"title_he": h["title"], "summary_he": h["summary"], "title_en": e["title"], "summary_en": e["summary"],
                     "advice": a, "why_he": why[a][0], "why_en": why[a][1]}
        e["title"], e["summary"] = e["title"].upper() if e["kind"] == "rule" else e["title"], e["summary"]
    groups = {}
    for h, e in zip(he["items"], en["items"]):
        if e["kind"] in ("rule", "memory"):
            groups[e["group"]] = {"title_he": h["group"], "title_en": e["group"]}
    groups.pop("Computer", None)  # one heading without a note keeps its raw words
    en["sections"] = groups
    return en


def knowledge_reply(view, body):
    """The fixture's answer to one /api/knowledge call; switches and undo change the sample like the bridge would."""
    action = body.get("action")
    if action in ("view", "scan"):
        return 200, view
    if action == "toggle":
        item = next((i for i in view["items"] if i["id"] == body.get("id")), None)
        if item is None:
            return 404, {"error": "unknown"}
        if item["locked"]:
            return 409, {"error": "locked"}
        entry = dict(id="%012x" % (len(view["history"]) + 1000), at=int(time.time()), action="toggle", item=item["id"],
                     kind=item["kind"], title=item["title"], actor="owner", origin="cockpit", before=item["on"], after=body["on"])
        item["on"] = item["connected"] = body["on"]
        view["history"].insert(0, dict(entry, undone=False, undo_id=""))
        return 200, {"ok": True, "entry": entry}
    if action == "undo":
        row = next((r for r in view["history"] if r["id"] == body.get("log")), None)
        if row is None:
            return 404, {"error": "unknown"}
        item = next(i for i in view["items"] if i["id"] == row["item"])
        item["on"] = item["connected"] = row["before"]
        row["undone"] = True
        return 200, {"ok": True, "entry": {"id": "%012x" % (len(view["history"]) + 2000), "of": row["id"], "after": row["before"]}}
    if action == "suggest":
        return 200, {"ok": True, "entry": {"id": "%012x" % 3000, "action": "suggest"}}
    return 400, {"error": "bad"}


def schedule_sample(lang="he"):
    """A Today/Tomorrow view as the bridge builds it: a late item, an all-day event, timed items and a long name."""
    long = ("להתקשר לרופא המשפחה ולבקש תור לבדיקות דם שנתיות לפני החג" if lang == "he"
            else "Call the family doctor and ask for an appointment for the yearly blood tests before the holiday")
    def row(i, title, **kw):
        return dict({"id": ("%012x" % i), "title": title, "time": None, "done": False, "kind": "own", "late": False, "allday": False}, **kw)
    today = [row(1, "Family day" if lang == "en" else "יום משפחה", id="", kind="cal", allday=True),
             row(2, "Team sync" if lang == "en" else "סנכרון צוות", id="", kind="cal", time="10:00"),
             row(3, long, time="14:30"), row(4, "Pay the bill" if lang == "en" else "לשלם חשבון", done=True, time="16:00"),
             row(5, "Buy milk" if lang == "en" else "לקנות חלב")]
    return {"today": "2026-11-15", "tomorrow": "2026-11-16", "days": {"today": today, "tomorrow": []},
            "open": {"today": 3, "tomorrow": 0}, "calendar": {"configured": True, "ok": True}}


def family_sample(lang="he", shabbat=False):
    """A family-board view as the bridge builds it: the starting lists, Wednesday, one chore ticked, a long name."""
    from ctb import family_board
    lists = family_board.defaults()
    def shown(rows, done=()):
        return [{"id": r["id"], "text": r["text"], "day": r["day"], "done": r["text"] in done} for r in rows]
    days = [{"day": d, "date": "2026-11-%02d" % (15 + d), "chores": shown([r for r in lists["weekly"] if r["day"] == d])} for d in range(7)]
    meals = [{"id": "%012x" % 90, "text": "שניצל ואורז", "day": 2, "done": False}]
    return {"today": "2026-11-18", "week_start": "2026-11-15", "day": 6 if shabbat else 3, "shabbat": shabbat, "shabbat_until": "17:41" if shabbat else None, "days": days,
            "daily": shown(lists["daily"], done=("כלים",)), "shop": shown(lists["shop"], done=("מוצרי חלב",)), "meals": meals,
            "open": {"chores": 1, "daily": 4}, "caps": {"text": 100}}


def workmap_sample(lang="he"):
    """The work-map lines and today's briefing as the bridge builds them: a long name, mixed languages, every status."""
    he = lang == "he"
    long = ("לאשר את נוסח ההודעה להורים לפני שליחתה אל כל הכיתות ולבדוק שההוראות ברורות לכולם" if he else
            "Approve the wording of the message to parents before it goes out to every class and check that it is clear")
    impact = "ההודעה יוצאת היום ולא מחר" if he else "The message goes out today instead of tomorrow"
    lines = [dict(what=long, status="waiting", impact=impact, source="ledger"),
             dict(what="Pick a caption style", status="waiting", impact=impact, source="ledger")]
    lines += [dict(what=("בונים את מסך המסננים %d" % i) if he else "Building the filters screen %d" % i, status="working",
                   impact="יהיה לך משהו חדש להשתמש בו." if he else "You get something new to use.", source="agent") for i in range(1, 5)]
    lines += [dict(what="Old intro removed", status="done", impact="הסתיים. לא צריך ממך כלום." if he else "Finished.", source="ledger"),
              dict(what="Fix the crash on the order page", status="done", impact="Fixed", source="ledger")]
    morning = {"date": "2026-11-15", "built": 0, "lang": lang, "schedule": [{"time": "10:00", "title": "Team sync"}, {"time": "", "title": long}],
               "schedule_more": 0, "chores": ["Take out the trash"], "overnight": [{"project": "Studio", "what": "Landed overnight"}],
               "overnight_more": 0, "waiting": [{"project": "Studio", "what": long, "impact": impact}], "waiting_more": 0}
    return lines, morning


def addons_reply(view, body):
    """The add-ons route the way panel._addons answers, on an in-memory copy of addons.view."""
    rows = {i["key"]: i for g in view["groups"] for i in g["items"]}
    if body.get("action") == "view":
        return 200, view
    row = rows.get(body.get("key"))
    if row is None:
        return 404, {"error": "unknown"}
    if row["locked"]:
        return 409, {"error": "locked"}
    previous, row["on"] = row["on"], body["on"]
    return 200, {"ok": True, "key": row["key"], "on": row["on"], "previous": previous, "item": row}


def addons_sample():
    from ctb import addons
    from ctb.config import Config
    return addons.view(Config())


def open_v2(browser, lang="he", scheme="light", state=None, width=390, knowledge=None, schedule=None, addons=None, family=None, height=844, start="",
            native=False, stub_extra="", ready=None):
    state = copy.deepcopy(state if state is not None else sample_state(lang))
    calls = []
    kview = knowledge if knowledge is not None else knowledge_sample(lang)
    aview = addons if addons is not None else addons_sample()
    fview = family if family is not None else (lambda path, body: (200, {"ok": True, "view": family_sample(lang)}))
    sview = schedule if schedule is not None else (lambda path, body: (200, {"ok": True, "view": schedule_sample(lang)}))
    ctx = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1,
                              locale="he-IL" if lang == "he" else "en-US", color_scheme=scheme)
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    stub = panel_browser.STUB % json.dumps("owner-fixture")
    # This is a page fixture, not a source of authentication evidence. HTTP auth
    # is separately tested against the real PanelHandler.
    stub = stub.replace('colorScheme: "light"', 'colorScheme: ' + json.dumps(scheme))
    stub += '\nwindow.Telegram.WebApp.close=function(){window.__closed=true;};'
    stub += stub_extra  # extra Telegram fields a test needs (cockpit_native)

    def route(request):
        url = request.request.url
        path = url.split("127.0.0.1", 1)[-1].split("?", 1)[0]
        if "telegram.org/js/telegram-web-app.js" in url:
            request.fulfill(content_type="application/javascript", body=stub)
        elif path.startswith(cockpit_v2.ASSET_PREFIX):
            name = path.rsplit("/", 1)[-1]
            request.fulfill(content_type="image/webp", body=(cockpit_v2.ASSET_ROOT / name).read_bytes())
        elif path == "/api/state":
            request.fulfill(json=state)
        elif path == "/api/app-token":
            panel_browser.app_token_reply(request)
        elif path == "/app/studio/api":
            from ctb.studio import provider
            request.fulfill(json={"ok": True, "desk": provider.sample()})
        elif path.startswith("/app/studio/"):
            from ctb.studio import page as studio_page
            request.fulfill(content_type="text/html", body=studio_page.render_page())
        elif path == "/api/radio-player":
            request.fulfill(json={"enabled": True, "on": True, "token": "fixture", "expires": int(time.time()) + 3600})
        elif path.startswith("/api/"):
            body = request.request.post_data_json
            if path not in ("/api/schedule-view", "/api/family-view", "/api/request-detail"):  # reads, not user actions
                calls.append((path, body))
            if path == "/api/setting":
                state["settings"][body["name"]] = body["value"]
                if body["name"] == "ui_v2":
                    state["ui_v2_enabled"] = body["value"] in ("on", "preview")
                request.fulfill(json={"ok": True, "settings": state["settings"]})
            elif path == "/api/request-detail":
                row = next(r for r in state["requests"] if (r["project"], r["id"]) == (body["project"], body["id"]))
                request.fulfill(json={"ok": True, "title": row["title"], "note": "", "steps": []})
            elif path == "/api/request":
                state["requests"] = [r for r in state["requests"] if (r["project"], r["id"]) != (body["project"], body["id"])]
                request.fulfill(json={"ok": True})
            elif path == "/api/briefing-dismiss":
                state["morning"] = None
                request.fulfill(json={"ok": True})
            elif path == "/api/dismiss":
                state["decisions"] = [d for d in state["decisions"] if d["id"] not in body["ids"]]
                request.fulfill(json={"ok": True, "closed": body["ids"]})
            elif path == "/api/topic-cleanup":
                rows = state.get("topic_cleanup", {}).get("topics", [])
                if body["action"] in ("keep", "close"):
                    state["topic_cleanup"]["topics"] = [r for r in rows if r["id"] not in body["ids"]]
                request.fulfill(json={"ok": True, "closed_ids": body["ids"] if body["action"] == "close" else [],
                                      "reopened": len(body["ids"]) if body["action"] == "reopen" else 0})
            elif path in ("/api/schedule-view", "/api/schedule"):
                status, reply = sview(path, body)
                request.fulfill(status=status, json=reply)
            elif path in ("/api/family-view", "/api/family"):
                status, reply = fview(path, body)
                request.fulfill(status=status, json=reply)
            elif path == "/api/knowledge":
                status, reply = knowledge_reply(kview, body)
                request.fulfill(status=status, json=reply)
            elif path == "/api/addons":
                status, reply = addons_reply(aview, body)
                request.fulfill(status=status, json=reply)
            elif path == "/api/decide":
                state["decisions"] = [d for d in state["decisions"] if d["id"] != body["id"]]
                request.fulfill(json={"ok": True, "delivery": "queued", "key": "fixture"})
            else:
                request.fulfill(json={"ok": True, "cleared": 1, "restored": 1})
        elif path == "/":
            request.fulfill(content_type="text/html", body=panel_page.render_page(lang, "fixture", native=native))
        else:
            request.fulfill(status=404, body="")

    page.route("**/*", route)
    page.knowledge = kview
    page.addons = aview
    page.goto("http://127.0.0.1/" + ("?tgWebAppStartParam=" + start if start else ""))
    page.locator(ready or (".v2-needs" if state.get("ui_v2_enabled") else "#tabs .tab")).first.wait_for()
    return ctx, page, calls, errors, state


def open_first_request(page):
    """Open "Agents are on it" and its first row; the same path an owner takes."""
    fold = page.locator('[data-v2-section="requests"] [data-block="requests-agents"]')
    fold.locator(":scope > .compact-toggle").click()
    page.wait_for_timeout(700)
    row = fold.locator(".request-row").first
    row.locator(".request-toggle").click()
    return row


def assert_contract(page, state):
    for requirement in (r for r in CONTRACT if r.get("screen", "now") == "now"):
        assert page.locator(requirement["selector"]).count(), requirement["id"]
    zone = page.locator('[data-v2-section="decisions"]')
    assert page.locator('#app > [data-v2-section]').first.get_attribute("data-v2-section") == "decisions"
    assert page.locator("#app > *").nth(1).get_attribute("data-v2-section") == "decisions"
    ids = [d["id"] for d in state["decisions"]] + [c["id"] for c in state["cards"] if c["kind"] in ("ask", "choice") and not c.get("ended")]
    assert sorted(zone.locator("[data-v2-decision]").evaluate_all("es => es.map(e=>e.dataset.v2Decision)")) == sorted(ids)
    for ident in ids:
        assert zone.locator('[data-v2-decision="%s"]' % ident).is_visible()
    for decision in state["decisions"]:
        row = zone.locator('[data-v2-decision="%s"]' % decision["id"])
        assert row.locator('[data-v2-answer="yes"]').is_visible()
        assert row.locator('[data-v2-answer="no"]').is_visible()
    for actions in page.locator('[data-v2-section="requests"] [data-block="requests-agents"] .request-actions').all():
        assert actions.locator("button").count() == 3
    # Items untouched for a week fold away and offer keep-open and drop instead.
    for actions in page.locator('[data-v2-section="dormant"] .request-actions').all():
        assert actions.locator("button").count() == 2
    # Rows are compact lines; a tap opens one and shows its three buttons at a finger-sized height.
    row = open_first_request(page)
    for button in page.locator('[data-v2-section="requests"] .request-row.open .request-actions button').all():
        assert button.is_visible() and button.bounding_box()["height"] >= 44
    row.locator(".request-toggle").click()  # back out, so the way-out button closes the app and not a layer
    page.locator('[data-v2-section="requests"] [data-block="requests-agents"] > .compact-toggle').click()
    page.wait_for_timeout(700)
    assert page.locator('#tabs [role="tab"]').count() == 4
    assert page.evaluate("document.documentElement.scrollWidth <= 390")
    assert page.locator("html").get_attribute("dir") == ("rtl" if page.locator("html").get_attribute("lang") == "he" else "ltr")


def assert_screen_contract(page, screen):
    """The must-keep items of one milestone-2 screen, plus the shell rules every screen keeps."""
    for requirement in (r for r in CONTRACT if r.get("screen") == screen):
        assert page.locator(requirement["selector"]).count(), requirement["id"]
    if screen == "settings":
        assert page.locator("[data-v2-section^='settings-'] > .compact-toggle").count() == 5
    if screen == "tools-radio":
        # One audio element and one player box, now inside the Radio tool.
        assert page.locator("audio").count() <= 1 and page.locator("#radio-player").count() == 1
    assert page.locator('#tabs [role="tab"]').count() == 4
    assert page.locator("#v2-exit").is_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    small = page.evaluate("""Array.from(document.querySelectorAll('#app button,#tabs button')).filter(b=>{
        const r=b.getBoundingClientRect();return r.width&&r.height&&getComputedStyle(b).visibility!=='hidden'&&r.height<44&&!b.closest('.compact-reveal,.srow,.topic-card')}).map(b=>b.className+':'+b.textContent.trim().slice(0,20))""")
    assert not small, (screen, small)
