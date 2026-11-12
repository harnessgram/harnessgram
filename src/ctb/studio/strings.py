"""The Studio app's words, one table per screen (a small t()-style layer, ready for more languages).

`STRINGS[lang][key]` is a plain string with `{name}` slots, or, for a count, a dict keyed by the plural category
of `Intl.PluralRules` (`one`, `two`, `many`, `other`; a missing category falls back to `other`). `WORDS` spells
small counts out for Hebrew lines, where a digit beside Hebrew letters reads badly. The page embeds this table as
JSON; its script only looks keys up (`t(key, vars)`), so a screen's copy never lives in the script itself.

Hebrew and English never share a line: a Hebrew screen holds no English word, and an English screen no Hebrew.
"""
from __future__ import annotations

LANGS = ("he", "en")
STUDIO_MANIFEST_TITLE_HE = "סטודיו"

COMMON = {
    "he": {
        "back": "חזרה", "retry": "לנסות שוב", "play": "לנגן", "frame": "הסטודיו", "close": "לסגור",
        "askTeam": "לכתוב לצוות", "title": "הסטודיו", "sep": " · ",
        "sampleNotice": "מוצגים סרטוני דוגמה. החיבור לסרטונים האמיתיים בא בשלב הבא.",
    },
    "en": {
        "back": "Back", "retry": "Try again", "play": "Play", "frame": "The Studio", "close": "Close",
        "askTeam": "Write to the team", "title": "The Studio", "sep": " · ",
        "sampleNotice": "Sample videos are shown. The link to the real videos comes in the next step.",
    },
}

HOME = {
    "he": {
        "waiting": "מחכה לך", "working": "בעבודה עכשיו", "shelf": "על המדף", "all": "הכל", "shelfAll": "הכל",
        "filterLabel": "סינון לפי סדרה", "stackLabel": "כרטיס {i} מתוך {n}",
        "newTitle": "סרטון חדש מתבנית", "newSub": "בוחרים תבנית, כותבים משפט, הצוות מפיק",
        "series": "סדרה", "episode": "פרק", "length": "אורך",
        "draftReady": "טיוטה {ord} מוכנה", "draftReadyN": "טיוטה מוכנה",
        "suggest": {"one": "הצוות מציע תיקון קטן אחד.", "two": "הצוות מציע שני תיקונים קטנים.",
                    "other": "הצוות מציע {n} תיקונים קטנים."},
        "suggestNone": "הצוות לא מצא מה לתקן. אפשר לצפות ולאשר.",
        "takes": "זה לוקח בערך {t}.",
        "openDirect": "לפתוח ולכוון", "approveAsIs": "לאשר כמו שהוא",
        "workerLine": "סוכן ההפקה עובד על זה · נשאר בערך {t}", "workerNoEta": "סוכן ההפקה עובד על זה",
        "lessThanMinute": "פחות מדקה",
        "stage": {"script": "תסריט", "voice": "קריינות", "art": "ציור", "captions": "כתוביות", "checks": "בדיקות"},
        "status": {"ready": "מוכן", "writing": "בכתיבה", "producing": "בהפקה", "published": "פורסם"},
        "meta": {"ready": "אושר · עותק פרטי נשלח אליך", "writing": "התסריט אצל הצוות", "producing": "בעבודה"},
        "tileMeta": {"ready": "אושר", "writing": "אצל הצוות", "producing": "בעבודה"},
        "publishedAgo": "פורסם {ago}", "publishedNow": "פורסם היום",
        "agoDay": {"one": "אתמול", "two": "לפני יומיים", "other": "לפני {n} ימים"},
        "agoWeek": {"one": "לפני שבוע", "two": "לפני שבועיים", "other": "לפני {n} שבועות"},
    },
    "en": {
        "waiting": "Waiting for you", "working": "In progress", "shelf": "On the shelf", "all": "All", "shelfAll": "All",
        "filterLabel": "Filter by series", "stackLabel": "Card {i} of {n}",
        "newTitle": "New video from a template", "newSub": "Pick a template, write a sentence, the team produces it",
        "series": "Series", "episode": "Episode", "length": "Length",
        "draftReady": "Draft {ord} ready", "draftReadyN": "Draft ready",
        "suggest": {"one": "The team suggests one small fix.", "other": "The team suggests {n} small fixes."},
        "suggestNone": "The team found nothing to fix. You can watch and approve.",
        "takes": "It takes about {t}.",
        "openDirect": "Open and direct", "approveAsIs": "Approve as is",
        "workerLine": "The production agent is on it · about {t} left", "workerNoEta": "The production agent is on it",
        "lessThanMinute": "under a minute",
        "stage": {"script": "Script", "voice": "Voice", "art": "Art", "captions": "Captions", "checks": "Checks"},
        "status": {"ready": "Ready", "writing": "Writing", "producing": "In production", "published": "Published"},
        "meta": {"ready": "Approved · private copy sent to you", "writing": "The script is with the team",
                 "producing": "In progress"},
        "tileMeta": {"ready": "Approved", "writing": "With the team", "producing": "In progress"},
        "publishedAgo": "Published {ago}", "publishedNow": "Published today",
        "agoDay": {"one": "yesterday", "other": "{n} days ago"},
        "agoWeek": {"one": "a week ago", "other": "{n} weeks ago"},
    },
}

