"""The radio's summaries in the alerts channel: one silent message per spoken update, through alerts.alert."""
import threading
import time

from conftest import FakeBot
from ctb import alerts as A, radio, radio_post as P, radio_script as R
from ctb.telegram import TelegramError
from test_alerts_settings import CHANNEL, choose, make_cfg, register, to_channel
from test_radio import BACKLOG, FRESH, NAMES, NOW, dec, eng, only

CLOCK = "14:30"
LINE_PAD = P.LINE_CHARS - 5


def rundown(lang="he", **kw):
    rep = {}
    snap = only(**kw)
    snap["working"], snap["idle"] = 2, 1
    snap["quota"] = [{"label": "Claude", "window": "week", "pct": 91}]
    R.build_script(snap, {}, NAMES, lang, when=time.localtime(0), report=rep)
    return rep


def sent(bot):
    return [p["text"] for p in to_channel(bot)]


def sample_rundown():
    ledger = [(1, "קוד", FRESH)]
    ds = [dec(f"n{n}", f"שאלה חדשה {n}?", FRESH - n) for n in range(5)]
    return rundown(decisions=ds + BACKLOG, done=[(8, "הסרטון עלה ללוח הטיסות", FRESH), (9, "ב", FRESH - 1),
                                                (10, "ג", FRESH - 2)])


def test_a_rundown_is_one_silent_html_message_with_sections_in_order():
    register()
    cfg, bot, state = make_cfg(), FakeBot(), {}
    assert P.post(cfg, sample_rundown(), "he", state, bot=bot, clock=CLOCK)
    (call,) = to_channel(bot)
    assert call["disable_notification"] is True and call["parse_mode"] == "HTML"
    body = call["text"]
    print("\n--- rundown (he)\n" + body)
    plain = body.replace("\u2068", "").replace("\u2069", "")
    heads = [l for l in body.split("\n") if l.startswith("<b>")]
    assert heads == ["<b>📻 רדיו הצוות · ⁨14:30⁩</b>", "<b>🔔 חדש שמחכה לך</b>", "<b>✅ הושלם</b>", "<b>👥 הצוות</b>",
                     "<b>⚠️ מכסות</b>"]
    assert "• החלטה בקוקפיט, אורה: מיזוג: שאלה חדשה 0?" in plain and "• ועוד שניים חדשים." in plain
    assert "<b>✅ הושלם</b>\n• מסלול: הסרטון עלה ללוח הטיסות.\n• מסלול: ב.\n• ועוד אחד." in plain
    assert body.rstrip().endswith("</i>") and "<i>חוץ מזה מחכים בקוקפיט חמישה דברים ישנים יותר.</i>" in body
    assert "החלטה ישנה" not in body and len(body) <= A.PART_CHARS


def test_the_english_mirror():
    register()
    bot = FakeBot()
    rep = rundown("en", decisions=[dec("n1", "Approve?", FRESH)] + BACKLOG[:2])
    assert P.post(make_cfg(), rep, "en", {}, bot=bot, clock=CLOCK)
    body = sent(bot)[0]
    print("\n--- rundown (en)\n" + body)
    assert body.startswith("<b>📻 Team radio · 14:30</b>") and "<b>🔔 New and waiting for you</b>" in body
    assert "<b>👥 Team</b>" in body and "<b>⚠️ Quotas</b>" in body
    assert "<i>Besides that, 2 older items still wait in the cockpit.</i>" in body


def test_a_news_update_and_a_reply_bulletin():
    register()
    bot = FakeBot()
    before = only(done=[], sessions=[{"id": "s1", "name": "תיקון הגשר", "state": "working"}])
    last = R.remember(before, NOW - 600)
    after = only(done=[(5, "הסרטון עלה", FRESH)], sessions=[{"id": "s1", "name": "תיקון הגשר", "state": "idle"}])
    rep = {}
    R.build_news(after, last, NAMES, "he", report=rep)
    assert P.post(make_cfg(), rep, "he", {}, bot=bot, clock=CLOCK)
    news = sent(bot)[0]
    print("\n--- news (he)\n" + news)
    assert "עדכון קצר" in news and "<b>👥 הצוות</b>\n• הסשן תיקון הגשר סיים לעבוד וממתין." in news
    assert "<b>✅ הושלם</b>\n• מסלול: הסרטון עלה." in news

    records = [{"name": "מסלול: מסך הבית", "say": "סיימתי את המסך ומחכה לאישור שלך.", "text": "x"},
               {"name": "אורה", "say": "המיזוג עבר בהצלחה.", "text": "y"}]
    assert P.post(make_cfg(), P.bulletin_report(records), "he", {}, bot=bot, clock=CLOCK)
    bulletin = sent(bot)[1]
    print("\n--- bulletin (he)\n" + bulletin)
    assert bulletin == ("<b>📻 רדיו הצוות · ⁨14:30⁩</b>\n\n<b>💬 עדכונים מהצוות</b>\n"
                        "• מסלול: מסך הבית: סיימתי את המסך ומחכה לאישור שלך.\n• אורה: המיזוג עבר בהצלחה.")
    assert P.post(make_cfg(), P.bulletin_report(records[:1]), "en", {}, bot=bot, clock=CLOCK)
    assert "<b>💬 Team replies</b>" in sent(bot)[2]


