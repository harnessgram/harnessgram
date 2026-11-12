"""The brainstorming room's words: seat names, the moderator's fixed lines and the room topic's replies, in Hebrew
and English (radio_room.py). The Cockpit's own labels for the room live in panel_page.STRINGS beside the rest of the
page. Hebrew lines never carry English: a project's name is the only outside word, and the caller passes it
through radio_room.clean first (a short Latin name is isolated, a longer one removed)."""
from __future__ import annotations

TEXT = {
    "he": {
        "seat_moderator": "המנחה", "seat_critic": "המבקר", "seat_designer": "המעצב", "seat_ceo": "המנכ״ל",
        "seat_delegate": "נציג {project}",
        "round_ideas": "רעיונות", "round_objections": "התנגדויות", "round_decision": "החלטה",
        "open": "פותחים את החדר. הנושא: {topic}. סביב השולחן: {seats}. שלושה סבבים: רעיונות, התנגדויות והחלטה.",
        "intro_ideas": "סבב ראשון, רעיונות. כל אחד אומר רעיון אחד.",
        "intro_objections": "סבב שני, התנגדויות. מה עלול להשתבש?",
        "intro_decision": "סבב אחרון, החלטה. מה בוחרים ולמה?",
        "pause": "רגע, עוצרים. מקשיבים לך.",
        "resume_heard": "תודה, שמענו. ממשיכים.",
        "resume_quiet": "ממשיכים.",
        "summary_fallback": "סיימנו. הסיכום מחכה לך בקוקפיט.",
        "close": "החדר נסגר. שום דבר לא נשלח לסשן בלי לחיצה שלך.",
        "topic_name": "חדר סיעור מוחות",
        "topic_open": "החדר פתוח. הנושא: {topic}\nהקלטה קולית כאן נכנסת לחדר לפני הדובר הבא. היא הצעה בלבד ואינה מאשרת שום פעולה.",
        "heard": "נכנס לחדר. הדובר הבא יתייחס לזה.",
        "closed": "אין עכשיו חדר פתוח. פותחים חדר מכרטיס הרדיו בקוקפיט.",
        "unreadable": "לא הצלחתי לקרוא את ההודעה. אפשר לנסות שוב בהקלטה קצרה או בטקסט.",
        "forwarded": "הודעה מועברת לא נכנסת לחדר. רק המילים שלך.",
        "secret": "בהודעה יש משהו שנראה כמו סיסמה או מפתח, ולכן היא לא נכנסה לחדר.",
    },
    "en": {
        "seat_moderator": "Moderator", "seat_critic": "Critic", "seat_designer": "Designer", "seat_ceo": "CEO",
        "seat_delegate": "{project} delegate",
        "round_ideas": "Ideas", "round_objections": "Objections", "round_decision": "Decision",
        "open": "The room is open. The topic: {topic}. Around the table: {seats}. Three rounds: ideas, objections "
                "and a decision.",
        "intro_ideas": "Round one, ideas. One idea each.",
        "intro_objections": "Round two, objections. What could go wrong?",
        "intro_decision": "Last round, the decision. What do we pick, and why?",
        "pause": "One moment, we stop. We are listening.",
        "resume_heard": "Thanks, we heard you. On we go.",
        "resume_quiet": "On we go.",
        "summary_fallback": "We are done. The summary waits for you in the Cockpit.",
        "close": "The room is closed. Nothing goes to a session without your tap.",
        "topic_name": "Brainstorm room",
        "topic_open": "The room is open. The topic: {topic}\nA voice note here joins the room before the next speaker. "
                      "It is only a proposal and approves nothing.",
        "heard": "In the room. The next speaker will take it up.",
        "closed": "No room is open now. Open one from the radio card in the Cockpit.",
        "unreadable": "I could not read that. Try a short voice note or a text.",
        "forwarded": "A forwarded message does not join the room: only your own words.",
        "secret": "That looks like it holds a password or a key, so it did not join the room.",
    },
}


def say(key: str, lang: str, **kw) -> str:
    table = TEXT["he" if lang == "he" else "en"]
    text = table[key]
    return text.format(**kw) if kw else text