# Counts and lengths spelled out in Hebrew (masculine for things, feminine for minutes); English uses numerals.
WORDS = {
    "he": {
        "masc": ["", "אחד", "שניים", "שלושה", "ארבעה", "חמישה", "שישה", "שבעה", "שמונה", "תשעה", "עשרה",
                 "אחד עשר", "שנים עשר", "שלושה עשר", "ארבעה עשר", "חמישה עשר", "שישה עשר", "שבעה עשר", "שמונה עשר",
                 "תשעה עשר", "עשרים"],
        "fem": ["", "אחת", "שתיים", "שלוש", "ארבע", "חמש", "שש", "שבע", "שמונה", "תשע", "עשר",
                "אחת עשרה", "שתים עשרה", "שלוש עשרה", "ארבע עשרה", "חמש עשרה", "שש עשרה", "שבע עשרה", "שמונה עשרה",
                "תשע עשרה", "עשרים"],
        "ord": ["", "ראשונה", "שנייה", "שלישית", "רביעית", "חמישית", "שישית", "שביעית", "שמינית", "תשיעית", "עשירית"],
        "minutes": {"one": "דקה", "two": "שתי דקות", "other": "{n} דקות"},
        "seconds": {"one": "שנייה אחת", "two": "שתי שניות", "other": "{n} שניות"},
        "about": {"half": "כחצי דקה", "one": "כדקה", "oneHalf": "כדקה וחצי", "two": "כשתי דקות",
                  "twoHalf": "כשתי דקות וחצי", "other": "כ{n} דקות"},
        "formats": {"wide": "לרוחב", "tall": "לגובה"},
    },
    "en": {
        "masc": [], "fem": [], "ord": [],
        "minutes": {"one": "a minute", "other": "{n} minutes"},
        "seconds": {"one": "one second", "other": "{n} seconds"},
        "about": {"half": "about half a minute", "one": "about a minute", "oneHalf": "about a minute and a half",
                  "two": "about two minutes", "twoHalf": "about two and a half minutes", "other": "about {n} minutes"},
        "formats": {"wide": "landscape", "tall": "portrait"},
    },
}