def test_the_switch_off_and_no_channel_send_nothing():
    cfg, rep = make_cfg(), sample_rundown()
    bot = FakeBot()
    assert not P.post(cfg, rep, "he", {}, bot=bot, clock=CLOCK) and not bot.methods("sendMessage")  # no channel
    register()
    choose(radio="off")
    assert not P.post(cfg, rep, "he", {}, bot=bot, clock=CLOCK) and not to_channel(bot)
    choose(radio="on")
    assert P.post(cfg, rep, "he", {}, bot=bot, clock=CLOCK) and len(to_channel(bot)) == 1


def test_the_same_body_is_not_posted_twice_but_a_change_is():
    register()
    cfg, bot, state = make_cfg(), FakeBot(), {}
    rep = rundown(decisions=[dec("n1", "לאשר?", FRESH)])
    assert P.post(cfg, rep, "he", state, bot=bot, clock="14:30")
    assert not P.post(cfg, rep, "he", state, bot=bot, clock="14:50")  # only the clock differs
    assert len(to_channel(bot)) == 1
    assert P.post(cfg, rundown(decisions=[dec("n1", "לאשר?", FRESH), dec("n2", "ועוד אחת?", FRESH)]), "he", state,
                  bot=bot, clock="15:10")
    assert len(to_channel(bot)) == 2


def station(bot):
    st = object.__new__(radio.Station)
    st.cfg, st.s, st.channel_bot = make_cfg(), {"lang": "he"}, bot
    return st


def test_a_failing_post_is_swallowed_and_logged_and_never_breaks_the_voice():
    register()
    lines = []

    class Down(FakeBot):
        def call(self, method, params=None, timeout=15):
            if (params or {}).get("chat_id") == CHANNEL:
                raise TelegramError("channel gone")
            return super().call(method, params, timeout)
    assert not P.post(make_cfg(), sample_rundown(), "he", {}, bot=Down(), clock=CLOCK)
    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("bug"))  # noqa: E731
    assert not P.post(make_cfg(), sample_rundown(), "he", {}, bot=FakeBot(), clock=CLOCK, send=boom, log=lines.append)
    assert lines and "could not post" in lines[0]
    st = station(Down())  # through the station: the post goes to the one worker; speaking is never touched
    st.post_channel(sample_rundown())
    st.post_channel(None)
    assert st.poster.drain()


def test_every_post_goes_through_one_worker_in_order_and_the_same_body_twice_is_sent_once():
    register()
    bot, threads, order = FakeBot(), set(), []
    real = A.alert

    def send(cfg, body, **kw):
        threads.add(threading.current_thread().name)
        order.append(body.split("\n\n", 1)[1])
        return real(cfg, body, bot=bot, **{k: v for k, v in kw.items() if k != "bot"})
    poster = P.Poster(send=send)
    cfg, same = make_cfg(), rundown(decisions=[dec("n1", "לאשר?", FRESH)])
    other = rundown(decisions=[dec("n1", "לאשר?", FRESH), dec("n2", "ועוד?", FRESH)])
    for rep in (same, same, other, same):  # two identical updates arriving together are not both sent
        poster.put(cfg, rep, "he")
    assert poster.drain()
    poster.stop()  # its worker must not outlive the test
    assert threads == {"radio-poster"} and len(to_channel(bot)) == 3  # same, other, same again (it changed back)
    assert order[0] != order[1] and order[0] == order[2]


def test_the_posting_queue_is_bounded_and_drops_the_oldest():
    register()
    gate, sent_bodies, lines = threading.Event(), [], []

    def send(cfg, body, **kw):
        gate.wait(5)
        sent_bodies.append(body)
        return True
    poster = P.Poster(log=lines.append, send=send)
    cfg = make_cfg()
    reps = [rundown(decisions=[dec(f"n{i}", f"שאלה מספר {i}?", FRESH)]) for i in range(P.QUEUE_MAX + 3)]
    for rep in reps:
        poster.put(cfg, rep, "he")
        time.sleep(0.01)
    assert len(poster.queue) <= P.QUEUE_MAX and any("oldest" in l for l in lines)
    gate.set()
    assert poster.drain() and len(sent_bodies) <= P.QUEUE_MAX + 1
    poster.stop()
    assert "שאלה מספר ⁨" + str(P.QUEUE_MAX + 2) + "⁩" in sent_bodies[-1]  # the newest one always goes out


def test_a_big_message_stays_under_the_telegram_limit():
    rep = {"kind": "bulletin", "lines": [("אורה", "משפט ארוך מאוד " * 40) for _ in range(12)]}
    assert 0 < len(P.render(rep, "he", CLOCK)) <= A.PART_CHARS


def test_secrets_and_markup_never_reach_the_channel_raw():
    rep = {"kind": "bulletin", "lines": [("אורה", "<b>x</b> & מפתח sk-ant-api03-" + "a1B2" * 12)]}
    body = P.render(rep, "he", CLOCK)
    plain = body.replace("\u2068", "").replace("\u2069", "")
    assert "sk-ant-api03" not in plain and "&lt;b&gt;x&lt;/b&gt; &amp;" in plain and "<b>x</b>" not in plain


def test_a_secret_that_crosses_the_cut_is_redacted_before_it_is_shortened():
    key = "sk-ant-api03-" + "a1B2" * 12
    for pad in range(LINE_PAD - 12, LINE_PAD + 14):  # the key starts just before, at and after the cut
        body = P.render({"kind": "bulletin", "lines": [("אורה", "ש" * pad + " " + key)]}, "he", CLOCK)
        assert "sk-ant" not in body and "api03" not in body and "a1B2" not in body, pad


def test_nothing_to_say_posts_nothing():
    assert P.render({"kind": "rundown"}, "he", CLOCK) == "" and P.render({"kind": "bulletin", "lines": []}, "he") == ""
