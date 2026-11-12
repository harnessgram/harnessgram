"""Sample videos for the Studio desk: bilingual sample text kept apart from the provider's logic."""
from __future__ import annotations


def _t(he: str, en: str) -> dict:
    return {"he": he, "en": en}


def _scene(i, he, en, start, end, scene):
    return {"i": i, "title": _t(he, en), "start": start, "end": end, "scene": scene, "frame": None}


def _line(i, scene_i, start, end, scene, he, en, **extra):
    out = {"i": i, "scene_i": scene_i, "start": start, "end": end, "said": _t(he, en), "scene": scene, "frame": None,
           "audio": None, "suggest": None, "short": None, "short_s": None, "why": None, "flag": None, "note": None}
    out.update(extra)
    return out


def _leaves() -> dict:
    scenes = [
        _scene(1, "השאלה", "The question", 0, 8, "q"),
        _scene(2, "מה יש בעלה", "Inside a leaf", 8, 20, "leaf"),
        _scene(3, "הסתיו מגיע", "Autumn arrives", 20, 33, "day"),
        _scene(4, "הצבעים מתגלים", "The colours appear", 33, 46, "gold"),
        _scene(5, "העלה נופל", "The leaf falls", 46, 53, "fall"),
        _scene(6, "משימה בחוץ", "A task outside", 53, 63, "hunt"),
        _scene(7, "להתראות", "Goodbye", 63, 68, "bye"),
    ]
    lines = [
        _line(1, 0, 0, 5, "q", "שאלתם אותנו השבוע: למה העלים מחליפים צבע בסתיו?",
              "You asked us this week: why do leaves change colour in autumn?"),
        _line(2, 0, 5, 8, "q", "אהה, רגע, נתחיל שוב.", "Um, wait, let's start again.", suggest="remove",
              why=_t("מעידה בקריינות. המשפט הבא מתחיל נקי.", "A slip in the narration. The next sentence starts clean.")),
        _line(3, 1, 8, 14, "leaf", "בכל עלה ירוק יש חומר קטן שנקרא כלורופיל.",
              "Every green leaf has a tiny substance called chlorophyll."),
        _line(4, 1, 14, 20, "sun", "הכלורופיל עוזר לעץ להכין מזון מאור השמש.", "Chlorophyll helps the tree make food from sunlight."),
        _line(5, 2, 20, 27, "day", "אז בעצם, מה שרציתי להגיד זה שבסתיו הימים מתקצרים.",
              "So really, what I wanted to say is that in autumn the days get shorter.", suggest="tighten",
              short=_t("בסתיו הימים מתקצרים.", "In autumn the days get shorter."), short_s=4,
              why=_t("הפתיחה חוזרת על עצמה. הקול נשאר.", "The opening repeats itself. The voice stays.")),
        _line(6, 2, 27, 33, "fade", "העץ מפסיק להכין כלורופיל, והירוק נעלם לאט.",
              "The tree stops making chlorophyll, and the green slowly fades."),
        _line(7, 3, 33, 40, "gold", "ואז מתגלים צבעים שהיו שם כל הזמן: צהוב וכתום.",
              "Then colours that were there all along appear: yellow and orange.", flag="visual"),
        _line(8, 3, 40, 46, "red", "חלק מהעלים מכינים גם צבע אדום חדש.", "Some leaves also make a new red colour.", flag="fix",
              note=_t("המילה האחרונה נבלעה בקול", "The last word is swallowed in the voice")),
        _line(9, 4, 46, 53, "fall", "בסוף העלה מתייבש ונופל, והעץ נח עד האביב.",
              "In the end the leaf dries and falls, and the tree rests until spring.", flag="take"),
        _line(10, 5, 53, 59, "hunt", "בפעם הבאה שאתם בחוץ, חפשו עלה בשלושה צבעים.",
              "Next time you are outside, look for a leaf with three colours.", suggest="tighten",
              short=_t("בחוץ, חפשו עלה בשלושה צבעים.", "Outside, look for a leaf with three colours."), short_s=4,
              why=_t("אותו רעיון במילים פחות.", "The same idea in fewer words.")),
        _line(11, 5, 59, 63, "hunt", "וספרו לחבר מה גיליתם.", "And tell a friend what you found."),
        _line(12, 6, 63, 68, "bye", "נתראה בשבוע הבא!", "See you next week!"),
    ]
    return {"id": "weekly-12", "series": "weekly", "title": _t("למה העלים מחליפים צבע?", "Why do leaves change colour?"),
            "episode": 12, "draft": 2, "length_s": 68, "scenes": scenes, "sentences": lines}


def _bees() -> dict:
    scenes = [
        _scene(1, "פתיחה", "Opening", 0, 12, "title"),
        _scene(2, "בין הפרחים", "Among the flowers", 12, 40, "bee"),
        _scene(3, "בכוורת", "In the hive", 40, 66, "gold"),
        _scene(4, "להתראות", "Goodbye", 66, 74, "bye"),
    ]
    lines = [
        _line(1, 0, 0, 6, "title", "איך הדבורים מכינות דבש?", "How do bees make honey?"),
        _line(2, 0, 6, 12, "title", "בואו נצא לגינה ונגלה.", "Let's go out to the garden and find out."),
        _line(3, 1, 12, 22, "bee", "הדבורה עפה מפרח לפרח ואוספת צוף מתוק.",
              "The bee flies from flower to flower and collects sweet nectar."),
        _line(4, 1, 22, 40, "bee", "היא שומרת את הצוף בבטן מיוחדת, שהיא בעצם, איך לומר, מין שק קטן כזה שיש לה בפנים.",
              "She keeps the nectar in a special stomach, which is really, how to put it, a kind of little bag she has inside.",
              suggest="tighten", short=_t("היא שומרת את הצוף בבטן מיוחדת.", "She keeps the nectar in a special stomach."),
              short_s=6, why=_t("המשפט מתארך. החצי הראשון אומר הכל.", "The sentence runs long. The first half says it all.")),
        _line(5, 2, 40, 55, "gold", "בכוורת, הדבורות מאווררות את הצוף עד שהוא מתייבש ונהיה דבש.",
              "In the hive, the bees fan the nectar until it dries into honey."),
        _line(6, 2, 55, 66, "gold", "כך כל צנצנת דבש היא עבודה של אלפי דבורים.",
              "So every jar of honey is the work of thousands of bees."),
        _line(7, 3, 66, 74, "bye", "נתראה בשבוע הבא!", "See you next week!"),
    ]
    return {"id": "weekly-13", "series": "weekly", "title": _t("איך הדבורים מכינות דבש?", "How do bees make honey?"),
            "episode": 13, "draft": 1, "length_s": 74, "scenes": scenes, "sentences": lines}