STATES = {
    "he": {
        "loadLine": "טוען את השולחן…",
        "emptyTitle": "השולחן ריק",
        "emptyText": "עוד אין סרטונים. בוחרים תבנית וכותבים במשפט אחד על מה הסרטון, והצוות מכין טיוטה.",
        "emptyTeam": "או לכתוב לצוות בנושא", "templates": "תבניות מוכנות", "tplMeta": "{len} · {fmt}",
        "offlineBanner": "המחשב של הסטודיו לא עונה. מה שכבר נטען מוצג כאן.",
        "offlineTitle": "המחשב לא עונה",
        "offlineText": "הסטודיו לא ענה. כשהמחשב יחזור, הדף יתעדכן לבד. אפשר גם לנסות עכשיו.",
        "offTitle": "הגשר כבוי",
        "offText": "הגשר של הקוקפיט כבוי כרגע, ולכן הסטודיו לא פעיל. כשמדליקים אותו הסטודיו חוזר לעבוד, וכל מה ששמור נשאר.",
        "offCheck": "לבדוק שוב",
        "expiredTitle": "החיבור פג",
        "expiredText": "מטעמי ביטחון החיבור נסגר אחרי רבע שעה בלי פעולה. כל השינויים שלך שמורים.",
        "expiredAction": "לחבר מחדש", "expiredNote": "אם זה לא עוזר: לסגור ולפתוח שוב מהכפתור בטלגרם.",
        "errorTitle": "הסטודיו נתקע", "errorWhen": "השולחן לא נטען",
        "errorWhy": "משהו השתבש בסטודיו, ולכן אי אפשר להציג את השולחן. כלום לא אבד.",
        "whatHappened": "מה קרה:",
        "noPermTitle": "אין הרשאה",
        "noPermText": "החשבון הזה לא מורשה להשתמש בסטודיו. הסטודיו פתוח רק לבעלים של הגשר.",
        "noPermNote": "אם זו טעות: לפתוח מחדש מהחשבון של הבעלים.",
    },
    "en": {
        "loadLine": "Loading the desk…",
        "emptyTitle": "The desk is empty",
        "emptyText": "There are no videos yet. Pick a template and write one sentence about the video, and the team prepares a draft.",
        "emptyTeam": "Or write to the team about a topic", "templates": "Ready templates", "tplMeta": "{len} · {fmt}",
        "offlineBanner": "The Studio computer is not answering. What already loaded is shown here.",
        "offlineTitle": "The computer is not answering",
        "offlineText": "The Studio did not answer. When the computer is back, the page updates by itself. You can also try now.",
        "offTitle": "The bridge is off",
        "offText": "The Cockpit bridge is off right now, so the Studio is not running. When it is turned on the Studio works again, and everything saved stays.",
        "offCheck": "Check again",
        "expiredTitle": "The connection expired",
        "expiredText": "For safety the connection closes after a quarter of an hour without activity. All your changes are saved.",
        "expiredAction": "Reconnect", "expiredNote": "If that does not help: close and open it again from the button in Telegram.",
        "errorTitle": "The Studio is stuck", "errorWhen": "The desk did not load",
        "errorWhy": "Something went wrong in the Studio, so the desk cannot be shown. Nothing was lost.",
        "whatHappened": "What happened:",
        "noPermTitle": "No permission",
        "noPermText": "This account is not allowed to use the Studio. The Studio is open only to the bridge's owner.",
        "noPermNote": "If this is a mistake: open it again from the owner's account.",
    },
}

NEXT = {
    "he": {
        "barNew": "סרטון חדש", "barNewSub": "שלב אחד, הצוות עושה את השאר",
        "barOpen": "שולחן הפקה", "barApprove": "אישור טיוטה",
        "title": "השלב הזה בדרך",
        "textNew": "בחירת תבנית וכתיבת נושא מהטלפון מגיעות בשלב הבא. עד אז אפשר לכתוב לצוות בשיחה, והוא ידאג לטיוטה.",
        "textOpen": "פתיחת הסרטון וכיוון המשפטים מהטלפון מגיעים בשלב הבא. הסרטון שמור אצל הצוות ולא הולך לשום מקום.",
        "textApprove": "האישור מהטלפון מגיע בשלב הבא. עד אז הטיוטה נשארת כאן ולא מתפרסמת.",
        "textItem": "פתיחת הסרטון מהטלפון מגיעה בשלב הבא. הוא שמור כמו שהוא.",
        "textMore": "המדף המלא מהטלפון מגיע בשלב הבא.",
        "note": "שום דבר לא נשלח ושום דבר לא השתנה.",
        "home": "לחזור לשולחן", "chat": "לכתוב לצוות בשיחה", "safe": "מה בטוח עכשיו",
        "safe1": "הסרטונים והטיוטות נשארים כמו שהם.", "safe2": "אין פרסום בלי אישור שלך בשיחה.",
        "barRender": "טיוטה חדשה", "barReview": "סקירה ואישור",
        "textRender": "הכנת טיוטה חדשה מהטלפון מגיעה בשלב הבא. הסימונים שלך שמורים כאן כטיוטה, ולא נשלחו לצוות.",
        "textReview": "הסקירה והאישור מהטלפון מגיעים בשלב הבא. הסימונים שלך שמורים כאן כטיוטה.",
        "noteMarks": "שום דבר לא נשלח. הסימונים שמורים כטיוטה בלבד.",
        "backVideo": "לחזור לסרטון", "safe3": "הסימונים שלך שמורים כטיוטה במחשב של הסטודיו.",
    },
    "en": {
        "barNew": "New video", "barNewSub": "One step, the team does the rest",
        "barOpen": "Production desk", "barApprove": "Approve a draft",
        "title": "This step is on its way",
        "textNew": "Picking a template and writing a topic from the phone come in the next step. Until then you can write to the team in the chat and they will prepare the draft.",
        "textOpen": "Opening the video and directing its sentences from the phone come in the next step. The video is kept by the team and goes nowhere.",
        "textApprove": "Approving from the phone comes in the next step. Until then the draft stays here and is not published.",
        "textItem": "Opening the video from the phone comes in the next step. It is kept as it is.",
        "textMore": "The full shelf on the phone comes in the next step.",
        "note": "Nothing was sent and nothing changed.",
        "home": "Back to the desk", "chat": "Write to the team in the chat", "safe": "What is safe right now",
        "safe1": "Videos and drafts stay as they are.", "safe2": "Nothing is published without your yes in the chat.",
        "barRender": "New draft", "barReview": "Review and approve",
        "textRender": "Preparing a new draft from the phone comes in the next step. Your marks are kept here as a draft and were not sent to the team.",
        "textReview": "Reviewing and approving from the phone come in the next step. Your marks are kept here as a draft.",
        "noteMarks": "Nothing was sent. The marks are kept as a draft only.",
        "backVideo": "Back to the video", "safe3": "Your marks are kept as a draft on the Studio computer.",
    },
}

# The project page ("the desk"): one video as a storyboard, the team's suggestions one at a time, and the sentence map.
DESK = {
    "he": {
        "draft": "טיוטה {ord}", "draftN": "טיוטה", "pillWaiting": "מחכה לך", "pillDone": "ההצעות נענו",
        "playerLabel": "תצוגה מקדימה", "playerTag": "תמונות וכתוביות, בלי קול", "pause": "לעצור",
        "removedVeil": "המשפט הזה יוסר", "mapLabel": "מפת המשפטים", "length": "אורך", "saves": "חוסך {t}",
        "modesLabel": "מה מוצג", "modeSuggest": "הצעות", "modeScenes": "סצנות", "modeAll": "כל המשפטים",
        "suggest": {"tighten": "הצוות מציע לקצר", "remove": "הצוות מציע להסיר"},
        "suggestPill": {"tighten": "הצעת הצוות: לקצר", "remove": "הצעת הצוות: להסיר"},
        "flag": {"visual": "תמונה חדשה", "take": "קול חדש", "fix": "צריך תיקון"},
        "afterShort": "אחרי הקיצור · נגיעה לעריכה", "yourShort": "הקיצור שלך · נגיעה לעריכה",
        "editLabel": "המילים שיישארו במשפט", "editPlaceholder": "כותבים כאן את המשפט הקצר",
        "editHint": "לוחצים ״לקצר״ כדי לשמור",
        "teamShortens": "הצוות יקצר את המשפט",
        "keep": "להשאיר", "tighten": "לקצר", "remove": "להסיר", "restore": "להחזיר", "choiceLabel": "מה לעשות במשפט",
        "hear": "להשמיע את המשפט", "hearNone": "לדוגמה הזו אין קול. בטיוטה אמיתית המשפט יישמע כאן.",
        "cueLabel": "הצעה {i} מתוך {n}",
        "noSuggestTitle": "אין הצעות פתוחות",
        "noSuggestText": "הצוות לא מצא מה לתקן בטיוטה הזו. אפשר לעבור על המשפטים ולסמן בעצמך.",
        "allAnsweredTitle": "ענית על כל ההצעות",
        "allAnsweredText": "הסימונים שמורים כטיוטה. אפשר לעבור על כל המשפטים או להכין טיוטה חדשה.",
        "toAll": "לכל המשפטים",
        "changes": {"one": "שינוי אחד · רק הוא יוכן מחדש", "two": "שני שינויים · רק הם יוכנו מחדש",
                    "other": "{n} שינויים · רק הם יוכנו מחדש"},
        "noChanges": "אין שינויים עדיין",
        "review": "לסקירה", "render": "להכין טיוטה חדשה",
        "storyboard": "הסצנות לפי הסדר", "storyboardHint": "נגיעה בסצנה פותחת את המשפטים שלה.",
        "sentencesCount": {"one": "משפט אחד", "two": "שני משפטים", "other": "{n} משפטים"},
        "scenesCount": {"one": "סצנה אחת", "two": "שתי סצנות", "other": "{n} סצנות"},
        "sceneOrd": "סצנה {ord}", "sceneN": "סצנה",
        "sceneSuggest": {"one": "הצעה פתוחה", "two": "שתי הצעות פתוחות", "other": "{n} הצעות פתוחות"},
        "sceneChanges": {"one": "שינוי אחד", "two": "שני שינויים", "other": "{n} שינויים"},
        "sceneClean": "בלי שינויים",
        "savedFail": "לא נשמר. המחשב לא עונה, אפשר לנסות שוב.",
        "savedFailGeneric": "לא נשמר. משהו השתבש, והסימון חזר למה שהיה.",
        "offlineBanner": "המחשב של הסטודיו לא עונה. מה שכבר נטען מוצג כאן, וסימונים לא יישמרו עכשיו.",
        "loadingTag": "טוען את הסרטון…", "loadingNote": "הפריים הראשון מוצג מיד, והשאר נטען ברקע.",
        "errorTitle": "הסרטון לא נטען", "errorWhen": "שולחן ההפקה",
        "errorWhy": "משהו השתבש בסטודיו, ולכן אי אפשר להציג את הסרטון. כלום לא אבד.",
        "notFoundTitle": "הסרטון לא נמצא",
        "notFoundWhy": "הסרטון הזה כבר לא על השולחן. אולי הצוות העביר אותו למדף.",
    },
    "en": {
        "draft": "Draft {ord}", "draftN": "Draft", "pillWaiting": "Waiting for you", "pillDone": "Suggestions answered",
        "playerLabel": "Preview", "playerTag": "Stills and captions, no sound", "pause": "Pause",
        "removedVeil": "This sentence will be removed", "mapLabel": "Sentence map", "length": "Length", "saves": "Saves {t}",
        "modesLabel": "What is shown", "modeSuggest": "Suggestions", "modeScenes": "Scenes", "modeAll": "All sentences",
        "suggest": {"tighten": "The team suggests shortening", "remove": "The team suggests removing"},
        "suggestPill": {"tighten": "Team suggests: shorten", "remove": "Team suggests: remove"},
        "flag": {"visual": "New picture", "take": "New voice", "fix": "Needs a fix"},
        "afterShort": "After shortening · tap to edit", "yourShort": "Your shorter wording · tap to edit",
        "editLabel": "The words that stay in the sentence", "editPlaceholder": "Write the short sentence here",
        "editHint": "Tap \u201cShorten\u201d to save",
        "teamShortens": "The team will shorten it",
        "keep": "Keep", "tighten": "Shorten", "remove": "Remove", "restore": "Restore", "choiceLabel": "What to do with the sentence",
        "hear": "Play this sentence", "hearNone": "This sample has no sound. In a real draft the sentence plays here.",
        "cueLabel": "Suggestion {i} of {n}",
        "noSuggestTitle": "No open suggestions",
        "noSuggestText": "The team found nothing to fix in this draft. You can go through the sentences and mark them yourself.",
        "allAnsweredTitle": "You answered every suggestion",
        "allAnsweredText": "Your marks are kept as a draft. You can go through all the sentences or prepare a new draft.",
        "toAll": "To all the sentences",
        "changes": {"one": "One change · only it is prepared again", "other": "{n} changes · only they are prepared again"},
        "noChanges": "No changes yet",
        "review": "To review", "render": "Prepare a new draft",
        "storyboard": "The scenes in order", "storyboardHint": "Tap a scene to open its sentences.",
        "sentencesCount": {"one": "one sentence", "other": "{n} sentences"},
        "scenesCount": {"one": "one scene", "other": "{n} scenes"},
        "sceneOrd": "Scene {ord}", "sceneN": "Scene",
        "sceneSuggest": {"one": "One open suggestion", "other": "{n} open suggestions"},
        "sceneChanges": {"one": "One change", "other": "{n} changes"},
        "sceneClean": "No changes",
        "savedFail": "Not saved. The computer is not answering; you can try again.",
        "savedFailGeneric": "Not saved. Something went wrong, and the mark is back to what it was.",
        "offlineBanner": "The Studio computer is not answering. What already loaded is shown here, and marks are not saved now.",
        "loadingTag": "Loading the video…", "loadingNote": "The first frame shows at once, and the rest loads in the background.",
        "errorTitle": "The video did not load", "errorWhen": "The production desk",
        "errorWhy": "Something went wrong in the Studio, so the video cannot be shown. Nothing was lost.",
        "notFoundTitle": "The video was not found",
        "notFoundWhy": "This video is no longer on the desk. The team may have moved it to the shelf.",
    },
}

# The words view: every sentence, scene by scene, with its still, its mark and the map of the whole video.
SENTS = {
    "he": {
        "title": "כל המשפטים",
        "legend": {"kept": "נשאר", "t": "מקוצר", "s": "הצעת הצוות", "n": "קול או תמונה חדשים", "f": "צריך תיקון",
                   "x": "הוסר"},
    },
    "en": {
        "title": "All sentences",
        "legend": {"kept": "Kept", "t": "Shortened", "s": "Team suggestion", "n": "New voice or picture", "f": "Needs a fix",
                   "x": "Removed"},
    },
}

SCREENS = {"common": COMMON, "home": HOME, "states": STATES, "next": NEXT, "words": WORDS, "desk": DESK, "sents": SENTS}


def table() -> dict:
    """The whole table as the page embeds it: {lang: {screen: {key: ...}}}."""
    return {lang: {name: part[lang] for name, part in SCREENS.items()} for lang in LANGS}


def keys(value, prefix: str = "") -> set:
    """Every key path of one language's tree (a plural dict counts as one key)."""
    out = set()
    for k, v in value.items():
        path = prefix + k
        if isinstance(v, dict) and not (set(v) <= {"one", "two", "many", "other", "half", "oneHalf", "twoHalf"}):
            out |= keys(v, path + ".")
        else:
            out.add(path)
    return out


# Bilingual sample titles used by the Studio data provider.
SAMPLE_TEXT = {
    'sample_text_1': {"he": 'שאלה של שבוע', "en": 'Question of the week'},
    'sample_text_2': {"he": 'סיפורים', "en": 'Stories'},
    'sample_text_3': {"he": 'מדריכים', "en": 'Guides'},
    'sample_text_4': {"he": 'סיכומים', "en": 'Recaps'},
    'sample_text_5': {"he": 'למה העלים מחליפים צבע?', "en": 'Why do leaves change colour?'},
    'sample_text_6': {"he": 'איך הדבורים מכינות דבש?', "en": 'How do bees make honey?'},
    'sample_text_7': {"he": 'הגשר הקטן', "en": 'The little bridge'},
    'sample_text_8': {"he": 'השבוע', "en": 'This week'},
    'sample_text_9': {"he": 'איך שותלים זרע', "en": 'How to plant a seed'},
    'sample_text_10': {"he": 'למה הירח משנה צורה?', "en": 'Why does the moon change shape?'},
    'sample_text_11': {"he": 'מאיפה מגיע האור?', "en": 'Where does light come from?'},
    'sample_text_12': {"he": 'סיפור לפני השינה', "en": 'Bedtime story'},
    'sample_text_13': {"he": 'מדריך קצר', "en": 'Short guide'},
    'sample_text_14': {"he": 'סיכום שבועי', "en": 'Weekly recap'},
}
