"""The control panel's page: one static HTML document with its strings table, styles and script.

Everything the owner reads comes from STRINGS (one table per UI language, the same keys in each). Agent text
arrives from /api/state with a Hebrew copy beside it when one exists ("title_he"; null while it is being
translated) and the page shows the Hebrew, keeping the English original folded under a small toggle. CSS
uses logical properties only (start/end, inline/block), so the layout follows the root `dir`. Colours come
from Telegram's theme parameters, with light and dark fallbacks outside Telegram.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import cockpit_v2
from .i18n import t

FSI, PDI = "⁨", "⁩"  # a short English name inside a Hebrew line is wrapped in these


def _iso(name: str) -> str:
    return FSI + name + PDI


STRINGS = {
    "he": {
        "hooksTitle": "תקינות החיבור לסשנים", "hooksBroken": "הגדרות החיבור השתנו. תשובות מסשנים עלולות להתעכב.",
        "hooksRestore": "שחזור", "hooksRestored": "החיבור שוחזר. סשנים שכבר היו פתוחים צריכים טעינה מחדש.",
        "hooksReload": "טעינת הסשנים מחדש", "hooksHint": "רק סשנים פנויים עם הגדרות ישנות ייטענו מחדש. סשן עובד או חלון עם שאלה יידלגו.",
        "hooksRunning": "טוען סשנים פנויים מחדש…", "hooksReloaded": "נטענו מחדש", "hooksSkipped": "דולגו", "hooksReloadFailed": "הטעינה לא הושלמה. אפשר לנסות שוב.",
        "reload_busy": "עדיין עובד, יש שאלה או טיוטה", "reload_current": "כבר מעודכן", "reload_no_pane": "אין חלון זמין",
        "hooksNReloaded": "נטענו מחדש: #", "hooksNCurrent": "מעודכנים: #", "hooksNBusy": "עובדים עכשיו, דולגו: #",
        "hooksNOther": "צריכים בדיקה: #", "hooksNone": "אין סשנים לטעינה",
        "reload_unknown": "לא ניתן לוודא שהסשן פנוי", "reload_failed": "הטעינה לא הושלמה", "reload_dialog": "ממתין לאישור בחלון", "reload_not_live": "כבר לא פעיל",
        "radioPlay": "האזנה", "radioPause": "השהיה", "listenSafari": "האזנה בספארי", "listenFailed": "לא הצלחתי ליצור קישור. נסו שוב", "listenRevoke": "ביטול קישורי האזנה", "listenRevokeHint": "קישורי האזנה בספארי פעילים. ביטול סוגר את כולם", "listenRevoked": "קישורי ההאזנה בוטלו", "devicesTitle": "וידג'טים מחוברים", "devicesHint": "מכשירים שהווידג'טים שלהם קוראים את הקוקפיט. ניתוק עובד מיד", "devicesAdded": "נוסף", "devicesRevoke": "ניתוק", "devicesRevoked": "המכשיר נותק", "devicesFailed": "לא הצלחתי לנתק. נסו שוב", "radioLive": "בשידור חי",
        "radioPlayerWaiting": "ממתין לשמע…", "radioPlayerOff": "הרדיו כבוי", "radioPlayerError": "ההאזנה נעצרה. אפשר לנסות שוב",
        "myMusic": "המוזיקה שלי", "myMusicHint": "הפלייליסטים והלייקים שלי ביוטיוב מיוזיק", "myMusicLiked": "מוזיקה שאהבתי", "myMusicEmpty": "אין פלייליסטים להצגה", "myMusicFailed": "לא הצלחתי לטעון את המוזיקה", "myMusicPlay": "ניגון", "myMusicPause": "השהיה", "myMusicNext": "הבא",
        "macTitle": "המחשב", "macUnavailable": "המידע אינו זמין כרגע", "macMemory": "לחץ זיכרון וזיכרון פנוי משוער", "macCpu": "עומס מעבד לדקה, חמש דקות ורבע שעה", "macCores": "ליבות", "macDisk": "מקום פנוי בכונן המערכת, ללא מקום שניתן לפינוי ובכוננים אחרים", "macBattery": "סוללה", "macPower": "מחובר לחשמל", "macOnBattery": "פועל על סוללה", "macNoBattery": "אין סוללה", "macUptime": "זמן מאז ההפעלה", "macHours": "שעות", "macGb": "גיגה־בייט", "macTopCpu": "שימוש ממוצע במעבד לאורך חיי התהליך", "macTopMemory": "הפעילים ביותר בזיכרון", "macTest": "הרצת בדיקות", "macEncoder": "קידוד וידאו", "macAgent": "סוכן", "macBrowser": "דפדפן", "macBuild": "כלי בנייה", "macOther": "תהליך אחר", "macOk": "המצב תקין", "macWarn": "כדאי לבדוק את מצב המחשב", "macCritical": "נדרשת תשומת לב למצב המחשב", "macMemoryOk": "יש זיכרון פנוי", "macMemoryWarn": "לחץ הזיכרון עולה", "macMemoryCritical": "לחץ הזיכרון גבוה", "macCpuOk": "העומס נמוך ביחס לליבות", "macCpuWarn": "העומס בחמש הדקות האחרונות מתקרב למספר הליבות", "macCpuCritical": "העומס בחמש הדקות האחרונות גבוה ביחס לליבות", "macDiskOk": "יש מקום פנוי", "macDiskWarn": "המקום הפנוי מצטמצם", "macDiskCritical": "נותר מעט מקום פנוי", "macBatteryOk": "מצב הסוללה תקין", "macBatteryWarn": "הסוללה מתרוקנת", "macBatteryCritical": "הסוללה כמעט ריקה",
        "moreTabs": "עוד", "title": "הקוקפיט", "here": "הנושא הזה", "all": "כל הלוח",
        "waiting": "מחכה לך", "nothing": "אין החלטות שמחכות לך",
        "nothingHint": "כשסוכן ישאל אותך משהו, השאלה תופיע כאן ותוכל לענות בלחיצה אחת.",
        "reminders": "תזכורות", "openTasks": "משימות פתוחות", "sessions": "עובדים עכשיו", "none": "אף סשן לא עובד כרגע",
        "noneHint": "סשן שמתחיל לעבוד יופיע כאן עם הצעד שהוא עושה עכשיו. לחיצה על שורה פותחת פרטים.",
        "endedList": "סשנים שהסתיימו", "quota": "מכסות", "banked": "איפוסים שמורים", "bank": "בנק האיפוסים", "bankHint": "כמה איפוסי מכסה שמורים יש לך. הסוכנים רואים את זה וימליצו מתי כדאי להשתמש.", "bankUnset": "לא הוגדר", "bankExpires": "בתוקף עד", "bankClear": "הסרה", "bankLeft": "עוד # ימים", "bankLeft1": "עוד יום אחד", "bankToday": "פג היום", "bankExpired": "פג תוקף", "less": "פחות", "more1": "עוד", "week": "שבועי",
        "reset": "מתאפס", "working": "עובד", "idle": "ממתין", "ended": "הסתיים",
        "ctx": "הקשר", "model": "מודל", "account": "חשבון", "last": "פעילות אחרונה", "folder": "תיקייה", "branch": "ענף",
        "bg1": "משימה ברקע", "bg": "משימות ברקע", "bgNone": "אין משימות ברקע",
        "now": "עכשיו", "updated": "עודכן", "updating": "מתעדכן…", "refresh": "רענון",
        "pull": "משכו למטה לרענון", "release": "שחררו לרענון",
        "denied": "אין גישה ללוח", "renderFailed": "שגיאה בהצגת הלוח", "open": "יש לפתוח את הלוח מתוך טלגרם",
        "reopen": "ההתחברות לטלגרם פגה. סגרו את הלוח ופתחו אותו מחדש",
        "busy": "המערכת עסוקה, נסו שוב", "failed": "השליחה נכשלה, נסו שוב", "retry": "נסו שוב",
        "offline": "אין חיבור כרגע, ננסה שוב בעוד רגע",
        "gone": "ההחלטה כבר נסגרה", "already": "כבר נענתה", "sent": "התשובה ממתינה למסירה לסוכן",
        "chose": "בחרת", "notePh": "הערה לסשן (לא חובה)", "addNote": "הוספת הערה",
        "topic": "נושא", "source": "מקור", "hideSource": "הסתרת המקור", "translating": "מתרגם לעברית…",
        "tapNote": "התשובה שלך כאן נחשבת אישור שלך, גם לפעולה רגישה, ורק למה שכתוב בשאלה",
        "radio": "שידורי הקוקפיט", "radioOn": "פועל", "radioOff": "כבוי", "radioStart": "הפעלה",
        "radioStop": "עצירה", "radioWait": "רגע, זה לוקח כמה שניות…",
        "radioHint": "שידור קולי קצר על מה שהצוות עושה, בערוץ של הצוות.",
        "session": "הסשן של הנושא", "board": "לוח הנושא", "checks": "משימות", "links": "קישורים",
        "emptyHere": "אין כרגע החלטות, תזכורות או משימות בנושא הזה",
        "emptyHereHint": "כל הסשנים, ההחלטות והמכסות נמצאים בלשונית ״כל הלוח״.", "goAll": "מעבר לכל הלוח",
        "nWaiting": "מחכים לך", "nWorking": "עובדים", "nIdle": "ממתינים",
        "noSession": "הסשן של הנושא הזה כבר לא פעיל",
        "digest": "מאז הביקור הקודם", "gotIt": "הבנתי",
        "dNew1": "החלטה חדשה", "dNew": "החלטות חדשות", "dDone1": "סשן סיים לעבוד", "dDone": "סשנים סיימו לעבוד",
        "dEnded1": "סשן נסגר", "dEnded": "סשנים נסגרו", "dStarted1": "סשן חדש", "dStarted": "סשנים חדשים",
        "settings": "הגדרות", "defModel": "מודל לסשנים חדשים",
        "defModelHint": "נשמר בהגדרות של " + _iso("Claude Code") + ". סשנים פתוחים ממשיכים עם המודל שלהם.",
        "notSet": "לא נקבע", "modelSaved": "סשנים חדשים יתחילו עם", "settingsBroken": "קובץ ההגדרות פגום, לא שיניתי דבר",
        "compactAt": "דחיסה אוטומטית ב־", "compactOwn": "דחיסה אוטומטית לסשן הזה", "compactGlobal": "כמו בכל הסשנים", "compactNever": "אף פעם", "compactOff": "כבוי",
        "screen": "המסך האמיתי", "screenHide": "הסתרת המסך", "screenNone": "אין כרגע מסך שאפשר להציג לסשן הזה", "recent": "סיימו לאחרונה", "nRecent": "סיימו לאחרונה", "idleList": "ממתינים", "leftList": "נשכחו מאחור", "leftHint": "סשנים פתוחים שלא נגעו בהם יותר מיום. אפשר לכתוב להם כדי להמשיך, או לסגור את הנושא. סגירה לא מוחקת דבר: כתיבה בנושא פותחת אותו שוב.", "closeTopic": "סגור את הנושא", "closeAll": "סגור את כולם", "sure": "לחץ שוב לאישור", "closedN": "נסגרו", "notClosed": "לא נסגר, אפשר לנסות שוב", "doneRem": "סמן שבוצע", "remCleared": "התזכורת נוקתה", "clearOld": "נקה את הישנות", "lastReply": "תשובה אחרונה",
        "openTopic": "לנושא", "openTg": "פתיחה בטלגרם", "newBadge": "חדש", "markRead": "סימון הכל כנקרא", "markedRead": "הכל סומן כנקרא",
        "staleList": "כבר לא רלוונטיות",
        "staleHint": "שאלות שכבר לא מחכות לך: הסשן שלהן הסתיים, או שהן נשאלו שוב מאוחר יותר.",
        "oldList": "ישנים", "oldHint": "פריטים שמחכים לך יותר משלושה ימים. אפשר לענות עליהם כרגיל.",
        "clearAll": "ניקוי הכל", "cleared": "נוקו", "why_ended": "הסשן הסתיים", "why_replaced": "נשאלה שוב",
        "more": "עוד",
        "radioSay": "השמעה ברדיו", "radioSayPh": "מה להשמיע עכשיו?", "radioSayBtn": "להשמיע עכשיו",
        "radioSaid": "יושמע ברדיו בעוד רגע", "radioIsOff": "הרדיו כבוי",
        "set_voice_digest": "תקציר קולי",
        "hint_voice_digest": "עדכוני הרדיו בהודעה קולית אחת. שקט בלילה ובחלונות שבת וחג שהוגדרו.",
        "digest_off": "כבוי", "digest_important": "חשובים בלבד", "digest_hourly": "כל שעה",
        "digest_every_3_hours": "כל שלוש שעות",
        "set_radio_every_reply": "הרדיו אומר כל תשובה",
        "hint_radio_every_reply": "כל תשובה של סשן נאמרת ברדיו בקצרה. כבוי: רק תשובה להקלטה קולית.",
        "set_radio_board": "מופע הפרויקטים ברדיו",
        "set_radio_music": "מוזיקה בהאזנה הפרטית",
        "hint_radio_music": "מוזיקה ופודקאסטים בין השידורים, בעוצמה נמוכה בזמן דיבור. נשארים בהאזנה הפרטית בלבד ונחים בשבת.",
        "musicNow": "מתנגן עכשיו", "musicNone": "אין מוזיקה שמתנגנת עכשיו", "musicLike": "אהבתי", "musicSkip": "דלג",
        "programs": "תוכניות רדיו", "programHint": "סדר השידור הפרטי. אפשר לשנות, לסדר ולהתחיל מיד.", "programStart": "התחל עכשיו", "programSave": "שמירה", "programUp": "למעלה", "programDown": "למטה", "programMinutes": "דקות",
        "hint_radio_board": "התמונה של הרדיו זזה: עמודה לכל פרויקט, קיר משימות לכל פרויקט (הושלם, בעבודה, הבא בתור) ופס מבזקים. כבוי: הכרטיס הרגיל.",
        "radioMinutes": "דקות קול משובח היום", "radioOf": "מתוך", "radioBudgetUnavailable": "תקציב הקול אינו זמין", "radioConfiguredCap": "תקרה מוגדרת", "radioActiveCap": "תקרה פעילה",
        "radioListening": "יש מאזין לרדיו", "radioNotListening": "לא זוהה מאזין — הקול החינמי ייבחר למבזק הבא",
        "radioListenerUnknown": "לא ניתן לוודא האזנה — הקול החינמי ייבחר למבזק הבא",
        "radioDetectionOff": "זיהוי האזנה אינו זמין — חלה המגבלה הרגילה",
        "radioListenerPlayer": "נגן הקוקפיט", "radioListenerCall": "השידור בטלגרם", "radioModeGroup": "מצב: שידור חי בקבוצה והאזנה פרטית", "radioModePrivate": "מצב: האזנה פרטית בלבד", "radioModeNext": "השינוי יחול בהפעלה הבאה של הרדיו",
        # the podcast queue on the radio (radio_podcast.py)
        "podcast": "פודקאסט ברדיו", "podIdle": "שום פרק לא מתנגן", "podLoading": "מוריד את הפרק…",
        "podRadioOff": "הרדיו כבוי. הפרק ישודר כשתדליקו אותו", "podPlay": "המשך", "podPause": "השהיה",
        "podSkip": "דילוג", "podStop": "עצירה", "podRemove": "הסרה", "podQueue": "בתור",
        "podAdd": "הוספת פרק", "podFeeds": "בחרו פודקאסט", "podFetching": "מביא את הפרקים…",
        "podNoEpisodes": "אין פרקים להשמעה בפיד הזה", "podBack": "חזרה לרשימה",
        "podLinkPh": "הדביקו קישור לפרק או לפיד", "podLinkBtn": "הוספה לתור", "podAdded": "הפרק נוסף לתור",
        "podErr_bad": "הבקשה לא תקינה", "podErr_url": "הקישור לא מותר: רק קישורים מאובטחים לאתרים ציבוריים",
        "podErr_fetch": "לא הצלחתי להביא את הפודקאסט", "podErr_feed": "זה לא פיד פודקאסט עם פרק להשמעה",
        "podErr_too_big": "הקובץ גדול מדי", "podErr_unknown": "אין פיד או פרק כזה", "podErr_full": "התור מלא",
        "podErr_dup": "הפרק כבר בתור", "podErr_unsuitable": "הפרק לא עובר את כללי ההתאמה של הרדיו",
        "podErr_explicit": "הפרק מסומן כמיועד לבוגרים", "podErr_rules": "בודק ההתאמה אינו זמין", "podErr_notext": "בקישור אין כותרת או תיאור לבדיקה: הדביקו פיד או דף פרק",
        "podErr_decode": "אי אפשר היה להשמיע את הפרק",
        # the brainstorming room on the radio (radio_room.py)
        "set_radio_room": "סיעור מוחות ברדיו",
        "hint_radio_room": "חדר חי ברדיו: מנחה, מבקר, מעצב, מנכ״ל ונציג לכל פרויקט קשור מדברים בשלושה סבבים. שום דבר לא נשלח לסשן בלי לחיצה שלך.",
        "room": "סיעור מוחות", "roomHint": "בוחרים נושא, והצוות מדבר עליו בשידור.",
        "roomSuggest": "נושאים שמחכים לך", "roomOwnPh": "או נושא משלך", "roomLength": "כמה זמן",
        "roomMin": "# דקות", "roomStart": "פותחים את החדר", "roomOpen": "החדר פתוח", "roomPaused": "עצרנו ומקשיבים לך",
        "roomSpeaking": "מדבר עכשיו", "roomWait": "רגע, אני רוצה לדבר", "roomWaitAsked": "עוצרים לפני הדובר הבא",
        "roomSum": "סכם עכשיו", "roomEnd": "סיום", "roomResume": "המשך בלי לדבר", "roomSayPh": "מה אתה רוצה להגיד לשולחן?",
        "roomSaySend": "שליחה לשולחן", "roomVoiceHint": "הקלטה קולית בנושא החדר בטלגרם נכנסת לפני הדובר הבא.",
        "roomPauseNote": "״רגע״ עוצר כל משפט שעוד לא עלה לשידור. המשפט שבשידור עכשיו מסתיים.",
        "roomProposals": "ההצעות על השולחן", "roomSummary": "סיכום החדר", "roomDecision": "ההחלטה",
        "roomTasks": "משימות מוצעות", "roomQuestions": "שאלות פתוחות", "roomSend": "שלח לסשן",
        "roomPick": "לאיזה סשן?", "roomSent": "נשלח אל", "roomSentHint": "יגיע לסשן בסוף הצעד שהוא עושה עכשיו. זו הצעה ולא אישור.",
        "roomNoTasks": "לא יצאו משימות מהחדר.", "roomClose": "סגירת הסיכום", "roomStopped": "החדר נעצר",
        "roomMinutes": "דקות קול משובח לחדרים היום", "roomRadioOff": "קודם מדליקים את הרדיו",
        "roomBusy": "כבר יש חדר פתוח", "roomFailed": "לא הצלחתי, נסו שוב", "roomNoSessions": "אין סשן פתוח לשלוח אליו",
        "roomCancel": "ביטול",
        # the Morning Brief home (brief.py): every line one language; "#" is a number word ("#~" before a noun, "#*" feminine)
        "tabBrief": "תדריך", "brief_morning": "תדריך הבוקר", "brief_noon": "תדריך הצהריים",
        "brief_evening": "תדריך הערב", "brief_night": "תדריך הלילה",
        "numsA": "אפס,אחד,שניים,שלושה,ארבעה,חמישה,שישה,שבעה,שמונה,תשעה,עשרה",
        "numsC": "אפס,אחד,שני,שלושה,ארבעה,חמישה,שישה,שבעה,שמונה,תשעה,עשרה",
        "numsF": "אפס,אחת,שתי,שלוש,ארבע,חמש,שש,שבע,שמונה,תשע,עשר",
        "hlMoving1": "קו אחד זז", "hlMoving": "#~ קווים זזים", "hlStuck1": "אחד תקוע", "hlStuck": "# תקועים",
        "hlWait1": "אחד מחכה לך", "hlWait": "# מחכים לך", "hlQuiet1": "קו אחד פתוח, אבל הוא לא זז כרגע",
        "hlQuiet": "#~ קווים פתוחים, ואף אחד מהם לא זז כרגע", "hlCalm": "הכול שקט: אין קו פעיל ואין משהו שמחכה לך.",
        "dot": ".",
        "since": "מאז שהסתכלת", "sinceFirst": "מעכשיו נסמן כאן מה השתנה מאז הביקור הקודם",
        "sDone1": "אחד הושלם", "sDone": "# הושלמו", "sStuck1": "אחד נתקע", "sStuck": "# נתקעו",
        "sNew1": "שאלה חדשה", "sNew": "#* שאלות חדשות", "sNone": "שום דבר לא השתנה",
        "waitNone": "אין כרגע משהו שמחכה לך", "bAnswer": "ענה", "bChoose": "בחר", "bClose": "סגור", "bDetails": "פרטים",
        "lines": "קווי העבודה", "linesNone": "אין כרגע קו עבודה פתוח",
        "linesNoSource": "עוד לא מחובר יומן משימות, אז הקווים נבנים מהסשנים בלבד. כשאין סשן פעיל, אין קו.",
        "staleBadge": "ישן", "liveBadge": "בעבודה", "mutedLine": "מושתק: העדכונים מהסשן הזה מקופלים",
        "why_stale_card": "הכרטיס עומד, ואף אחד לא עובד עליו.", "why_no_move": "לא זז יותר מיממה, ואף אחד לא עובד עליו.",
        "why_job_died": "עבודה ברקע נעצרה באמצע.", "why_check_failed": "בדיקה נכשלה.",
        "clDone": "{1} מתוך {2} הושלמו",
        "src_status": "משפט מצב מהסוכן", "src_ledger": "יומן המשימות", "src_session": "הצעד האחרון של הסשן",
        "agentFrom": "מאת הסוכן", "agentUnknown": "לא ידוע", "srcFrom": "מקור", "muteIt": "השתק את הסשן הזה", "unmuteIt": "בטל השתקה", "mutedOk": "הסשן הושתק בתדריך",
        "unmutedOk": "ההשתקה בוטלה",
        "noticed": "שמנו לב לבד", "noticedNone": "לא ראינו משהו חריג",
        "n_stale_card_ended": "הסשן נגמר והכרטיס נשאר פתוח", "n_stale_card_no_update": "כרטיס התקדמות שלא זז",
        "n_job_died": "עבודה ברקע נעצרה", "n_review_findings": "סקירה מצאה דברים לתקן", "n_check_failed": "בדיקה נכשלה",
        "n_note": "הערה מהבודק האוטומטי", "n_promise": "הבטחה שעוד לא קרתה", "n_source_down": "לא הצלחתי לקרוא את יומן המשימות",
        "teams": "צוותים", "tWork1": "אחד עובד", "tWork": "# עובדים", "tNone": "אף אחד לא עובד",
        "tWaste1": "אחד מבוזבז", "tWaste": "# מבוזבזים", "mac": "מק", "mac_ok": "רגוע", "mac_warn": "עמוס",
        "mac_critical": "בלחץ", "mac_unknown": "לא ידוע",
        "askPh": "שאל את הקוקפיט…", "q_waiting": "מה מחכה לי", "q_stuck": "מה תקוע", "q_working": "מי עובד",
        "q_changed": "מה השתנה", "q_mac": "איך המק",
        "askSoon": "תשובות לשאלות חופשיות יגיעו בקרוב. בינתיים אפשר לבחור שאלה קבועה.",
        "set_brief_home": "תדריך הבוקר כמסך הבית",
        "hint_brief_home": "תצוגה מקדימה: משפט אחד על המצב, מה השתנה, מה מחכה לך, וכל קו עבודה עם הגיל שלו. כבוי: המסך הרגיל.",
        "set_app_wide_cockpit": "קוקפיט רחב",
        "hint_app_wide_cockpit": "מציג באייפד ובמסך מתקפל שני אזורים זה לצד זה.",
        "set_app_summary_rows": "שורות סיכום במסך הנעילה",
        "hint_app_summary_rows": "מציג בכרטיס את המשימות הנסתרות כשורת „ועוד כמה״.",

        # the YouTube staff radio card (panel_youtube.py); "YouTube" stays a short isolated name inside Hebrew lines
        "ytMirrorTitle": "מראת הרדיו ביוטיוב",
        "ytMirrorHint": "אותם סיכומים וקול של הרדיו הביתי, כולל הלוח הנע. שידור לא רשום בכל פעם שהרדיו פועל.",
        "ytMirrorOn": "הפעלת מראה קבועה", "ytMirrorOff": "כיבוי המראה וביטול ההרשאה",
        "ytMirrorConfirm": "לאשר שידור של כל תוכן הרדיו הביתי בערוץ הזה, בכל פעם שהרדיו פועל?",
        "ytTitle": "שידורי הקוקפיט ב־" + _iso("YouTube"),
        "ytHint": "שידור לא רשום לשיתוף עם הצוות: רק התקדמות ומספרים של הפרויקטים שבחרת, בלי שום טקסט של משימות.",
        "ytSetup": "החיבור עוד לא הוגדר. צריך הגדרה חד־פעמית של פרטי ההתחברות במחשב.",
        "ytConnect": "התחברות עם " + _iso("YouTube"), "ytNotConnected": "לא מחובר", "ytReady": "מחובר",
        "ytLiveNow": "בשידור", "ytConnectedAs": "מחובר לערוץ",
        "ytOpenBrowser": "ההתחברות נפתחת בדפדפן. בסיום חוזרים לטלגרם.",
        "ytNoLive": "שידור חי עוד לא מופעל בערוץ. מפעילים אותו ב־" + _iso("YouTube\u00a0Studio") + ", וזה יכול לקחת עד יממה.",
        "ytLiveSince": "בשידור מאז", "ytLeft": "שעות שנותרו", "ytLeftLess": "נותרה פחות משעה",
        "ytEnded": "השידור האחרון הסתיים.", "ytFailed": "השידור האחרון נעצר בגלל תקלה.",
        "ytCleanup": "שידור קודם עוד לא נסגר. הוא ייסגר בפעולה הבאה.",
        "ytBridgeOff": "הגשר כבוי, ולכן אין שידור. אפשר רק לעצור או לנתק.",
        "ytEnable": "שידורי הקוקפיט פעילים", "ytEnableHint": "כבוי: אי אפשר לעלות לשידור, ושידור פעיל נעצר.",
        "ytProjects": "על אילו פרויקטים הצוות ישמע", "ytProjectsHint": "השם מההגדרות שלך או שם כללי, התקדמות ומספרים. בלי בחירה, הרדיו רק מזדהה.",
        "ytNoProjects": "אין עדיין פרויקטים לבחירה.", "ytUnspeakable": "השם לא יוקרא: יש בו אותיות לועזיות או ספרות.",
        "ytStart": "עלייה לשידור", "ytConfirm": "זה יעלה עכשיו שידור חי לא רשום בערוץ:",
        "ytConfirmGo": "כן, לעלות לשידור", "ytCancel": "ביטול", "ytStop": "עצירת השידור",
        "ytDisconnect": "ניתוק הערוץ", "ytWait": "רגע, זה לוקח כמה שניות…", "ytNotReady": "אי אפשר כרגע. רעננו ונסו שוב.",
        "ytReconnect": "צריך להתחבר מחדש לערוץ: ההתחברות השמורה שייכת ללקוח התחברות שכבר לא קיים כאן, או שיש כמה ואי אפשר לדעת איזה.",
        "ytRevokeFailed": "הערוץ נותק כאן, אבל גוגל לא אישרה את ביטול ההרשאה. אפשר להסיר אותה בדף הגישה של חשבון גוגל.",
        "ytNeedsEnable": "כדי לעלות לשידור צריך להפעיל את שידורי הקוקפיט.", "ytOpFailed": "הפעולה האחרונה לא הצליחה.",
        'ytErr_youtube_rules_invalid': 'שורה ברשימת המילים הפרטית אינה מילה פשוטה. תקן אותה ונסה שוב.', 'ytErr_youtube_disabled': 'שידורי הקוקפיט כבויים. הפעל אותם ונסה שוב.', 'ytErr_youtube_not_connected': 'הערוץ אינו מחובר. חבר אותו ונסה שוב.', 'ytErr_youtube_not_enabled': 'שידור חי אינו מופעל בערוץ. יש להפעיל אותו ב־\u2068YouTube Studio\u2069.', 'ytErr_youtube_api_failed': 'יוטיוב דחה את הבקשה. נסה שוב.', 'ytErr_youtube_job_failed': 'לא ניתן להפעיל את השידור ברקע. נסה שוב.', 'ytErr_youtube_busy': 'הרדיו עסוק בשינוי אחר. נסה שוב בעוד רגע.', 'ytErr_youtube_channel_changed': 'הערוץ השתנה. אשר שוב את העלייה לשידור.', 'ytErr_youtube_confirm_missing': 'אישור השידור פג או כבר נוצל. אשר שוב.', 'ytErr_youtube_macos_only': 'שידור זה פועל רק במחשב מק.', 'ytErr_youtube_stop_first': 'יש לעצור תחילה את שידורי הקוקפיט.', 'ytErr_youtube_no_client': 'לא הוגדרה התחברות לערוץ.', 'ytErr_youtube_connect_failed': 'ההתחברות לערוץ נכשלה. נסה שוב.', 'ytErr_youtube_connect_timeout': 'זמן ההתחברות הסתיים. נסה שוב.', 'ytErr_youtube_scope_missing': 'ההרשאה לערוץ לא ניתנה. התחבר שוב ואשר אותה.', 'ytErr_youtube_no_channel': 'לחשבון הזה אין ערוץ.', 'ytErr_youtube_secret_store_failed': 'לא ניתן לשמור את פרטי ההתחברות.', 'ytErr_youtube_connect_stale': 'ההתחברות כבר אינה בתוקף. התחבר שוב.',
        "bridge": "הגדרות הגשר", "saved": "נשמר", "partlySaved": "נשמר רק בחלק מהחשבונות", "on": "פועל", "off": "כבוי",
        "set_away": "מצב היעדרות", "hint_away": "סשן שעבד בשש השעות האחרונות ממשיך לבד לפי ההמלצה שלו, עד שתחזור. סשנים ישנים לא מתעוררים.",
        "set_voice_confirm": "אישור הקלטות", "hint_voice_confirm": "הקלטה קולית מתומללת ומחכה לאישור שלך לפני שליחה.",
        "set_delivery_end": "הודעות בסוף תשובה", "hint_delivery_end": "הודעה שנשלחת בזמן עבודה נמסרת לסשן רק כשהוא מסיים. כבוי: בין צעדים.",
        "set_status_repost": "כרטיס מצב חדש", "hint_status_repost": "כרטיס מצב חדש אחרי כל תשובה. כבוי: אותו כרטיס מתעדכן.",
        "set_agent_notices": "הודעה על כל סוכן", "hint_agent_notices": "הודעה בנושא על כל סוכן ברקע שסיים.",
        "set_agents_topic": "נושא נפרד לסוכנים", "hint_agents_topic": "הסוכנים ברקע מדווחים בנושא משלהם.",
        "set_general_offer": "לשאול לפני ניתוב", "hint_general_offer": "הודעה בנושא הכללי מציעה לאן לשלוח אותה לפני שהיא נשלחת.",
        "set_auto_account_switch": "החלפת חשבון אוטומטית", "hint_auto_account_switch": "מעבירה עבודה בין חשבונות ועשויה להפעיל סשנים מחדש ולהעביר תמלילים ביניהם.",
        "set_quota_auto_continue": "המשך אוטומטי אחרי חידוש מכסה", "hint_quota_auto_continue": "שולחת הוראת המשך לסשן כשהמכסה חוזרת.",
        "set_emergency_credit": "המשך חירום בקרדיט", "hint_emergency_credit": "כשהמכסה נגמרת, מציעה המשך בלחיצה עד $10 ביום לכל השיחות יחד. ההוצאה היא אומדן; תור שכבר התחיל עשוי לחרוג. כל הפעלה דורשת אישור חדש.",
        "set_session_auto_revive": "שחזור סשנים אוטומטי", "hint_session_auto_revive": "פותחת מחדש סשנים לאחר הפעלה מחדש או קריסה; ההפעלה נכנסת לתוקף לאחר הפעלת השירות מחדש.",
        "set_progress": "התקדמות חיה", "hint_progress": "שולחת עדכוני התקדמות לנושא בזמן העבודה.",
        "set_progress_details": "התקדמות מפורטת", "hint_progress_details": "כוללת פרטי פעולות, קטעי פלט ודברי הסוכן; כבוי מציג תוויות בלבד.",
        "set_compact_actions": "פעולות מקוצרות", "hint_compact_actions": "מציג רק מצב ותיאור קצר. אפשר לפתוח פרטים בכל כרטיס בנפרד.",
        "set_auto_cards": "כרטיסי התקדמות אוטומטיים", "hint_auto_cards": "השלבים מתעדכנים לפי העבודה, בלי להפריע לסוכן.",
        "set_auto_topic_names": "שמות נושאים אוטומטיים", "hint_auto_topic_names": "שולחת קטעי שיחה לספק הענן שעשוי לחייב את החשבון או הפרויקט המוגדר; עד 150 בקשות ביום אינן מגבלת הוצאה.",
        "set_state_emoji": "אימוג׳י לפי מצב העבודה", "hint_state_emoji": "משנה את האימוג׳י הגדול לפי הפעולה הנוכחית.",
        "set_activity": "פעילות חיה", "hint_activity": "כמה מהעבודה של הסוכן מוצג בנושא בזמן שהוא עובד: רק המילים שלו, הצעדים האחרונים, או הכול כולל פלט.",
        "set_live_activity_cockpit": "פעילות חיה בקוקפיט", "hint_live_activity_cockpit": "מציג פעולות כלים, שלבי התקדמות וסוכנים כאן, ובטלגרם משאיר הודעת מצב קצרה אחת לכל סשן.",
        "set_default_effort": "רמת חשיבה לסשנים חדשים", "hint_default_effort": "בחירת ברירת המחדל לכל חשבונות קלוד; הכפתור 🎚️ משנה רק את הסשן הנוכחי.",
        "effort_low": "נמוכה", "effort_medium": "בינונית", "effort_high": "גבוהה", "effort_xhigh": "גבוהה+", "effort_max": "מרבית", "effort_mixed": "שונה בין החשבונות",
        "act_quiet": "שקט", "act_normal": "רגיל", "act_full": "הכול",
        "coldOn": "פעיל", "set_cold_compact": "כיווץ לפני הודעה אחרי הפסקה", "hint_cold_compact": "אחרי שעה בלי פעילות המודל, תוקף העותק השמור של השיחה (מטמון) פג וכל השיחה נקראת שוב במחיר מלא. כיווץ לפני השליחה מוזיל את ההודעה הזו ואת הבאות, אך לוקח כדקה ומשאיר רק סיכום של פרטים ישנים.",
        "set_auto_compact": "דחיסה אוטומטית", "hint_auto_compact": "גודל ההקשר שבו סשן נדחס לבד.",
        "tabMe": "בשבילי", "tabAgents": "סוכנים", "appFrame": "כלי פרטי", "activityAgent": "סוכן",
        "heroOne": "שאלה מחכה לך", "heroMany": "שאלות מחכות לך",
        "heroHint": "החלטות ושאלות של הסוכנים שמחכות לתשובה שלך",
        "questionClosed": "השאלה הזאת כבר נסגרה", "showOpenQuestions": "הצגת השאלות הפתוחות",
        "allClear": "הכל רגוע", "allClearHint": "שום דבר לא מחכה לך כרגע. הסוכנים ממשיכים לעבוד.",
        "cardsAsk": "הסוכנים שואלים", "cardsShow": "עדכונים מהסוכנים", "cardsOld": "כרטיסים ישנים",
        "cardsOldHint": "כרטיסים של סשנים שהסתיימו, עדכונים שנסגרו או התקדמות שהושלמה לפני חצי שעה לפחות.",
        "cardUpdated": "עודכן", "cardStale": "לא מעודכן", "cardAuto": "כרטיס אוטומטי מרשימת המשימות", "cardClosed": "נסגר אוטומטית", "cardDismiss": "הסרה",
        "cardWorking": "עובדים עכשיו", "cardNow": "עכשיו", "cardTask": "המשימה הפעילה", "liveActivity": "פעילות חיה",
        "cardTextMore": "הצגת הטקסט המלא", "cardTextLess": "קיצור הטקסט",
        "cardChoose": "בחר", "cardEnlarge": "הגדלת התמונה", "cardImageClose": "סגירת התמונה",
        "cardImageHint": "אפשר לצבוט להגדלה ולגרור את התמונה", "cardImageError": "התמונה לא זמינה כרגע",
        "k_progress": "התקדמות", "k_choice": "בחירה", "k_ask": "תשובה קצרה", "k_link": "קישור", "k_numbers": "נתונים",
        "cardSent": "התשובה ממתינה למסירה לסוכן", "cardAlready": "כבר נענה", "cardGone": "הכרטיס כבר נסגר",
        "deliveryTitle": "מסירת תשובות", "deliveryQueued": "ממתינה למסירה לסוכן",
        "deliveryFlight": "בניסיון מסירה — עדיין אין אישור קליטה", "deliveryDone": "הסוכן קיבל את התשובה",
        "deliveryUnknown": "אין עדיין הוכחת קליטה", "deliveryRetry": "ממתין: השיחה אינה נגישה; יבוצע ניסיון נוסף",
        "queueFull": "הסוכן לא מפנה את התור. התשובה לא נוספה; אפשר לנסות שוב לאחר שהתור יתפנה.",
        "answerPh": "התשובה שלך…", "sayPh": "לכתוב לסוכן הזה…", "saySent": "ההודעה ממתינה למסירה לסוכן", "sayGone": "הסוכן הזה כבר לא פעיל", "waitingAgent": "ממתין לסוכן", "editIt": "עריכה", "cancelIt": "ביטול", "unsaid": "ההודעה בוטלה", "unsayLate": "ההודעה כבר בניסיון מסירה ואי אפשר לערוך אותה", "sidHint": "המזהה הקבוע של הסוכן", "send": "שליחה", "openLink": "פתיחה", "wrote": "כתבת",
        "step": "שלב", "of": "מתוך", "allDone": "כל השלבים הושלמו", "allAgents": "לכל הסוכנים",
        "backToV2": "חזרה לעיצוב הקוקפיט החדש", "backToV2Button": "חזרה עכשיו",
    },
    "en": {
        "hooksTitle": "Session connection health", "hooksBroken": "Connection settings changed. Session replies may be delayed.",
        "hooksRestore": "Restore", "hooksRestored": "Connection restored. Already open sessions need a reload.",
        "hooksReload": "Reload sessions", "hooksHint": "Only idle sessions with older settings reload. Working sessions, drafts and dialogs are skipped.",
        "hooksRunning": "Reloading idle sessions…", "hooksReloaded": "Reloaded", "hooksSkipped": "Skipped", "hooksReloadFailed": "Reload did not finish. You can try again.",
        "reload_busy": "Working, dialog or draft", "reload_current": "Already current", "reload_no_pane": "No available pane",
        "hooksNReloaded": "Reloaded: #", "hooksNCurrent": "Up to date: #", "hooksNBusy": "Working now, skipped: #",
        "hooksNOther": "Need a check: #", "hooksNone": "No sessions to reload",
        "reload_unknown": "Cannot verify idle state", "reload_failed": "Reload did not finish", "reload_dialog": "Waiting for a terminal approval", "reload_not_live": "No longer live",
        "radioPlay": "Listen", "radioPause": "Pause", "listenSafari": "Listen in Safari", "listenFailed": "Could not create the link. Try again", "listenRevoke": "Revoke listening links", "listenRevokeHint": "Safari listening links are active. Revoking closes all of them", "listenRevoked": "Listening links revoked", "devicesTitle": "Connected widgets", "devicesHint": "Devices whose widgets read the Cockpit. Revoking works at once", "devicesAdded": "Added", "devicesRevoke": "Revoke", "devicesRevoked": "Device revoked", "devicesFailed": "Could not revoke. Try again", "radioLive": "Live",
        "radioPlayerWaiting": "Waiting for audio…", "radioPlayerOff": "Radio is off", "radioPlayerError": "Playback stopped. Try again",
        "myMusic": "My music", "myMusicHint": "My YouTube Music playlists and likes", "myMusicLiked": "Liked music", "myMusicEmpty": "No playlists to show", "myMusicFailed": "Music could not be loaded", "myMusicPlay": "Play", "myMusicPause": "Pause", "myMusicNext": "Next",
        "macTitle": "This Mac", "macUnavailable": "Information is not available yet", "macMemory": "Memory pressure and estimated available memory", "macCpu": "CPU load over one, five and fifteen minutes", "macCores": "cores", "macDisk": "Free space on the system volume, excluding purgeable space and other volumes", "macBattery": "Battery", "macPower": "Connected to power", "macOnBattery": "On battery", "macNoBattery": "No battery", "macUptime": "Time since startup", "macHours": "hours", "macGb": "GB", "macTopCpu": "Highest lifetime average CPU use", "macTopMemory": "Highest memory use", "macTest": "Test run", "macEncoder": "Video encoding", "macAgent": "Agent", "macBrowser": "Browser", "macBuild": "Build tool", "macOther": "Other process", "macOk": "Everything looks normal", "macWarn": "Check the computer status", "macCritical": "The computer needs attention", "macMemoryOk": "Memory is available", "macMemoryWarn": "Memory pressure is rising", "macMemoryCritical": "Memory pressure is high", "macCpuOk": "Load is low for the core count", "macCpuWarn": "The five minute load is approaching the core count", "macCpuCritical": "The five minute load is high for the core count", "macDiskOk": "Free space is available", "macDiskWarn": "Free space is getting low", "macDiskCritical": "Very little free space remains", "macBatteryOk": "Battery level is normal", "macBatteryWarn": "Battery is running low", "macBatteryCritical": "Battery is almost empty",
        "moreTabs": "More", "title": "Cockpit", "here": "This topic", "all": "Everything",
        "waiting": "Waiting for you", "nothing": "No decisions waiting for you",
        "nothingHint": "When an agent asks you something, it shows up here and one tap answers it.",
        "reminders": "Reminders", "openTasks": "Open tasks", "sessions": "Working now", "none": "No session is working right now",
        "noneHint": "A session that starts working shows up here with the step it is on. Tap a row for details.",
        "endedList": "Ended sessions", "quota": "Quota", "banked": "Banked resets", "bank": "Reset bank", "bankHint": "How many saved quota resets you hold. Agents see this and will recommend when one is worth spending.", "bankUnset": "Not set", "bankExpires": "Expires", "bankClear": "Clear", "bankLeft": "# days left", "bankLeft1": "1 day left", "bankToday": "Expires today", "bankExpired": "Expired", "less": "Less", "more1": "More", "week": "Weekly",
        "reset": "Resets", "working": "Working", "idle": "Idle", "ended": "Ended",
        "ctx": "Context", "model": "Model", "account": "Account", "last": "Last activity", "folder": "Folder", "branch": "Branch",
        "bg1": "background task", "bg": "background tasks", "bgNone": "No background tasks",
        "now": "now", "updated": "Updated", "updating": "Updating…", "refresh": "Refresh",
        "pull": "Pull down to refresh", "release": "Release to refresh",
        "denied": "Access denied", "renderFailed": "Could not show the board", "open": "Open the panel from inside Telegram",
        "reopen": "Your Telegram login expired. Close the panel and open it again",
        "busy": "Busy, try again", "failed": "Could not send, try again", "retry": "Try again",
        "offline": "No connection right now, trying again shortly",
        "gone": "This decision is already closed", "already": "Already answered", "sent": "Your answer is queued for delivery",
        "chose": "You chose", "notePh": "Note for the session (optional)", "addNote": "Add a note",
        "topic": "Topic", "source": "Original", "hideSource": "Hide original", "translating": "Translating…",
        "tapNote": "Your answer here counts as your approval, sensitive actions included, and only for what the question says",
        "radio": "Team radio", "radioOn": "On", "radioOff": "Off", "radioStart": "Start",
        "radioStop": "Stop", "radioWait": "One moment, this takes a few seconds…",
        "radioHint": "A short spoken update on what the team is doing, in the team channel.",
        "session": "This topic's session", "board": "Topic board", "checks": "Tasks", "links": "Links",
        "emptyHere": "Nothing waiting, no reminders and no tasks in this topic",
        "emptyHereHint": "All sessions, decisions and quota are under \"Everything\".", "goAll": "Go to everything",
        "nWaiting": "Waiting for you", "nWorking": "Working", "nIdle": "Idle",
        "noSession": "This topic's session is no longer active",
        "digest": "Since you last looked", "gotIt": "Got it",
        "dNew1": "new decision", "dNew": "new decisions", "dDone1": "session finished working", "dDone": "sessions finished working",
        "dEnded1": "session ended", "dEnded": "sessions ended", "dStarted1": "new session", "dStarted": "new sessions",
        "settings": "Settings", "defModel": "Model for new sessions",
        "defModelHint": "Saved in Claude Code's settings. Open sessions keep their own model.",
        "notSet": "Not set", "modelSaved": "New sessions will start with", "settingsBroken": "The settings file is damaged; nothing was changed",
        "compactAt": "Auto-compact at ", "compactOwn": "Auto-compact for this session", "compactGlobal": "Same as all sessions", "compactNever": "Never", "compactOff": "off",
        "screen": "Live screen", "screenHide": "Hide screen", "screenNone": "This session has no screen to show right now", "recent": "Finished recently", "nRecent": "Finished recently", "idleList": "Idle", "leftList": "Left behind", "leftHint": "Open sessions nobody touched for over a day. Write to one to continue it, or close its topic. Closing deletes nothing: writing in the topic opens it again.", "closeTopic": "Close topic", "closeAll": "Close all", "sure": "Tap again to confirm", "closedN": "Closed", "notClosed": "Not closed, you can try again", "doneRem": "Mark as done", "remCleared": "Reminder cleared", "clearOld": "Clear the old ones", "lastReply": "Last reply",
        "openTopic": "Topic", "openTg": "Open in Telegram", "newBadge": "New", "markRead": "Mark all as seen", "markedRead": "All marked as seen",
        "staleList": "No longer relevant",
        "staleHint": "Questions that no longer wait on you: their session ended, or they were asked again later.",
        "oldList": "Older", "oldHint": "Items that have waited for you more than three days. You can answer them as usual.",
        "clearAll": "Clear all", "cleared": "Cleared", "why_ended": "Session ended", "why_replaced": "Asked again",
        "more": "More",
        "radioSay": "Say on the radio", "radioSayPh": "What should the radio say now?", "radioSayBtn": "Say it now",
        "radioSaid": "The radio will say it in a moment", "radioIsOff": "The radio is off",
        "set_voice_digest": "Voice digest",
        "hint_voice_digest": "Radio updates in one voice note. Quiet at night and during configured Sabbath and holiday windows.",
        "digest_off": "Off", "digest_important": "Important only", "digest_hourly": "Hourly",
        "digest_every_3_hours": "Every 3 hours",
        "set_radio_every_reply": "Radio speaks every reply",
        "hint_radio_every_reply": "Every session's reply is said briefly on the radio. Off: only replies to voice notes.",
        "set_radio_board": "Project show on the radio",
        "set_radio_music": "Private listening music",
        "hint_radio_music": "Music and podcasts between broadcasts, lowered under speech. Private listening only; silent on Shabbat.",
        "musicNow": "Now playing", "musicNone": "No music playing now", "musicLike": "Like", "musicSkip": "Skip",
        "programs": "Radio programs", "programHint": "The private running order. Edit, reorder, and start it now.", "programStart": "Start now", "programSave": "Save", "programUp": "Up", "programDown": "Down", "programMinutes": "minutes",
        "hint_radio_board": "The radio's picture moves: a column per project, a task wall per project (done, in work, next up) and a news ticker. Off: the usual card.",
        "radioMinutes": "Paid voice minutes today", "radioOf": "of", "radioBudgetUnavailable": "Voice budget unavailable", "radioConfiguredCap": "Configured cap", "radioActiveCap": "Active cap",
        "radioListening": "A listener is detected", "radioNotListening": "No listener detected — the free voice will be selected for the next bulletin",
        "radioListenerUnknown": "Listening could not be verified — the free voice will be selected for the next bulletin",
        "radioDetectionOff": "Listening detection unavailable — fallback cap applies",
        "radioListenerPlayer": "Cockpit player", "radioListenerCall": "Telegram live stream", "radioModeGroup": "Mode: live stream in the group and private listening", "radioModePrivate": "Mode: private listening only", "radioModeNext": "The change applies the next time the radio starts",
        # the podcast queue on the radio (radio_podcast.py)
        "podcast": "Podcast on the radio", "podIdle": "Nothing is playing", "podLoading": "Downloading the episode…",
        "podRadioOff": "The radio is off. The episode goes on the air when you turn it on", "podPlay": "Resume",
        "podPause": "Pause", "podSkip": "Skip", "podStop": "Stop", "podRemove": "Remove", "podQueue": "Queue",
        "podAdd": "Add an episode", "podFeeds": "Pick a podcast", "podFetching": "Getting the episodes…",
        "podNoEpisodes": "No playable episodes in this feed", "podBack": "Back to the list",
        "podLinkPh": "Paste an episode or feed link", "podLinkBtn": "Add to the queue", "podAdded": "Added to the queue",
        "podErr_bad": "That request is not valid", "podErr_url": "That link is not allowed: https links to public sites only",
        "podErr_fetch": "Could not get the podcast", "podErr_feed": "That is not a podcast feed with a playable episode",
        "podErr_too_big": "That file is too large", "podErr_unknown": "No such feed or episode", "podErr_full": "The queue is full",
        "podErr_dup": "Already in the queue", "podErr_unsuitable": "The episode does not pass the radio's suitability rules",
        "podErr_explicit": "The episode is marked explicit", "podErr_rules": "The suitability checker is not available", "podErr_notext": "That link has no title or description to check: paste a feed or an episode page",
        "podErr_decode": "The episode could not be played",
        "set_radio_room": "Brainstorm room on the radio",
        "hint_radio_room": "A live room on the radio: a moderator, a critic, a designer, a CEO and a delegate per related project talk in three rounds. Nothing goes to a session without your tap.",
        "room": "Brainstorm", "roomHint": "Pick a topic and the team talks it through on the air.",
        "roomSuggest": "Topics waiting for you", "roomOwnPh": "Or your own topic", "roomLength": "How long",
        "roomMin": "# minutes", "roomStart": "Open the room", "roomOpen": "The room is open", "roomPaused": "Paused, listening to you",
        "roomSpeaking": "Speaking now", "roomWait": "One moment, I want to talk", "roomWaitAsked": "Stopping before the next speaker",
        "roomSum": "Sum up now", "roomEnd": "End", "roomResume": "Go on without me", "roomSayPh": "What do you want to tell the table?",
        "roomSaySend": "Send to the table", "roomVoiceHint": "A voice note in the room's Telegram topic joins before the next speaker.",
        "roomPauseNote": "\u201cOne moment\u201d stops every line not yet on the air. The line on the air now finishes.",
        "roomProposals": "Proposals on the table", "roomSummary": "Room summary", "roomDecision": "The decision",
        "roomTasks": "Proposed tasks", "roomQuestions": "Open questions", "roomSend": "Send to session",
        "roomPick": "Which session?", "roomSent": "Sent to", "roomSentHint": "It reaches the session when its current step ends. It is a proposal, not an approval.",
        "roomNoTasks": "The room proposed no tasks.", "roomClose": "Close the summary", "roomStopped": "The room stopped",
        "roomMinutes": "Paid voice minutes for rooms today", "roomRadioOff": "Turn the radio on first",
        "roomBusy": "A room is already open", "roomFailed": "That did not work, try again", "roomNoSessions": "No open session to send to",
        "roomCancel": "Cancel",
        "tabBrief": "Brief", "brief_morning": "Morning brief", "brief_noon": "Midday brief",
        "brief_evening": "Evening brief", "brief_night": "Night brief",
        "numsA": "zero,one,two,three,four,five,six,seven,eight,nine,ten",
        "numsC": "zero,one,two,three,four,five,six,seven,eight,nine,ten",
        "numsF": "zero,one,two,three,four,five,six,seven,eight,nine,ten",
        "hlMoving1": "one line moving", "hlMoving": "#~ lines moving", "hlStuck1": "one stuck", "hlStuck": "# stuck",
        "hlWait1": "one waiting for you", "hlWait": "# waiting for you", "hlQuiet1": "one line open, but it is not moving now",
        "hlQuiet": "#~ lines open, and none of them is moving now", "hlCalm": "All quiet: no active line and nothing waiting for you.",
        "dot": ".",
        "since": "Since you looked", "sinceFirst": "From now on this shows what changed since your last visit",
        "sDone1": "one done", "sDone": "# done", "sStuck1": "one got stuck", "sStuck": "# got stuck",
        "sNew1": "a new question", "sNew": "#* new questions", "sNone": "nothing changed",
        "waitNone": "Nothing is waiting for you right now", "bAnswer": "Answer", "bChoose": "Choose", "bClose": "Close", "bDetails": "Details",
        "lines": "Work lines", "linesNone": "No open work line right now",
        "linesNoSource": "No task ledger is connected yet, so lines come from the sessions alone. With no live session there is no line.",
        "staleBadge": "Old", "liveBadge": "Working", "mutedLine": "Muted: this session's updates are folded",
        "why_stale_card": "The card stands still, and nobody is working on it.", "why_no_move": "No movement for over a day, and nobody is on it.",
        "why_job_died": "A background job stopped halfway.", "why_check_failed": "A check failed.",
        "clDone": "{1} of {2} done",
        "src_status": "The agent's status sentence", "src_ledger": "The task ledger", "src_session": "The session's latest step",
        "agentFrom": "From agent", "agentUnknown": "Unknown", "srcFrom": "Source", "muteIt": "Mute this session", "unmuteIt": "Unmute", "mutedOk": "Session muted in the brief",
        "unmutedOk": "Unmuted",
        "noticed": "Noticed by itself", "noticedNone": "Nothing unusual spotted",
        "n_stale_card_ended": "The session ended and its card stayed open", "n_stale_card_no_update": "A progress card that stopped moving",
        "n_job_died": "A background job stopped", "n_review_findings": "A review found things to fix", "n_check_failed": "A check failed",
        "n_note": "A note from the watcher", "n_promise": "A promise that has not happened yet", "n_source_down": "Could not read the task ledger",
        "teams": "Teams", "tWork1": "one working", "tWork": "# working", "tNone": "nobody working",
        "tWaste1": "one about to go unused", "tWaste": "# about to go unused", "mac": "Mac", "mac_ok": "calm", "mac_warn": "busy",
        "mac_critical": "strained", "mac_unknown": "unknown",
        "askPh": "Ask the Cockpit…", "q_waiting": "What waits for me", "q_stuck": "What is stuck", "q_working": "Who is working",
        "q_changed": "What changed", "q_mac": "How is the Mac",
        "askSoon": "Answers to free questions are coming soon. Meanwhile, pick a fixed question.",
        "set_brief_home": "Morning brief as the home screen",
        "hint_brief_home": "Preview: one sentence on where things stand, what changed, what waits for you, and every work line with its age. Off: the usual home.",
        "set_app_wide_cockpit": "Wide Cockpit",
        "hint_app_wide_cockpit": "Shows two Cockpit panes side by side on iPad and foldable screens.",
        "set_app_summary_rows": "Lock-screen summary rows",
        "hint_app_summary_rows": "Lists hidden tasks on the card as a “+N more” row.",
        "ytMirrorTitle": "Home radio mirror on YouTube",
        "ytMirrorHint": "The home radio's full summaries and voice, with the moving board. Unlisted whenever the radio is on.",
        "ytMirrorOn": "Enable standing mirror", "ytMirrorOff": "Stop mirror and revoke permission",
        "ytMirrorConfirm": "Broadcast all home radio content on this channel whenever the radio is on?",
        "ytTitle": "Staff radio on YouTube",
        "ytHint": "An unlisted stream to share with the staff: only progress and numbers of the projects you pick, never any task text.",
        "ytSetup": "Not set up yet: the sign-in client needs a one-time setup on the computer.",
        "ytConnect": "Connect with YouTube", "ytNotConnected": "Not connected", "ytReady": "Connected",
        "ytLiveNow": "Live", "ytConnectedAs": "Connected to the channel",
        "ytOpenBrowser": "Sign-in opens in the browser. Come back to Telegram when done.",
        "ytNoLive": "Live streaming is not enabled on the channel yet. Turn it on in YouTube Studio; it can take up to 24 hours.",
        "ytLiveSince": "Live since", "ytLeft": "Hours left", "ytLeftLess": "Less than an hour left",
        "ytEnded": "The last broadcast has ended.", "ytFailed": "The last broadcast stopped because of a problem.",
        "ytCleanup": "An earlier broadcast is not closed yet; the next action closes it.",
        "ytBridgeOff": "The bridge is off, so nothing is streamed. You can only stop or disconnect.",
        "ytEnable": "Staff radio on", "ytEnableHint": "Off: going live is not possible and a live stream stops.",
        "ytProjects": "Projects the staff hear about", "ytProjectsHint": "Your staff label or a neutral name, progress and counts. With none picked, the radio only says who it is.",
        "ytNoProjects": "No projects to pick yet.", "ytUnspeakable": "This name will not be read: it has Latin letters or digits.",
        "ytStart": "Go live", "ytConfirm": "This starts an unlisted live stream now on the channel:",
        "ytConfirmGo": "Yes, go live", "ytCancel": "Cancel", "ytStop": "Stop the broadcast",
        "ytDisconnect": "Disconnect the channel", "ytWait": "One moment, this takes a few seconds…",
        "ytNotReady": "Not possible right now. Refresh and try again.",
        "ytReconnect": "Connect the channel again: the saved sign-in belongs to a sign-in client that is no longer here, or there are several and it is not clear which.",
        "ytRevokeFailed": "The channel is disconnected here, but Google did not confirm removing the permission. You can remove it on your Google account's access page.",
        "ytNeedsEnable": "Turn the staff radio on to go live.", "ytOpFailed": "The last action did not work.",
        'ytErr_youtube_rules_invalid': 'A line in the private word list is not a plain word. Fix it and try again.', 'ytErr_youtube_disabled': 'Turn on the staff radio and try again.', 'ytErr_youtube_not_connected': 'Connect the channel and try again.', 'ytErr_youtube_not_enabled': 'Live streaming is not enabled on this channel. Enable it in YouTube Studio.', 'ytErr_youtube_api_failed': 'YouTube declined the request. Try again.', 'ytErr_youtube_job_failed': 'Could not start the broadcast in the background. Try again.', 'ytErr_youtube_busy': 'The radio is handling another change. Try again shortly.', 'ytErr_youtube_channel_changed': 'The channel changed. Confirm going live again.', 'ytErr_youtube_confirm_missing': 'The go-live confirmation expired or was already used. Confirm again.', 'ytErr_youtube_macos_only': 'This broadcast requires a Mac.', 'ytErr_youtube_stop_first': 'Stop the staff radio first.', 'ytErr_youtube_no_client': 'The channel sign-in is not configured.', 'ytErr_youtube_connect_failed': 'Channel sign-in failed. Try again.', 'ytErr_youtube_connect_timeout': 'Sign-in timed out. Try again.', 'ytErr_youtube_scope_missing': 'Channel permission was not granted. Connect again and allow it.', 'ytErr_youtube_no_channel': 'This account has no channel.', 'ytErr_youtube_secret_store_failed': 'Could not save the sign-in details.', 'ytErr_youtube_connect_stale': 'The sign-in is no longer valid. Connect again.',
        "bridge": "Bridge settings", "saved": "Saved", "partlySaved": "Saved for only some accounts", "on": "On", "off": "Off",
        "set_away": "Away mode", "hint_away": "A session that worked in the last six hours carries on with its own recommendation until you are back. Older sessions are not woken.",
        "set_voice_confirm": "Confirm voice notes", "hint_voice_confirm": "A voice note is transcribed and waits for your OK before it is sent.",
        "set_delivery_end": "Deliver at the end of a reply", "hint_delivery_end": "A message sent while a session works reaches it when it finishes. Off: between steps.",
        "set_status_repost": "New status card", "hint_status_repost": "A new status card after every reply. Off: the same card updates.",
        "set_agent_notices": "Notice per agent", "hint_agent_notices": "A notice in the topic for each background agent that finished.",
        "set_agents_topic": "Separate agents topic", "hint_agents_topic": "Background agents report in a topic of their own.",
        "set_general_offer": "Ask before routing", "hint_general_offer": "A message in General suggests where it should go before it is sent.",
        "set_auto_account_switch": "Automatic account switching", "hint_auto_account_switch": "Moves work between accounts and may restart sessions and transfer transcripts between them.",
        "set_quota_auto_continue": "Continue when quota returns", "hint_quota_auto_continue": "Sends a continue instruction to a session when quota returns.",
        "set_emergency_credit": "Emergency credit", "hint_emergency_credit": "Offers one-tap continuation when subscription quota runs out, up to an estimated $10 per local day across sessions. A running turn can exceed the cap. Each activation needs a new approval.",
        "set_session_auto_revive": "Automatic session recovery", "hint_session_auto_revive": "Reopens sessions after a reboot or crash; enabling takes effect after restarting the service.",
        "set_progress": "Live progress", "hint_progress": "Sends progress updates to the topic while work is running.",
        "set_progress_details": "Detailed progress", "hint_progress_details": "Includes action details, output previews and agent text; off shows labels only.",
        "set_compact_actions": "Compact actions", "hint_compact_actions": "Shows only status and the short description. Each card can be expanded separately.",
        "set_auto_cards": "Automatic progress cards", "hint_auto_cards": "Keeps progress steps current from activity without interrupting the agent.",
        "set_auto_topic_names": "Automatic topic names", "hint_auto_topic_names": "May send conversation excerpts to the cloud provider and charge the configured account or project; the 150 daily request limit is not a spending limit.",
        "set_state_emoji": "Emoji by work state", "hint_state_emoji": "Changes the large working emoji to match the current activity.",
        "set_activity": "Live activity", "hint_activity": "How much of an agent's work its topic shows while it works: only its words, the latest steps, or everything with output.",
        "set_live_activity_cockpit": "Live activity in the Cockpit", "hint_live_activity_cockpit": "Shows tool actions, progress steps, and agents here while Telegram keeps one compact status message per session.",
        "set_default_effort": "Thinking level for new sessions", "hint_default_effort": "Sets the default for every Claude account; the 🎚️ button changes only the current session.",
        "effort_low": "Low", "effort_medium": "Medium", "effort_high": "High", "effort_xhigh": "Very high", "effort_max": "Max", "effort_mixed": "Different between accounts",
        "act_quiet": "Quiet", "act_normal": "Normal", "act_full": "Everything",
        "coldOn": "On", "set_cold_compact": "Compact before a message after a break", "hint_cold_compact": "After an hour without API activity, the saved conversation copy (cache) has expired, so the whole conversation is read again at full price. Compacting first makes this message and the next ones cheaper, but takes about a minute and keeps only a summary of older details.",
        "set_auto_compact": "Auto-compact", "hint_auto_compact": "The context size at which a session compacts itself.",
        "tabMe": "For me", "tabAgents": "Agents", "appFrame": "Private tool", "activityAgent": "Agent",
        "heroOne": "question waiting for you", "heroMany": "questions waiting for you",
        "heroHint": "Decisions and questions from your agents that need your answer",
        "questionClosed": "This question was already closed", "showOpenQuestions": "Show open questions",
        "allClear": "All clear", "allClearHint": "Nothing is waiting for you right now. Your agents keep working.",
        "cardsAsk": "Your agents ask", "cardsShow": "Updates from your agents", "cardsOld": "Older cards",
        "cardsOldHint": "Cards from ended sessions, closed updates, or progress completed at least half an hour ago.",
        "cardUpdated": "Updated", "cardStale": "Stale", "cardAuto": "Automatic card from the task list", "cardClosed": "Closed automatically", "cardDismiss": "Dismiss",
        "cardWorking": "Working now", "cardNow": "Now", "cardTask": "Current task", "liveActivity": "Live activity",
        "cardTextMore": "Show full text", "cardTextLess": "Shorten text",
        "cardChoose": "Choose", "cardEnlarge": "Enlarge image", "cardImageClose": "Close image",
        "cardImageHint": "Pinch to zoom and drag to pan", "cardImageError": "Image is unavailable right now",
        "k_progress": "Progress", "k_choice": "Choice", "k_ask": "Short answer", "k_link": "Link", "k_numbers": "Figures",
        "cardSent": "Your answer is queued for delivery", "cardAlready": "Already answered", "cardGone": "This card is already closed",
        "deliveryTitle": "Answer delivery", "deliveryQueued": "Queued for delivery",
        "deliveryFlight": "In flight — receipt not confirmed", "deliveryDone": "The agent received your answer",
        "deliveryUnknown": "Receipt not confirmed", "deliveryRetry": "Waiting: session not reachable; delivery will be retried",
        "queueFull": "This agent is not draining its queue. Your answer was not added; try again after it drains.",
        "answerPh": "Your answer…", "sayPh": "Write to this agent…", "saySent": "Your message is queued for delivery", "sayGone": "This agent is no longer active", "waitingAgent": "Waiting for the agent", "editIt": "Edit", "cancelIt": "Cancel", "unsaid": "Message cancelled", "unsayLate": "This message is already in flight and cannot be edited", "sidHint": "The agent's fixed id", "send": "Send", "openLink": "Open", "wrote": "You wrote",
        "step": "Step", "of": "of", "allDone": "All steps done", "allAgents": "All agents",
        "backToV2": "Back to the new Cockpit design", "backToV2Button": "Switch back now",
    },
}

# Shared session actions use the bot's canonical English/Hebrew pair.
for _lang in STRINGS:
    STRINGS[_lang].update({"forkAction": t("fork_action", lang=_lang),
                           "forkConfirm": t("fork_confirm", lang=_lang),
                           "forkStarting": t("fork_starting", lang=_lang)})

STRINGS["he"].update({"usageTitle":"צריכת סשנים", "usageToday":"היום", "usageWeek":"השבוע", "usageSession":"כל הסשן", "usageTokens":"אסימונים", "usageCost":"עלות משוערת", "usageInput":"קלט", "usageOutput":"פלט", "usageWrite":"כתיבה למטמון", "usageRead":"קריאה מהמטמון", "usageShare":"חלק המטמון", "usageTop":"הסשנים המובילים היום", "usageUnknown":"לא הוגדר מחיר", "usageUnpriced":"אסימונים ללא מחיר", "usageModelUnknown":"דגם לא מזוהה", "usageHint":"כולל רק סשנים שמחוברים לגשר. הערכה לפי המחירים המוגדרים; מחירי ברירת המחדל עשויים להיות לא עדכניים. המנוי בתשלום קבוע."})
STRINGS["en"].update({"usageTitle":"Session usage", "usageToday":"Today", "usageWeek":"This week", "usageSession":"Whole session", "usageTokens":"Tokens", "usageCost":"Estimated cost", "usageInput":"Input", "usageOutput":"Output", "usageWrite":"Cache write", "usageRead":"Cache read", "usageShare":"Cache share", "usageTop":"Top sessions today", "usageUnknown":"no price set", "usageUnpriced":"Unpriced tokens", "usageModelUnknown":"Unknown model", "usageHint":"Covers only sessions connected to the bridge. Estimate from configured prices; defaults may be outdated. The subscription is a flat fee."})

STRINGS["he"].update({"compactDetails":"פרטים", "compactExpand":"הרחבה", "compactCollapse":"צמצום", "compactSettings":"אפשרויות נוספות", "compactEnabled":"פעילות"})
STRINGS["en"].update({"compactDetails":"Details", "compactExpand":"Expand", "compactCollapse":"Collapse", "compactSettings":"More options", "compactEnabled":"enabled"})
_REQUEST_STRINGS = {
    "he": {"requestsTitle": "בקשות", "requestsProject": "לפי פרויקט", "requestsAll": "כל הפרויקטים",
           "oldHint": "פריטים שמחכים זמן רב. תזכורות עוברות לכאן שלושה ימים אחרי המועד, או אחרי שבועיים בלי מועד. אפשר להסיר או להחזיר אותן לרשימה הפעילה.",
           "requestsYou": "נדרש ממך", "requestsTeam": "בטיפול הצוות", "requestsDone": "בוצע",
           "requestDone": "בוצע", "requestDrop": "לא רלוונטי", "requestPush": "לקדם", "requestTouch": "עדיין רלוונטי", "requestConfirm": "לאשר?", "requestClosed": "הבקשה כבר נסגרה", "requestSaved": "הפעולה נשמרה", "requestsOlder": "ישנים", "requestsReviewOld": "לעבור על הבקשות הישנות",
           "requestsSteps": "שלבי הטיפול", "requestsEmpty": "אין בקשות פתוחות כרגע",
           "requestsNeedsYou": "מחכות לך", "requestsShowAll": "לכל {n} הבקשות",
           "requestsMore": "עוד {n} בקשות", "requestsOpenCount": "{n} פתוחות",
           "requestsOwnerCount": "{n} מחכות לך", "requestsBackAll": "חזרה לכל הבקשות",
           "requestsOwnerEmpty": "אין בקשות שמחכות לך כרגע",
           "restoreRem": "להחזיר לרשימה הפעילה", "remRestored": "התזכורת חזרה לרשימה הפעילה"},
    "en": {"requestsTitle": "Requests", "requestsProject": "By project", "requestsAll": "All projects",
           "oldHint": "Items waiting a long time. Reminders move here three days after their deadline, or after two weeks without a date. Clear or restore them to the active list.",
           "requestsYou": "You", "requestsTeam": "Team is working", "requestsDone": "Done",
           "requestDone": "Done", "requestDrop": "Not relevant", "requestPush": "Push forward", "requestTouch": "Still relevant", "requestConfirm": "Confirm?", "requestClosed": "Already closed", "requestSaved": "Action saved", "requestsOlder": "Older", "requestsReviewOld": "Review old items",
           "requestsSteps": "Steps", "requestsEmpty": "No open requests right now",
           "requestsNeedsYou": "Needs you", "requestsShowAll": "Show all {n}",
           "requestsMore": "Show {n} more", "requestsOpenCount": "{n} open",
           "requestsOwnerCount": "{n} need you", "requestsBackAll": "Back to all requests",
           "requestsOwnerEmpty": "Nothing needs you right now",
           "restoreRem": "Restore to active list", "remRestored": "Reminder restored to active list"},
}
for _lang, _pairs in _REQUEST_STRINGS.items():
    STRINGS[_lang].update(_pairs)

STRINGS["he"].update({"topicsTitle": "נושאים", "topicsEmpty": "נושאים מהיומן יופיעו כאן אוטומטית",
    "topic_owner": "מחכה לך", "topic_stuck": "תקוע", "topic_working": "בעבודה",
    "topic_decided": "הוחלט", "topic_done": "הושלם", "topicsDone": "נושאים שהושלמו",
    "topicDecision": "ההחלטה האחרונה", "topicNext": "הצעד הבא", "topicLatest": "העדכון האחרון", "topicNoDecision": "טרם נרשמה החלטה",
    "topicNoNext": "טרם נרשם צעד הבא", "topicTimeline": "היסטוריית הנושא", "topicNoNotes": "טרם נרשמו עדכונים",
    "topicClose": "סגירה", "topicPRs": "בקשות מיזוג", "topicPRUnknown": "מצב לא ידוע",
    "topicPR_open": "פתוחה", "topicPR_draft": "טיוטה", "topicPR_merged": "מוזגה",
    "topicStage_planning": "תכנון", "topicStage_development": "פיתוח", "topicStage_review": "סקירה",
    "topicStage_integration": "מיזוג", "topicStage_active": "פעיל"})
STRINGS["en"].update({"topicsTitle": "Topics", "topicsEmpty": "Ledger topics appear here automatically",
    "topic_owner": "Waiting for you", "topic_stuck": "Stuck", "topic_working": "In progress",
    "topic_decided": "Decided", "topic_done": "Done", "topicsDone": "Completed topics",
    "topicDecision": "Last decision", "topicNext": "Next step", "topicLatest": "Latest update", "topicNoDecision": "No decision recorded yet",
    "topicNoNext": "No next step recorded yet", "topicTimeline": "Topic timeline", "topicNoNotes": "No updates recorded yet",
    "topicClose": "Close", "topicPRs": "Pull requests", "topicPRUnknown": "State unknown",
    "topicPR_open": "Open", "topicPR_draft": "Draft", "topicPR_merged": "Merged",
    "topicStage_planning": "Planning", "topicStage_development": "Development", "topicStage_review": "Review",
    "topicStage_integration": "Integration", "topicStage_active": "Active"})
STRINGS["he"].update({"topicStatusHeading": "התקדמות"})
STRINGS["en"].update({"topicStatusHeading": "Progress"})

PAGE = r"""<!doctype html>
<html lang="__LANG__" dir="__DIR__">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="icon" href="/favicon-32.png" type="image/png" sizes="32x32">
<link rel="apple-touch-icon" href="/apple-touch-icon.png" sizes="180x180">
<link rel="manifest" href="/manifest.webmanifest">
<title>__TITLE__</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style nonce="__NONCE__">
:root{
--bg:#eef3fb;--card:#ffffff;--edge:rgba(20,50,110,.07);--field:var(--tg-theme-bg-color,#f6f8fc);
--text:var(--tg-theme-text-color,#0f1729);--hint:var(--tg-theme-hint-color,#6b7a90);
--accent:var(--tg-theme-button-color,#2f7cf6);--on-accent:var(--tg-theme-button-text-color,#ffffff);
--link:var(--tg-theme-link-color,var(--accent));--danger:var(--tg-theme-destructive-text-color,#e5484d);
--ok:#16a34a;--ok-soft:rgba(22,163,74,.12);--warn:#f59e0b;--warn-ink:#c27400;--warm:rgba(245,158,11,.12);
--idle:#a0a8b3;--off:#c3c8cf;--line:rgba(20,50,110,.09);--soft:rgba(100,116,139,.10);--blue-soft:rgba(47,124,246,.10);
--yes:var(--accent);--on-yes:var(--on-accent);--go:var(--blue-soft);--on-go:var(--link);
--glow:rgba(47,124,246,.16);--glow2:rgba(245,158,11,.10);
--shadow:0 1px 2px rgba(15,35,75,.05),0 6px 18px rgba(15,35,75,.07);
--shadow-lg:0 2px 4px rgba(15,35,75,.05),0 16px 36px rgba(15,35,75,.12);
--hero-hot:linear-gradient(var(--hero-a,250deg),#fff1db 0%,#fff8ee 45%,#ffffff 100%);
--hero-calm:linear-gradient(var(--hero-a,250deg),#e3f6ea 0%,#f4fbf6 45%,#ffffff 100%);
--glass:none;--r:20px;--fx:1}
:root[dir="rtl"]{--fx:-1;--hero-a:110deg}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
--bg:#0a1120;--card:rgba(24,36,64,.74);--edge:rgba(130,170,255,.15);--field:rgba(255,255,255,.05);
--text:var(--tg-theme-text-color,#eaf0ff);--hint:var(--tg-theme-hint-color,#8b9bbb);
--ok:#34d399;--ok-soft:rgba(52,211,153,.14);--warn:#fbbf24;--warn-ink:#fbbf24;--warm:rgba(251,191,36,.13);
--off:#3e4a62;--line:rgba(150,180,255,.12);--soft:rgba(150,170,210,.11);--blue-soft:rgba(80,140,255,.16);
--yes:linear-gradient(180deg,#fcc23c,#f59e0b);--on-yes:#231600;--go:linear-gradient(180deg,#3ee08f,#16b56a);--on-go:#03210f;
--glow:rgba(56,120,255,.34);--glow2:rgba(32,200,140,.10);
--shadow:0 1px 0 rgba(255,255,255,.04) inset,0 10px 28px rgba(0,0,0,.34);
--shadow-lg:0 1px 0 rgba(255,255,255,.05) inset,0 18px 44px rgba(0,0,0,.45);
--hero-hot:linear-gradient(var(--hero-a,250deg),rgba(251,191,36,.20),rgba(24,36,64,.80) 70%);
--hero-calm:linear-gradient(var(--hero-a,250deg),rgba(52,211,153,.18),rgba(24,36,64,.80) 70%);
--glass:blur(14px)}}
:root[data-theme="dark"]{
--bg:#0a1120;--card:rgba(24,36,64,.74);--edge:rgba(130,170,255,.15);--field:rgba(255,255,255,.05);
--text:var(--tg-theme-text-color,#eaf0ff);--hint:var(--tg-theme-hint-color,#8b9bbb);
--ok:#34d399;--ok-soft:rgba(52,211,153,.14);--warn:#fbbf24;--warn-ink:#fbbf24;--warm:rgba(251,191,36,.13);
--off:#3e4a62;--line:rgba(150,180,255,.12);--soft:rgba(150,170,210,.11);--blue-soft:rgba(80,140,255,.16);
--yes:linear-gradient(180deg,#fcc23c,#f59e0b);--on-yes:#231600;--go:linear-gradient(180deg,#3ee08f,#16b56a);--on-go:#03210f;
--glow:rgba(56,120,255,.34);--glow2:rgba(32,200,140,.10);
--shadow:0 1px 0 rgba(255,255,255,.04) inset,0 10px 28px rgba(0,0,0,.34);
--shadow-lg:0 1px 0 rgba(255,255,255,.05) inset,0 18px 44px rgba(0,0,0,.45);
--hero-hot:linear-gradient(var(--hero-a,250deg),rgba(251,191,36,.20),rgba(24,36,64,.80) 70%);
--hero-calm:linear-gradient(var(--hero-a,250deg),rgba(52,211,153,.18),rgba(24,36,64,.80) 70%);
--glass:blur(14px)}
*{box-sizing:border-box;min-inline-size:0;-webkit-tap-highlight-color:transparent}
html{background:var(--bg)}
body{margin:0;min-block-size:100vh;color:var(--text);
background:radial-gradient(130% 380px at 50% -120px,var(--glow),transparent 70%),radial-gradient(90% 300px at 50% 105%,var(--glow2),transparent 70%),var(--bg);
font:16px/1.45 -apple-system,BlinkMacSystemFont,"SF Hebrew","Segoe UI","Noto Sans Hebrew",Arial,sans-serif;
padding-block:max(var(--tg-safe-area-inset-top,0px),var(--tg-content-safe-area-inset-top,0px)) calc(108px + max(env(safe-area-inset-bottom),var(--tg-safe-area-inset-bottom,0px),var(--tg-content-safe-area-inset-bottom,0px)));
padding-inline:max(16px,var(--hg-safe-x,0px));-webkit-font-smoothing:antialiased}
button{font:inherit;color:inherit;max-inline-size:100%;white-space:normal;overflow-wrap:anywhere}
.card,.row{min-inline-size:0;overflow-wrap:anywhere}
[hidden]{display:none!important}
.top{display:flex;align-items:center;gap:12px;padding-block:16px 4px}
.top .ttl{flex:1;min-width:0}
h1{font-size:28px;line-height:1.1;margin:0;font-weight:800;letter-spacing:-.015em}
.sub{color:var(--hint);font-size:14px;margin-block-start:3px;overflow-wrap:anywhere;white-space:pre-line}
.status{display:flex;align-items:center;gap:6px;font-size:12.5px;color:var(--hint);min-height:18px;margin-block-start:3px}
.status .sp{inline-size:12px;block-size:12px;display:inline-grid}
.status .sp svg.i{inline-size:12px;block-size:12px}
.icon-btn{flex:none;inline-size:44px;block-size:44px;border-radius:50%;border:1px solid var(--edge);background:var(--card);box-shadow:var(--shadow);
display:grid;place-items:center;cursor:pointer;color:var(--link);-webkit-backdrop-filter:var(--glass);backdrop-filter:var(--glass);transition:transform .15s}
.icon-btn:active{transform:scale(.92)}
svg.i{inline-size:20px;block-size:20px;stroke:currentColor;fill:none;stroke-width:2;stroke-linecap:round;stroke-linejoin:round;flex:none}
.spin svg.i,.sp.spin{animation:spin .9s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
#ptr{block-size:0;overflow:hidden;display:flex;align-items:flex-end;justify-content:center;gap:6px;color:var(--hint);font-size:13px;transition:block-size .2s ease}
#ptr.drag{transition:none}
#ptr.ready{color:var(--link)}
/* Bottom navigation follows the document direction and clears the home indicator. */
.tabs{position:fixed;inset-inline:0;inset-block-end:0;z-index:10;display:flex;align-items:stretch;
 padding-block:6px calc(6px + max(env(safe-area-inset-bottom),var(--tg-safe-area-inset-bottom,0px)));padding-inline:4px;
 background:var(--bg);border-block-start:1px solid var(--edge);box-shadow:var(--shadow-lg)}
.tab{flex:1;min-inline-size:0;min-block-size:56px;border:0;background:transparent;color:var(--hint);cursor:pointer;
 display:flex;flex-direction:column;align-items:center;justify-content:center;gap:2px;padding:0 2px;font-size:11px;font-weight:650;line-height:1.2}
.tab-icon{position:relative;display:grid;place-items:center;inline-size:48px;block-size:28px;border-radius:99px}
.tab.on{color:var(--accent)}.tab.on .tab-icon{background:var(--blue-soft)}
.tab-label{max-inline-size:100%;text-align:center;overflow-wrap:anywhere}
.tab:active{transform:scale(.96)}
.more-sheet{position:fixed;z-index:11;inset-inline:12px;inset-block-end:calc(76px + max(env(safe-area-inset-bottom),var(--tg-safe-area-inset-bottom,0px)));
 max-block-size:50vh;overflow:auto;padding:8px;border:1px solid var(--edge);border-radius:18px;background:var(--bg);box-shadow:var(--shadow-lg)}
.more-sheet .tab{inline-size:100%;min-block-size:48px;flex-direction:row;justify-content:flex-start;gap:8px;padding-inline:8px;font-size:15px;text-align:start}
.more-sheet .tab-label{text-align:start}
/* a cockpit app: its own page in a sandboxed frame, kept alive while other tabs show */
#apps{margin-block-end:12px}
.appframe{display:block;inline-size:100%;block-size:calc(var(--tg-viewport-stable-height,100vh) - 176px - max(env(safe-area-inset-bottom),var(--tg-safe-area-inset-bottom,0px)));min-block-size:360px;border:0;
border-radius:0;background:transparent}
#apps[hidden],.appframe[hidden],.apperr[hidden]{display:none}
/* focus mode: the app asked to fold the header and tabs, so its frame fills the page edge to edge (the top inset stays) */
body.focus{padding-block-end:0;padding-inline:0}
body.focus #apps{margin-block-end:0}
body.focus .appframe{block-size:calc(var(--tg-viewport-stable-height,100vh) - max(var(--tg-safe-area-inset-top,0px),var(--tg-content-safe-area-inset-top,0px)));min-block-size:0}
/* measures the bottom safe-area inset (the home-indicator strip) so an app can keep its main action above it */
#probe{position:fixed;inset-block-start:0;inset-inline-start:0;inline-size:0;block-size:0;visibility:hidden;pointer-events:none;
padding-block-end:max(env(safe-area-inset-bottom),var(--tg-safe-area-inset-bottom,0px))}
.tab .tb{position:absolute;inset-block-start:-3px;inset-inline-end:-3px;font-size:11px;font-weight:800;min-inline-size:18px;block-size:18px;line-height:18px;border-radius:99px;padding-inline:5px;background:var(--warn);color:#2a1a00}
.mac-status.ok{color:var(--ok)}.mac-status.warn{color:var(--warn-ink)}.mac-status.critical{color:var(--danger)}
.mac-row{display:flex;flex-wrap:wrap;overflow-wrap:anywhere;justify-content:space-between;gap:12px;margin-block-start:8px}.mac-value{font-variant-numeric:tabular-nums;text-align:end}.mac-detail{font-size:12px;opacity:.85}.mac-processes{margin-block-start:10px}
/* cards */
.card{position:relative;background:var(--card);border:1px solid var(--edge);border-radius:var(--r);padding:16px;margin-block-end:12px;box-shadow:var(--shadow);
-webkit-backdrop-filter:var(--glass);backdrop-filter:var(--glass)}
.v2-return{display:flex;align-items:center;justify-content:space-between;gap:14px;border-color:color-mix(in srgb,var(--accent) 32%,var(--edge));background:color-mix(in srgb,var(--card) 88%,var(--blue-soft))}
.v2-return .nm{flex:1;min-inline-size:0;font-size:17px;font-weight:800;line-height:1.35}
.v2-return .btn{flex:none}
@media(max-width:420px){.v2-return{align-items:stretch;flex-direction:column}.v2-return .btn{inline-size:100%}}
.enter{animation:rise .42s cubic-bezier(.2,.8,.2,1) both}
@keyframes rise{from{opacity:0;transform:translateY(10px) scale(.985)}to{opacity:1;transform:none}}
/* hero */
.hero{display:flex;align-items:center;gap:16px;padding:18px 18px;box-shadow:var(--shadow-lg);background:var(--hero-hot)}
.hero.hot{border-color:rgba(245,158,11,.35)}
.hero.calm{background:var(--hero-calm);border-color:rgba(22,163,74,.22)}
:root[data-theme="dark"] .hero.hot{box-shadow:0 0 0 1px rgba(251,191,36,.18),0 0 28px rgba(251,191,36,.16),var(--shadow-lg)}
.hero .htx{flex:1;min-width:0}
.hero .h-t{font-size:23px;font-weight:800;line-height:1.2;letter-spacing:-.01em}
.hero.hot .h-t b{color:var(--warn-ink)}
.hero .h-s{font-size:14px;color:var(--hint);margin-block-start:4px;line-height:1.4}
.ring{position:relative;flex:none;inline-size:84px;block-size:84px}
.ring svg{inline-size:100%;block-size:100%;display:block}
:root[dir="rtl"] .ring>svg{transform:scaleX(-1)}
.rg-t{fill:none;stroke:var(--warm);stroke-width:5.5}
.rg-a{fill:none;stroke:var(--warn);stroke-width:5.5;stroke-linecap:round;transition:stroke-dashoffset 1s cubic-bezier(.2,.8,.2,1)}
.calm .rg-t{stroke:var(--ok-soft)}.calm .rg-a{stroke:var(--ok)}
.ring .mid{position:absolute;inset:0;display:grid;place-items:center;font-size:30px;font-weight:800;font-variant-numeric:tabular-nums}
.calm .ring .mid{color:var(--ok)}.calm .ring .mid svg.i{inline-size:30px;block-size:30px;stroke-width:2.6}
.hot .ring{filter:drop-shadow(0 4px 10px rgba(245,158,11,.25))}
/* counters */
.tiles{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-block:0 6px}
.tile{padding:12px 12px;border-radius:18px;background:var(--card);border:1px solid var(--edge);box-shadow:var(--shadow);text-align:center;
-webkit-backdrop-filter:var(--glass);backdrop-filter:var(--glass)}
.tile b{display:block;font-size:28px;line-height:1.05;font-weight:800;font-variant-numeric:tabular-nums}
.tile span{font-size:13px;color:var(--hint)}
.tile.hot{background:var(--warm);border-color:rgba(245,158,11,.30)}.tile.hot b{color:var(--warn-ink)}
.tile.go{border-color:rgba(22,163,74,.22)}.tile.go b{color:var(--ok)}
:root[data-theme="dark"] .tile.go{background:var(--ok-soft)}
/* section headers */
.sec{display:flex;align-items:center;gap:8px;margin-block:24px 10px;padding-inline:2px}
.sec::before{content:"";flex:none;inline-size:4px;block-size:18px;border-radius:99px;background:var(--accent)}
.sec.hot::before{background:var(--warn)}.sec.go::before{background:var(--ok)}.sec.dim::before{background:var(--off)}
.sec h2{font-size:19px;letter-spacing:-.005em;color:var(--text);margin:0;font-weight:800}
.sec .n{font-size:12.5px;font-weight:700;color:var(--hint);background:var(--soft);border-radius:99px;padding:1px 9px;font-variant-numeric:tabular-nums}
.sec.hot .n{background:var(--warm);color:var(--warn-ink)}.sec.go .n{background:var(--ok-soft);color:var(--ok)}
.sec .link-btn.end{margin-inline-start:auto}
/* decisions */
.ask{border-color:rgba(245,158,11,.38);box-shadow:0 0 0 1px rgba(245,158,11,.10),var(--shadow-lg)}
:root[data-theme="dark"] .ask{box-shadow:0 0 0 1px rgba(251,191,36,.22),0 0 26px rgba(251,191,36,.14),var(--shadow-lg)}
.ask::before{content:"";position:absolute;inset-block:16px;inset-inline-start:-1px;inline-size:4px;border-radius:99px;background:var(--warn)}
.ask-top{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--hint);margin-block-end:8px}
.ask-top .where{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:700;color:var(--warn-ink)}
.q{font-size:18px;font-weight:750;line-height:1.38;overflow-wrap:anywhere;white-space:pre-line}
.pq-request{margin-block-start:6px;font-size:13px;line-height:1.4;white-space:pre-wrap;overflow-wrap:anywhere;text-align:start;unicode-bidi:isolate}
.detail{margin-block-start:6px;font-size:15px;line-height:1.5;color:var(--text);opacity:.78;white-space:pre-line;overflow-wrap:anywhere}
.tr{display:inline-flex;align-items:center;gap:6px;margin-block-start:8px;font-size:12.5px;color:var(--hint)}
.dotpulse{inline-size:6px;block-size:6px;border-radius:50%;background:var(--accent);animation:blink 1.1s ease-in-out infinite}
@keyframes blink{50%{opacity:.25}}
.src-t{border:0;background:var(--soft);color:var(--hint);border-radius:99px;font-size:12px;padding:3px 10px;margin-block-start:8px;cursor:pointer}
.src-t.on{color:var(--link)}
.orig{margin-block-start:8px;padding:10px 12px;border-radius:12px;background:var(--soft);font-size:13.5px;line-height:1.45;color:var(--hint);
white-space:pre-line;overflow-wrap:anywhere;text-align:start;unicode-bidi:plaintext}
.note-t{display:inline-flex;align-items:center;gap:6px;border:0;background:transparent;color:var(--link);font-size:14px;font-weight:600;padding:10px 0 0;cursor:pointer}
.note-t svg.i{inline-size:16px;block-size:16px}
textarea{display:block;inline-size:100%;margin-block-start:10px;min-block-size:46px;resize:vertical;font:inherit;font-size:16px;line-height:1.4;padding:11px 14px;
border-radius:14px;border:1px solid var(--line);background:var(--field);color:var(--text);outline:none;transition:border-color .2s,box-shadow .2s}
textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--blue-soft)}
textarea::placeholder{color:var(--hint)}
.btns{display:flex;flex-wrap:wrap;gap:10px;margin-block-start:14px}
.btn{flex:1;min-block-size:52px;border-radius:15px;border:1.5px solid var(--line);font-weight:700;font-size:16.5px;cursor:pointer;
background:var(--field);color:var(--text);overflow-wrap:anywhere;display:flex;align-items:center;justify-content:center;gap:6px;padding-inline:12px;
transition:transform .12s ease,opacity .2s,box-shadow .2s}
.btn:active{transform:scale(.96)}
.btn.yes{background:var(--yes);border-color:transparent;color:var(--on-yes);flex:1.35;box-shadow:0 6px 16px rgba(47,124,246,.28)}
:root[data-theme="dark"] .btn.yes{box-shadow:0 6px 18px rgba(245,158,11,.28)}
.rdone{flex:none;inline-size:40px;block-size:40px;border-radius:50%;border:1.5px solid var(--line);background:transparent;color:var(--ok,#2fbf71);
 display:grid;place-items:center;cursor:pointer;margin-inline-start:8px}.rdone:disabled{opacity:.4}.rdone svg{inline-size:20px;block-size:20px}
.btn.arm{border-color:var(--warn);color:var(--warn-ink)}
.hint.pad{padding:12px 16px 4px;color:var(--hint);font-size:14px;line-height:1.45}
.btn.sm{min-block-size:44px;font-size:15px;flex:none;padding-inline:20px}
.btn.go{background:var(--go);color:var(--on-go);border-color:transparent}
.btn.pri{background:linear-gradient(180deg,rgba(255,255,255,.16),rgba(255,255,255,0)),var(--accent);color:var(--on-accent);border-color:transparent}
.btn:disabled{opacity:.5;cursor:default;transform:none}
.done-c{display:flex;gap:12px;align-items:center}
.done-c .ok{flex:none;inline-size:42px;block-size:42px;border-radius:50%;display:grid;place-items:center;background:var(--ok-soft);color:var(--ok);animation:pop .4s cubic-bezier(.2,1.4,.4,1) both}
.done-c .ok.no{background:var(--soft);color:var(--hint)}
@keyframes pop{from{transform:scale(.4);opacity:0}}
.done-c b{display:block;font-size:15px}
.done-c small{display:block;font-size:13px;color:var(--hint);overflow-wrap:anywhere}
.err{margin-block-start:10px;font-size:13.5px;color:var(--danger)}
.safe{font-size:12px;color:var(--hint);margin:2px 6px 0;line-height:1.5}
/* the decision stack */
.stack{position:relative;touch-action:pan-y}
.stack.multi{padding-block-start:22px}
.stack .ghost{position:absolute;inset-block-start:11px;inset-inline:10px;block-size:90px;border-radius:var(--r);background:var(--card);
border:1px solid rgba(245,158,11,.35);box-shadow:var(--shadow-lg);-webkit-backdrop-filter:var(--glass);backdrop-filter:var(--glass)}
.stack .ghost.g2{inset-block-start:0;inset-inline:22px;opacity:.7}
:root[data-theme="dark"] .stack .ghost{background:#16213b}
.stack>.card{z-index:1}
.swap{animation:swapIn .34s cubic-bezier(.2,.8,.2,1) both}
.swap.back{animation-name:swapBack}
@keyframes swapIn{from{opacity:0;transform:translateX(calc(var(--fx)*40px)) scale(.97)}}
@keyframes swapBack{from{opacity:0;transform:translateX(calc(var(--fx)*-40px)) scale(.97)}}
.dots{display:flex;justify-content:center;gap:4px;margin-block:2px 8px}
.dot{inline-size:22px;block-size:22px;border:0;padding:0;background:transparent;cursor:pointer;display:grid;place-items:center}
.dot::after{content:"";inline-size:7px;block-size:7px;border-radius:99px;background:var(--off);transition:inline-size .25s,background .25s}
.dot.on::after{inline-size:20px;background:var(--warn)}
/* empty states and rows */
.empty{display:flex;gap:14px;align-items:center}
.empty .ic{flex:none;inline-size:46px;block-size:46px;border-radius:15px;display:grid;place-items:center;background:var(--soft);color:var(--hint)}
.empty .ic.good{background:var(--ok-soft);color:var(--ok)}
.empty b{display:block;font-size:15.5px}
.empty small{display:block;font-size:13.5px;color:var(--hint);margin-block-start:2px}
.group{padding:0 16px}
.row{display:flex;gap:12px;align-items:center;padding-block:13px;border-block-start:1px solid var(--line)}
.row:first-child{border-block-start:0}
.row .body{flex:1;min-width:0}
.row .nm{font-weight:700;font-size:15.5px;overflow-wrap:anywhere;white-space:pre-line}
.row .mt{font-size:12.5px;color:var(--hint);margin-block-start:4px;display:flex;flex-wrap:wrap;align-items:center;gap:4px 10px}
.row.ticked .nm{text-decoration:line-through;color:var(--hint)}
a.row{color:var(--text);text-decoration:none}
.sdot{flex:none;inline-size:10px;block-size:10px;border-radius:50%;background:var(--off)}
.sdot.working{background:var(--ok);box-shadow:0 0 0 4px var(--ok-soft);animation:breathe 2s ease-in-out infinite}
.sdot.idle{background:var(--warn)}
@keyframes breathe{50%{box-shadow:0 0 0 7px rgba(31,163,92,0)}}
.pill{flex:none;font-size:12px;font-weight:700;padding:3px 10px;border-radius:99px;background:var(--soft);color:var(--hint)}
.pill.working,.pill.on{background:var(--ok-soft);color:var(--ok)}
.pill.idle{background:var(--warm);color:var(--warn-ink)}
.chip{display:inline-flex;align-items:center;gap:4px}
.chip.hot{color:var(--warn-ink);font-weight:600}
.chip.mdl{background:var(--blue-soft);color:var(--link);border-radius:99px;padding:1px 8px;font-weight:650}
.lead{flex:none;inline-size:38px;block-size:38px;border-radius:12px;display:grid;place-items:center;background:var(--soft);color:var(--hint)}
.lead.warm{background:var(--warm);color:var(--warn-ink)}
.lead.blue{background:var(--blue-soft);color:var(--link)}
.lead.ok{background:var(--ok-soft);color:var(--ok)}
.kv{display:flex;flex-wrap:wrap;justify-content:space-between;gap:12px;padding-block:9px;border-block-start:1px solid var(--line);font-size:14.5px}
.kv:first-of-type{border-block-start:0}
.kv span:first-child{color:var(--hint)}
.kv span:last-child{text-align:end;overflow-wrap:anywhere}
.s-head{display:flex;align-items:center;gap:12px;margin-block-end:6px}
.s-head .nm{flex:1;min-width:0;font-weight:750;font-size:16.5px;overflow-wrap:anywhere;white-space:pre-line}
.bar{block-size:8px;border-radius:99px;background:var(--soft);overflow:hidden;margin-block-start:7px}
.bar i{display:block;block-size:100%;border-radius:99px;background:var(--ok);transition:inline-size .8s cubic-bezier(.2,.8,.2,1)}
.bar i.warn{background:var(--warn)}.bar i.bad{background:var(--danger)}
.acct{padding-block:14px;border-block-start:1px solid var(--line)}.acct:first-child{border-block-start:0}
.acct .ah{display:flex;align-items:center;gap:10px}
.acct .nm{flex:1;min-width:0;font-weight:750}
.acct .pc{font-size:20px;font-weight:800;font-variant-numeric:tabular-nums}
.meter{margin-block-start:8px}
.meter .lb{display:flex;justify-content:space-between;align-items:baseline;gap:10px;font-size:13px;color:var(--hint)}
.meter .lb b{color:var(--text);font-variant-numeric:tabular-nums}
.stepper{display:flex;align-items:center;gap:10px;margin-inline-start:auto}.stepper .bn{min-inline-size:28px;text-align:center;font-size:18px;font-variant-numeric:tabular-nums}.stepper .bn.unset{font-size:12px;font-weight:500;color:var(--hint)}
.stepb{inline-size:36px;block-size:36px;border-radius:50%;border:1px solid var(--line);background:var(--bg2,transparent);color:inherit;display:grid;place-items:center;cursor:pointer}.stepb:disabled{opacity:.35}.stepb .i{inline-size:18px;block-size:18px}
.bankh{padding:10px 14px 14px}
.bankx{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;padding-block:0 12px;padding-inline-start:50px;font-size:13px;color:var(--hint)}
.bankx .bkd{font:inherit;color:var(--text);background:var(--field);border:1px solid var(--line);border-radius:10px;padding:5px 9px;min-block-size:34px}
.bankx .bkd:disabled{opacity:.5}.bankx .link-btn{font-size:13px;padding:4px 0}
.bankx .dl{font-weight:650;color:var(--text)}.bankx .dl.soon{color:var(--danger)}
.meter.bk{margin-block-start:10px;padding-block-start:10px;border-block-start:1px dashed var(--line)}
.seg{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-block-start:12px}
.opt{min-block-size:46px;border-radius:13px;border:1.5px solid var(--line);background:var(--field);font-weight:650;cursor:pointer;
display:flex;align-items:center;justify-content:center;gap:6px;transition:border-color .2s,background .2s,transform .12s}
.opt:active{transform:scale(.96)}
.opt.on{border-color:var(--accent);background:var(--blue-soft);color:var(--link)}
.opt:disabled{opacity:.6}
.digest{background:linear-gradient(135deg,rgba(47,124,246,.12),rgba(31,163,92,.08)),var(--card)}
.digest .hd{display:flex;align-items:center;gap:8px;font-weight:700;font-size:14.5px}
.digest .hd .when{flex:1;font-weight:400;color:var(--hint);font-size:12.5px}
.digest .chips{display:flex;flex-wrap:wrap;gap:8px;margin-block-start:10px}
.digest .c{background:var(--card);border:1px solid var(--edge);border-radius:10px;padding:6px 10px;font-size:13.5px}
.digest .c b{font-variant-numeric:tabular-nums;margin-inline-end:4px}
.link-btn{border:0;background:transparent;color:var(--link);font-weight:650;font-size:14px;cursor:pointer;padding:6px 0}
details>summary{list-style:none;cursor:pointer}
details>summary::-webkit-details-marker{display:none}

.pod .podnow{font-size:15.5px;font-weight:700;line-height:1.5;overflow-wrap:anywhere}
.pod .podsub{font-size:12.5px;color:var(--hint);margin-block-start:2px}
.pod .btns{flex-wrap:wrap}.pod .btns .btn{min-inline-size:96px}
.pod .podrow{display:flex;gap:12px;align-items:center;padding-block:10px;border-block-start:1px solid var(--line)}
.pod .podrow>span{flex:1;min-inline-size:0;overflow-wrap:anywhere;font-size:14.5px;line-height:1.45}
.pod .podeps{display:grid;gap:8px;margin-block-start:12px}
.pod .podeps .btn{justify-content:flex-start;text-align:start;min-block-size:48px;padding-block:8px;font-size:15px;font-weight:600}
.pod .podlink{display:flex;gap:8px;margin-block-start:12px;flex-wrap:wrap}
.pod .podlink input{flex:1 1 180px;min-inline-size:0;min-block-size:48px;border:1.5px solid var(--line);border-radius:14px;background:var(--field);color:var(--text);font:inherit;font-size:15px;padding-inline:12px;box-sizing:border-box}
.music-now{min-inline-size:0;margin-block:12px;padding-block:10px;border-block:1px solid var(--line)}
.music-now .nm,.music-now .safe{min-inline-size:0;overflow-wrap:anywhere}.music-now>.btn{margin-block-start:10px}
.program-card{display:grid;gap:10px}.program-block{display:grid;grid-template-columns:minmax(0,1fr) 76px;gap:8px;align-items:center;padding-block:8px;border-block-end:1px solid var(--line)}
.program-block select,.program-block input,.program-request input{min-inline-size:0;min-block-size:44px;border:1.5px solid var(--line);border-radius:12px;background:var(--field);color:var(--text);font:inherit;padding-inline:10px;box-sizing:border-box}.program-request{display:flex;flex-wrap:wrap;gap:8px}.program-request input{flex:1 1 180px}
/* the old group: calm and secondary, one line, a wide tap target for a thumb */
.oldg{margin-block:6px 14px}
.oldg>.compact-toggle{min-block-size:48px;color:var(--hint);background:var(--soft)}
.oldg>.compact-toggle:active{background:var(--blue-soft)}
.oldbody{padding-block-start:10px}.oldbody>.safe{margin-block-end:10px}
.srow{border-block-start:1px solid var(--line)}.srow:first-child{border-block-start:0}
.srow>summary.row{border-block-start:0}
.srow>summary.row:active,.row.tap:active{opacity:.7}
.srow[open]>summary .act{white-space:normal}
.more{padding-block:0 14px;padding-inline-start:58px}
.act{display:flex;gap:6px;align-items:center;font-size:13.5px;color:var(--text);opacity:.85;margin-block-start:3px;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.act svg.i{inline-size:14px;block-size:14px;color:var(--ok)}
.act.sum svg.i{color:var(--link)}
.act span{overflow:hidden;text-overflow:ellipsis}
.reply{overflow-wrap:anywhere;margin-block-start:10px;padding:10px 12px;border-radius:12px;background:var(--soft);font-size:14.5px;line-height:1.5;white-space:pre-line}
.scr{margin-block-start:8px;padding:10px;border-radius:12px;background:#0f1419;color:#e6e6e6;font:11px/1.45 ui-monospace,Menlo,monospace;white-space:pre;overflow:auto;max-height:60vh;direction:ltr;text-align:start}
.reply .lbl{font-size:12px;color:var(--hint);font-weight:600;margin-block-end:2px}
.row.tap{cursor:pointer}.row.tap>svg.i{color:var(--off);inline-size:16px;block-size:16px}
:root[dir="rtl"] .row.tap>svg.i{transform:scaleX(-1)}
.row.col{align-items:stretch}
.idle-more{padding-block:10px;color:var(--hint);font-size:12.5px}
.row .when{flex:none;font-size:12px;color:var(--hint);white-space:nowrap}
.pad{padding-block:10px 0}
.link-btn.go{display:inline-flex;align-items:center;gap:4px;font-size:13px;padding:2px 0}
.link-btn.go svg.i{inline-size:15px;block-size:15px}
.link-btn.end{margin-inline-start:auto;font-size:13px}
.link-btn.all{display:flex;align-items:center;gap:4px;margin-inline:auto;padding:10px 4px 2px}
:root[dir="rtl"] .link-btn.all svg.i{transform:scaleX(-1)}
.question-highlight{outline:3px solid var(--accent);outline-offset:3px;scroll-margin-block:24px}
.badge{flex:none;font-size:11px;font-weight:800;padding:1px 8px;border-radius:99px;background:var(--accent);color:var(--on-accent)}
.seg.three{grid-template-columns:repeat(3,minmax(0,1fr))}
.sw{flex:none;position:relative;inline-size:52px;block-size:32px;border-radius:99px;border:0;background:var(--off);cursor:pointer;transition:background .2s}
.sw i{position:absolute;inset-block-start:3px;inset-inline-start:3px;inline-size:26px;block-size:26px;border-radius:50%;background:#fff;
box-shadow:0 1px 3px rgba(0,0,0,.25);transition:inset-inline-start .22s cubic-bezier(.2,.8,.2,1)}
.sw.on{background:var(--ok)}.sw.on i{inset-inline-start:23px}
.yt .ch{font-weight:600;margin-block:2px 8px}.yt .ytp,.yt .ytc{margin-block:10px;padding-block:8px;border-block-start:1px solid var(--line)}
.yt .row.chk{gap:10px;align-items:center;padding-inline:0}.yt .row.chk input{inline-size:22px;block-size:22px;flex:none}
.yt .mt.warn{color:var(--warn-ink)}.yt .group{margin-block:8px}
.my-music-player{inline-size:100%;min-inline-size:200px;min-block-size:200px;margin-block:12px;overflow:hidden}.my-music iframe{inline-size:100%;min-block-size:200px;border:0}
/* robots */
.bot{position:relative;flex:none;inline-size:44px;block-size:44px;border-radius:50%}
.bot svg{inline-size:100%;block-size:100%;display:block;border-radius:50%;box-shadow:0 2px 8px rgba(15,35,75,.12)}
.bot.working::after{content:"";position:absolute;inset:-3px;border-radius:50%;border:2px solid var(--ok);animation:ping 2s ease-out infinite}
.bot.working::before{content:"";position:absolute;inset-block-end:0;inset-inline-end:0;inline-size:12px;block-size:12px;border-radius:50%;background:var(--ok);
border:2px solid var(--card);z-index:1}
.bot.idle::before{content:"";position:absolute;inset-block-end:0;inset-inline-end:0;inline-size:12px;block-size:12px;border-radius:50%;background:var(--warn);border:2px solid var(--card);z-index:1}
.bot.ended svg{filter:grayscale(.85);opacity:.65}
.bot.sm{inline-size:38px;block-size:38px}
@keyframes ping{0%{transform:scale(.92);opacity:.9}80%,100%{transform:scale(1.22);opacity:0}}
.bot.working .eye{animation:look 4s ease-in-out infinite}
@keyframes look{0%,40%,100%{opacity:1}45%{opacity:.15}50%{opacity:1}}
:root[data-theme="dark"] .bot svg{box-shadow:0 0 16px rgba(80,140,255,.25)}
.cm{display:inline-flex;align-items:center;gap:6px}
.cm .bar{inline-size:46px;block-size:5px;margin:0}
/* agent cards */
.ac{padding:16px}
.ac.stale,.ac.auto-closed{background:var(--soft);border-color:var(--edge)}
.ac.stale .pbar i,.ac.auto-closed .pbar i{background:var(--hint);box-shadow:none}
.ac-status{color:var(--hint);font-weight:700;margin-block-start:8px}
.ac.asks{border-color:rgba(245,158,11,.30)}
.ac-h{display:flex;gap:12px;align-items:flex-start}
.ac-b{flex:1;min-width:0}
.ac-k{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--hint);flex-wrap:wrap}
.ac-k .kd{display:inline-flex;align-items:center;gap:4px;font-weight:750;color:var(--link)}
.ac-k .kd svg.i{inline-size:14px;block-size:14px}
.asks .ac-k .kd{color:var(--warn-ink)}
.ac-k>span+span::before{content:"·";margin-inline-end:6px;color:var(--hint);font-weight:400}
.sid{font:600 11px ui-monospace,SFMono-Regular,Menlo,monospace;opacity:.6;flex:none;letter-spacing:.02em}
.ac-k .tp{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-inline-size:60%}
.ac-t{font-size:16.5px;font-weight:750;line-height:1.35;margin-block-start:3px;overflow-wrap:anywhere;white-space:pre-line}
.ac-d{font-size:14px;color:var(--hint);margin-block-start:3px;line-height:1.45;overflow-wrap:anywhere;white-space:pre-line}
.card-copy-toggle{display:block;inline-size:100%;min-inline-size:0;min-block-size:44px;border:0;padding-block:0;padding-inline:0;background:transparent;text-align:start;font:inherit;cursor:pointer}
.card-copy-preview{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:3;line-clamp:3;overflow:hidden;text-overflow:ellipsis;overflow-wrap:break-word;word-break:normal}
.card-copy-toggle.expanded .card-copy-preview{display:block;-webkit-line-clamp:unset;line-clamp:unset;overflow:visible}
.card-copy-toggle:focus-visible{outline:2px solid var(--link);outline-offset:2px;border-radius:4px}
.ac.busy{opacity:.85}
/* one session's cards under one head: its robot, topic and tag once, a way into its topic, then its cards */
.sgrp{margin-block-end:14px}
.sg-h{display:flex;align-items:center;gap:10px;padding-block:2px 8px;padding-inline:2px}
.sg-n{flex:1;min-inline-size:0;display:flex;align-items:baseline;gap:8px}
.sg-t{font-size:15px;font-weight:750;min-inline-size:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sg-go{display:inline-flex;align-items:center;gap:6px;min-block-size:38px;padding-inline:14px;font-size:14px}
.sg-go svg.i{inline-size:16px;block-size:16px}
.sgrp>.card{margin-block-end:8px}
.sgrp>.card:last-child{margin-block-end:0}
.ac .done-c{margin-block-start:14px;padding:12px;border-radius:14px;background:var(--soft)}
.pg{display:flex;align-items:center;gap:12px;margin-block-start:14px}
.pbar{flex:1;block-size:10px;border-radius:99px;background:var(--soft);overflow:hidden}
.pbar i{display:block;block-size:100%;border-radius:99px;background:linear-gradient(90deg,#5aa2ff,var(--accent));transition:inline-size 1s cubic-bezier(.2,.8,.2,1)}
:root[dir="rtl"] .pbar i{background:linear-gradient(270deg,#5aa2ff,var(--accent))}
:root[data-theme="dark"] .pbar i{background:linear-gradient(90deg,#1fb978,#4ef0a0);box-shadow:0 0 12px rgba(78,240,160,.45)}
:root[data-theme="dark"][dir="rtl"] .pbar i{background:linear-gradient(270deg,#1fb978,#4ef0a0)}
.pct{flex:none;font-size:15px;font-weight:800;padding:3px 11px;border-radius:99px;background:var(--blue-soft);color:var(--link);font-variant-numeric:tabular-nums}
:root[data-theme="dark"] .pct{background:var(--ok-soft);color:var(--ok)}
.track{list-style:none;margin:14px 0 0;padding:0;display:grid}
.track li{position:relative;display:flex;flex-direction:column;align-items:center;gap:6px;text-align:center;min-width:0}
.track li::before{content:"";position:absolute;inset-block-start:10px;inset-inline-end:50%;inline-size:100%;block-size:3px;background:var(--line);border-radius:99px}
.track li:first-child::before{display:none}
.track li.done::before,.track li.cur::before{background:var(--accent)}
:root[data-theme="dark"] .track li.done::before,:root[data-theme="dark"] .track li.cur::before{background:var(--ok)}
.track .sd{position:relative;z-index:1;inline-size:23px;block-size:23px;border-radius:50%;border:2.5px solid var(--off);background:var(--card);display:grid;place-items:center}
.track .done .sd{background:var(--accent);border-color:var(--accent);color:#fff}
.track .cur .sd{border-color:var(--accent);box-shadow:0 0 0 4px var(--blue-soft)}
.track .cur .sd::after{content:"";inline-size:9px;block-size:9px;border-radius:50%;background:var(--accent);animation:blink 1.4s ease-in-out infinite}
:root[data-theme="dark"] .track .done .sd{background:var(--ok);border-color:var(--ok);color:#03210f}
:root[data-theme="dark"] .track .cur .sd{border-color:var(--ok);box-shadow:0 0 0 4px var(--ok-soft)}
:root[data-theme="dark"] .track .cur .sd::after{background:var(--ok)}
.track .sd svg.i{inline-size:13px;block-size:13px;stroke-width:3}
.track .sl{font-size:12px;color:var(--hint);line-height:1.3;overflow-wrap:anywhere;padding-inline:2px}
.track .cur .sl{color:var(--text);font-weight:700}
.opts{display:grid;gap:8px;margin-block-start:14px}
.opts.c2{grid-template-columns:repeat(2,minmax(0,1fr))}.opts.c3{grid-template-columns:repeat(3,minmax(0,1fr))}
.copt{min-block-size:46px;border-radius:13px;border:1.5px solid var(--line);background:var(--field);font-weight:700;font-size:15px;cursor:pointer;padding:8px 10px;
overflow-wrap:anywhere;transition:transform .12s,background .2s,border-color .2s,color .2s}
.copt:active{transform:scale(.96)}
.copt.on{background:linear-gradient(180deg,rgba(255,255,255,.16),rgba(255,255,255,0)),var(--accent);color:var(--on-accent);border-color:transparent}
:root[data-theme="dark"] .copt.on{background:var(--go);color:var(--on-go)}
.copt:disabled{cursor:default}
.copt:disabled:not(.on){opacity:.5}
.choice-tiles{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-block-start:14px}
.choice-tile{min-inline-size:0;display:flex;flex-direction:column;gap:8px;padding:8px;border:1px solid var(--line);border-radius:16px;background:var(--field)}
.choice-preview{display:block;inline-size:100%;padding:0;border:0;border-radius:10px;overflow:hidden;background:var(--card);cursor:zoom-in;min-block-size:96px}
.choice-preview img{display:block;inline-size:100%;block-size:auto;max-block-size:70vh;object-fit:contain}.choice-tiles.tall{grid-template-columns:minmax(0,1fr)}.choice-tiles.tall .choice-preview img{max-block-size:62vh}.choice-preview{position:relative}.choice-zoom{position:absolute;inset-block-end:8px;inset-inline-start:8px;padding:4px 10px;border-radius:999px;background:rgba(0,0,0,.6);color:#fff;font-size:13px;font-weight:600;pointer-events:none}
.choice-preview:disabled{cursor:wait}.choice-label{overflow-wrap:anywhere;font-weight:700}
.choice-tile .copt{inline-size:100%;margin-block-start:auto;min-block-size:48px}
.choice-image-error{font-size:13px;color:var(--hint);overflow-wrap:anywhere}
@media(max-width:359px){.choice-tiles{grid-template-columns:minmax(0,1fr)}}
.card-image-viewer{position:fixed;inset:0;inline-size:100%;max-inline-size:none;block-size:100%;max-block-size:none;margin:0;padding:0;border:0;background:#111;color:#fff;overflow:hidden}
.card-image-viewer[open]{display:flex;flex-direction:column}.card-image-viewer::backdrop{background:#111}
.card-image-bar{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:calc(12px + env(safe-area-inset-top)) 16px 12px;flex-shrink:0}
.card-image-label{min-inline-size:0;overflow-wrap:anywhere}.card-image-close{min-inline-size:48px;min-block-size:48px;border:1px solid #aaa;border-radius:12px;background:#222;color:#fff;padding:10px;font:inherit;cursor:pointer}
.card-image-stage{display:flex;align-items:center;justify-content:center;flex:1;min-block-size:0;overflow:hidden;touch-action:none;direction:ltr}
.card-image-stage img{display:block;inline-size:auto;block-size:auto;max-inline-size:100%;max-block-size:100%;object-fit:contain;user-select:none;-webkit-user-select:none;transform-origin:center}
.card-image-hint{text-align:center;font-size:13px;padding:12px 16px calc(12px + env(safe-area-inset-bottom));margin:0;color:#ddd}
.path{font:500 13px ui-monospace,SFMono-Regular,Menlo,monospace;overflow-wrap:anywhere;text-align:end;unicode-bidi:isolate}
.saybox{margin-block-start:12px}.saybox .askrow{margin-block-start:8px}
.wait{margin-block-end:8px;padding:10px 14px;border-radius:18px;border-end-end-radius:6px;background:var(--blue-soft);border:1px dashed var(--line)}
.wait-t{font-size:15px;line-height:1.45;white-space:pre-wrap;overflow-wrap:anywhere}
.wait-f{display:flex;align-items:center;gap:6px;margin-block-start:8px;flex-wrap:wrap}
.wait-m{display:inline-flex;align-items:center;gap:5px;font-size:12px;color:var(--hint);margin-inline-end:auto}
.wait-m svg.i{inline-size:13px;block-size:13px}
.wait-b{display:inline-flex;align-items:center;gap:5px;border:0;border-radius:999px;padding:7px 12px;font:inherit;font-size:13px;font-weight:600;
background:var(--field);color:var(--link);cursor:pointer;min-block-size:34px}
.wait-b svg.i{inline-size:14px;block-size:14px}.wait-b.del{color:var(--hint)}.wait-b:disabled{opacity:.5}
/* the composer: one rounded field with the send button inside it, growing with the text like a chat app's */
.askrow{display:flex;gap:6px;align-items:flex-end;margin-block-start:12px;padding:5px;padding-inline-start:16px;border-radius:25px;
border:1px solid var(--line);background:var(--field);transition:border-color .2s,box-shadow .2s;cursor:text}
.askrow:focus-within{border-color:var(--accent);box-shadow:0 0 0 3px var(--blue-soft)}
.askrow textarea{margin:0;flex:1;min-inline-size:0;min-block-size:38px;max-block-size:168px;resize:none;border:0;border-radius:0;background:transparent;
padding:8px 0;box-shadow:none;overflow-y:auto;overscroll-behavior:contain}
.askrow textarea:focus{box-shadow:none}
.askrow .cnt{flex:none;align-self:center;font-size:12px;font-variant-numeric:tabular-nums;color:var(--hint)}
.askrow .cnt.hot{color:var(--warn,#c2410c);font-weight:600}
.send{flex:none;inline-size:44px;block-size:44px;border-radius:50%;border:0;display:grid;place-items:center;cursor:pointer;
background:linear-gradient(180deg,rgba(255,255,255,.16),rgba(255,255,255,0)),var(--accent);color:var(--on-accent);
transition:transform .18s cubic-bezier(.3,1.4,.5,1),opacity .2s,background .2s}
.send svg.i{inline-size:18px;block-size:18px}
.send:active{transform:scale(.9)}
.send:disabled{background:transparent;color:var(--hint);opacity:.7;cursor:default}
.askrow.busy{opacity:.6}
@media (prefers-reduced-motion:reduce){.send{transition:none}}
:root[dir="rtl"] .send svg.i{transform:scaleX(-1)}
.lk{display:flex;align-items:center;gap:12px;margin-block-start:14px;padding:10px;border-radius:15px;background:var(--soft)}
.lk-i{flex:none;inline-size:40px;block-size:40px;border-radius:12px;display:grid;place-items:center;background:var(--blue-soft);color:var(--link)}
.lk-t{flex:1;min-width:0}
.lk-t b{display:block;font-size:14.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lk-t small{display:block;font-size:12px;color:var(--hint);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:start}
.figs{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(0,1fr);margin-block-start:14px}
.fig{text-align:center;padding-inline:6px;min-width:0}
.fig+.fig{border-inline-start:1px solid var(--line)}
.fig b{display:block;font-size:22px;font-weight:800;line-height:1.15;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.fig span{display:block;font-size:12.5px;color:var(--hint);margin-block-start:2px;overflow-wrap:anywhere}
.foot{text-align:center;font-size:12px;color:var(--hint);margin-block-start:18px}
.sk{border-radius:14px;background:linear-gradient(90deg,var(--soft) 25%,var(--line) 37%,var(--soft) 63%);background-size:400% 100%;animation:shimmer 1.3s ease infinite}
@keyframes shimmer{0%{background-position:100% 50%}100%{background-position:0 50%}}
.sk.t{block-size:14px;inline-size:40%;margin-block:20px 10px}
.sk.c{block-size:120px;border-radius:var(--r);margin-block-end:12px}
.sk.c.s{block-size:64px}
#toast{position:fixed;inset-inline:16px;inset-block-end:calc(84px + max(env(safe-area-inset-bottom),var(--tg-safe-area-inset-bottom,0px)));display:flex;justify-content:center;pointer-events:none;z-index:12}
body.focus #toast{inset-block-end:calc(18px + max(env(safe-area-inset-bottom),var(--tg-safe-area-inset-bottom,0px)))}
#toast div{max-inline-size:min(100%,440px);overflow-wrap:anywhere;background:rgba(15,23,41,.94);color:#fff;border-radius:16px;padding:12px 16px;font-size:14.5px;font-weight:600;
box-shadow:0 10px 30px rgba(0,0,0,.25);display:flex;gap:8px;align-items:center;opacity:0;transform:translateY(10px);transition:opacity .22s,transform .22s;white-space:pre-line}
#toast div.show{opacity:1;transform:none}
#toast div.bad{background:var(--danger)}
@media (prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
.room .live{display:flex;align-items:center;gap:8px;font-weight:700;font-size:14px;color:var(--danger)}
.room .live.pz{color:var(--warn-ink)}
.room .live i{inline-size:9px;block-size:9px;border-radius:50%;background:currentColor;animation:blink 1.4s infinite}
.room .ttl{margin-block-start:6px;font-size:19px;font-weight:800;line-height:1.3}
.room .rnds{display:flex;flex-wrap:wrap;gap:8px;margin-block-start:12px}
.room .rnd{display:inline-flex;align-items:center;gap:4px;padding:4px 12px;border-radius:99px;border:1px solid var(--line);font-size:13.5px;color:var(--hint)}
.room .rnd.on{background:var(--warm);border-color:var(--warn);color:var(--warn-ink);font-weight:700}
.room .rnd.past{color:var(--ok)}
.room .rnd svg.i{inline-size:14px;block-size:14px}
.room .seats{display:grid;gap:7px;margin-block-start:14px}
.room .seat{display:flex;align-items:center;gap:9px;font-size:15px;color:var(--hint)}
.room .seat i{flex:none;inline-size:9px;block-size:9px;border-radius:50%;background:var(--off)}
.room .seat.on{color:var(--text);font-weight:750}
.room .seat.on i{background:var(--warn);box-shadow:0 0 0 4px var(--warm)}
.room .btn.wait{background:var(--danger);color:#fff;border-color:transparent;flex:1}
.room .btns.col{flex-direction:column}
.room .sub{margin-block-start:14px;font-size:13.5px;font-weight:700;color:var(--hint)}
.room .props{display:grid;gap:6px;margin-block-start:6px}
.room .props div{padding:8px 12px;border-radius:12px;background:var(--soft);font-size:15px}
.room .chips{display:flex;flex-wrap:wrap;gap:8px;margin-block-start:8px}
.room .chips .opt{min-block-size:40px;padding-inline:12px;font-size:14px;text-align:start}
.room .task{display:flex;align-items:center;gap:10px;padding-block:10px;border-block-start:1px solid var(--edge)}
.room .task .tx{flex:1;min-inline-size:0;font-size:15px;line-height:1.4}
.room .task .sent{font-size:13px;color:var(--ok);font-weight:650}
.room .pick{display:grid;gap:6px;margin-block-start:8px}
.room .q{font-size:15px;line-height:1.45;padding-block:4px}
.room .dec{font-size:16px;font-weight:700;line-height:1.4;margin-block-start:4px}
/* the Morning Brief home (brief.py): small cards, one sentence each, every fact with its age; stale facts grey */
.bf{padding:14px 16px}
.bf-h{display:flex;align-items:baseline;gap:8px;font-size:13px;color:var(--hint);font-weight:650}
.bf-h .bf-t{flex:1;min-inline-size:0}
.bf-h .bf-e{flex:none;font-weight:600}
.bf.bhd{background:var(--hero-calm),var(--card);padding-block:16px 18px}
.bf.bhd.hot{background:var(--hero-hot),var(--card)}
.bf .hl{font-size:22px;line-height:1.35;font-weight:800;letter-spacing:-.01em;margin-block-start:8px;overflow-wrap:anywhere}
.bf-none{font-size:14px;color:var(--hint);margin-block-start:8px}
.bchips{display:flex;flex-wrap:wrap;gap:8px;margin-block-start:10px}
.bchip{border-radius:99px;padding:4px 12px;font-size:13.5px;font-weight:700;background:var(--soft);color:var(--hint)}
.bchip.ok{background:var(--ok-soft);color:var(--ok)}.bchip.bad{background:rgba(229,72,77,.12);color:var(--danger)}
.bchip.warn{background:var(--warm);color:var(--warn-ink)}
.bf.bwait.hot{border:1.5px solid var(--warn);background:linear-gradient(var(--warm),var(--warm)),var(--card)}
.bi{border-block-start:1px solid var(--line)}
.bf-h+.bi{border-block-start:0;margin-block-start:2px}
.bw,.bl,.bn{display:flex;gap:12px;align-items:center;padding-block:10px}
.bw-b,.bl-b,.bn-b{flex:1;min-inline-size:0}
.bn-k{font-size:12.5px;color:var(--hint);font-weight:650}
.bw-t .bw-k{color:inherit;font-weight:650}
.bl-n b,.bw-k{max-inline-size:100%;min-inline-size:0;overflow-wrap:anywhere}
.b-agent{font-size:13px;font-weight:750;color:var(--link);overflow-wrap:anywhere}
.b-agent-text{border-inline-start:2px solid var(--link);padding-inline-start:8px}
.bw-t,.bn-t{font-size:15.5px;font-weight:650;overflow-wrap:anywhere;margin-block-start:2px}
.bw-n{font-size:13px;color:var(--hint);margin-block-start:2px}
.bage{font-size:12px;color:var(--hint);margin-block-start:4px;display:flex;flex-wrap:wrap;gap:4px 8px}
.bact{flex:none;min-block-size:36px;padding-inline:16px;border-radius:99px;border:0;font-weight:750;font-size:14.5px;cursor:pointer;
background:var(--accent);color:var(--on-accent);box-shadow:0 4px 12px rgba(47,124,246,.25)}
.bact.soft{background:var(--blue-soft);color:var(--link);box-shadow:none}
.bact:active{transform:scale(.95)}
.bexp{padding-block-end:4px}
.bl{align-items:flex-start;cursor:pointer}
.tdot{flex:none;inline-size:11px;block-size:11px;border-radius:50%;margin-block-start:6px;background:var(--idle)}
.t-ok .tdot{background:var(--ok);box-shadow:0 0 0 4px var(--ok-soft)}.t-wait .tdot{background:var(--warn);box-shadow:0 0 0 4px var(--warm)}
.t-bad .tdot{background:var(--danger);box-shadow:0 0 0 4px rgba(229,72,77,.14)}
.bl-n{display:flex;flex-direction:column;align-items:start;gap:8px}
.bl-n b{font-size:16px;font-weight:800}
.old-b{font-size:11px;font-weight:800;padding:1px 8px;border-radius:99px;background:var(--warn);color:#231600}
.live-b{font-size:11px;font-weight:750;padding:1px 8px;border-radius:99px;background:var(--ok-soft);color:var(--ok)}
.bl-s{font-size:15px;margin-block-start:3px;overflow-wrap:anywhere}
.bl-s.dim,.bl.stale .bl-s,.bw.stale .bw-t,.bn.stale .bn-t{color:var(--hint)}
.bl.stale .bl-s{text-decoration:line-through;text-decoration-color:var(--off)}
.bl-r{font-size:14px;color:var(--danger);font-weight:650;margin-block-start:3px}
.bl-c{font-size:13px;color:var(--link);font-weight:650;margin-block-start:3px}
.bl-x{margin-block:2px 10px;padding:10px 12px;border-radius:14px;background:var(--soft);font-size:13.5px}
.bl-x .row{padding-block:6px}
.bl-x .ck{display:flex;gap:8px;align-items:center;padding-block:3px}
.bl-x .ck svg.i{inline-size:16px;block-size:16px;color:var(--ok)}
.bl-x .ck.no svg.i{color:var(--off)}
.bl-x .bact{margin-block-start:8px}
.bl-b>.bact{margin-block-start:8px}
.trow{display:flex;align-items:center;gap:10px;inline-size:100%;text-align:start;cursor:pointer;font-size:14.5px;font-weight:650}
.trow .tr-a{flex:1;min-inline-size:0}
.trow .tr-m{flex:none;color:var(--hint)}
.trow .tr-m.m-ok{color:var(--ok)}.trow .tr-m.m-warn{color:var(--warn-ink)}.trow .tr-m.m-critical{color:var(--danger)}
.trow .stale{opacity:.6}
.askbox{position:sticky;inset-block-end:calc(80px + max(env(safe-area-inset-bottom),var(--tg-safe-area-inset-bottom,0px)));z-index:4;margin-block:6px 4px}
.ask-f{display:flex;align-items:center;gap:8px;padding-block:6px;padding-inline:14px 6px;border-radius:99px;background:var(--card);
border:1px solid var(--edge);box-shadow:var(--shadow-lg);-webkit-backdrop-filter:var(--glass);backdrop-filter:var(--glass)}
.ask-f>svg.i{color:var(--link)}
.ask-f input{flex:1;min-inline-size:0;border:0;background:transparent;font:inherit;font-size:15.5px;color:var(--text);padding-block:8px;outline:none}
.ask-f input::placeholder{color:var(--hint)}
.ask-go{flex:none;inline-size:38px;block-size:38px;border-radius:50%;border:0;background:var(--blue-soft);color:var(--link);display:grid;place-items:center;cursor:pointer}
.ask-go svg.i{transform:scaleX(var(--fx))}
.ask-q{display:none;flex-wrap:wrap;gap:8px;margin-block-end:8px}
.askbox.open .ask-q{display:flex}
.ask-q button{border:1px solid var(--edge);background:var(--card);color:var(--link);border-radius:99px;padding:6px 12px;font-size:13.5px;font-weight:650;cursor:pointer;box-shadow:var(--shadow)}
.askbox .safe{margin-block-start:6px}
.flash{animation:flash 1.2s ease}
@keyframes flash{0%,40%{box-shadow:0 0 0 2px var(--accent),var(--shadow)}100%{box-shadow:var(--shadow)}}

/* Shared disclosure: a compact story first, full details on demand. */
.compact{border:1px solid var(--edge);border-radius:16px;background:var(--field);margin-block:8px;max-inline-size:100%;overflow-wrap:anywhere}
.compact-toggle{display:flex;align-items:center;gap:8px;inline-size:100%;min-block-size:48px;padding-block:10px;padding-inline:12px;border:0;border-radius:16px;background:transparent;text-align:start;cursor:pointer}
.compact-toggle:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.compact-title{flex:1;font-size:13px;font-weight:750;line-height:1.3}
.compact-toggle>svg.i{flex:none;inline-size:14px;block-size:14px;transform:rotate(90deg);color:var(--hint);transition:transform .16s}
.compact.open>.compact-toggle>svg.i{transform:rotate(-90deg)}
.compact-figures{display:flex;align-items:center;gap:6px;max-inline-size:70%;font-size:12px;font-weight:700}
.compact-figure{display:inline-flex;align-items:center;gap:4px;min-block-size:24px;padding-block:3px;padding-inline:6px;border-radius:8px;background:var(--soft);font-variant-numeric:tabular-nums}
.compact-figure.cost{background:var(--blue-soft);color:var(--link)}
.compact-figure.cache{background:var(--ok-soft);color:var(--ok)}
.cache-ring{flex:none;inline-size:14px;block-size:14px;border-radius:50%;background:conic-gradient(var(--ok) var(--share),var(--soft) 0);position:relative}
.cache-ring::after{content:"";position:absolute;inset:3px;border-radius:50%;background:var(--card)}
.compact-reveal{display:grid;grid-template-rows:0fr;transition:grid-template-rows .16s ease}
.compact.open>.compact-reveal{grid-template-rows:1fr}
.compact-panel{overflow:hidden;min-block-size:0;visibility:hidden;transition:visibility .16s}
.compact.open>.compact-reveal>.compact-panel{visibility:visible}
.compact:not(.open)>.compact-reveal *{visibility:hidden!important}
.compact-inner{padding-block:0 12px;padding-inline:12px}
.compact-inner>.card{margin-block:8px}
.compact .compact{background:var(--card)}
.compact-mini{inline-size:30px;block-size:5px;background:var(--soft);border-radius:9px;overflow:hidden;flex:none}
.compact-mini>i{display:block;block-size:100%;background:var(--accent);border-radius:inherit}
.usage-summary .compact-title{font-size:12px}
.usage-summary>.compact-toggle{gap:5px;padding-inline:8px}
.usage-summary>.compact-toggle>.compact-figures{gap:4px;max-inline-size:78%;font-size:11px}
.usage-summary .compact-figure{padding-inline:5px}
/* Owner ledger checklist: independent of the surrounding collapsible sections. */
.topics{min-inline-size:0;margin-block:12px 20px}
.topic-cards{display:grid;gap:10px;min-inline-size:0}
.topic-card{display:block;inline-size:100%;min-inline-size:0;text-align:start;color:var(--text);padding:14px;border:1px solid var(--edge);border-radius:16px;background:var(--card);cursor:pointer;font:inherit}
.topic-card:focus-visible,.topic-close:focus-visible{outline:2px solid var(--link);outline-offset:2px}
.topic-heading{display:flex;align-items:center;gap:8px;justify-content:space-between;min-inline-size:0}
.topic-title{flex:1;font-size:16px;line-height:1.5;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-inline-size:0}
.topic-status{flex:none;white-space:nowrap}
.topic-progress{display:inline-flex;flex:none;align-items:center;gap:5px;font-size:12px;white-space:nowrap}
.topic-meter{display:block;inline-size:32px;block-size:3px;background:var(--edge);border-radius:3px;overflow:hidden}
.topic-meter-fill{display:block;block-size:100%;background:var(--link);border-radius:inherit}
.topic-status.owner,.topic-status.stuck{background:var(--warm);color:var(--warn-ink)}
.topic-status.done{background:var(--blue-soft);color:var(--link)}
.topic-project{display:block;color:var(--hint);font-size:12px;margin-block:4px 10px;overflow-wrap:anywhere}
.topic-field{display:block;margin-block-start:8px;min-inline-size:0;overflow-wrap:anywhere;font-size:13px;line-height:1.6}
.topic-field b{display:block;font-size:12px;color:var(--hint)}
.topic-card .topic-copy{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;line-clamp:2;overflow:hidden}
.topic-copy{display:block;white-space:pre-wrap;overflow-wrap:anywhere}
.topic-prs{display:flex;flex-wrap:wrap;gap:6px;margin-block-start:10px;font-size:12px;overflow-wrap:anywhere}
.topic-prs span{unicode-bidi:isolate}
.topic-sheet{box-sizing:border-box;inline-size:calc(100% - 24px);max-inline-size:600px;max-block-size:85dvh;margin-block:auto 12px;margin-inline:auto;padding:18px;border:1px solid var(--edge);border-radius:22px;background:var(--card);color:var(--text);overflow:auto;overscroll-behavior:contain}
.topic-sheet::backdrop{background:rgba(0,0,0,.45)}
.topic-sheet h2{margin:0;font-size:19px;overflow-wrap:anywhere;min-inline-size:0}
.topic-close{min-inline-size:64px;min-block-size:44px;flex:none}
.topic-timeline{padding-inline-start:22px;padding-inline-end:0;line-height:1.7;overflow-wrap:anywhere}
.topic-timeline li{padding-block:8px;border-block-end:1px solid var(--edge)}
.topic-timeline time{display:block;font-size:12px;color:var(--hint)}
/* The rail scrolls inside the section, never across the phone viewport. */
.requests{min-inline-size:0;max-inline-size:100%;margin-block:12px 20px}
.requests-filter{display:flex;flex-wrap:nowrap;gap:6px;margin-block:8px 12px;padding-block:3px 7px;padding-inline:2px;min-inline-size:0;max-inline-size:100%;overflow:auto;overscroll-behavior-inline:contain;scroll-snap-type:inline proximity;scrollbar-width:thin}
.requests-filter .btn{display:inline-flex;align-items:center;gap:7px;flex:none;min-inline-size:0;max-inline-size:85%;min-block-size:44px;padding-block:7px;padding-inline:12px;border-radius:99px;scroll-snap-align:start;white-space:nowrap;font-size:13px}
.requests-filter .btn[aria-pressed="true"]{background:var(--text);color:var(--bg);border-color:var(--text);font-weight:800}
.requests-pill-name{overflow:hidden;text-overflow:ellipsis;min-inline-size:0}
.requests-count{flex:none;font-variant-numeric:tabular-nums;opacity:.75}
.requests h3{font-size:14px;overflow-wrap:anywhere;margin-block:0}
.requests-needs{border:1px solid var(--edge);border-inline-start:3px solid var(--warn-ink);border-radius:16px;background:var(--card);margin-block-end:14px;overflow:hidden}
.request-groups{display:grid;gap:8px;min-inline-size:0;margin-block-end:10px}
.requests-fold{margin-block-end:10px}
.requests-fold .request-groups{margin-block-end:0}
.requests-head{display:flex;align-items:center;justify-content:space-between;gap:8px;padding-block:6px;padding-inline:12px;min-inline-size:0}
.requests-head .link-btn,.requests-more{min-block-size:44px;font-size:13px}
.requests-head h3{display:flex;align-items:center;gap:7px;min-inline-size:0}
.request-project{min-inline-size:0;max-inline-size:100%}
.request-project>.compact-toggle{min-block-size:60px}
.request-project>.compact-toggle>.compact-title{min-inline-size:0;font-size:14px}
.request-project .compact-figures{flex:none;flex-direction:column;align-items:flex-end;gap:2px;font-size:12px;max-inline-size:55%}
.requests-owner-count{color:var(--warn-ink)}
.requests-list{list-style:none;margin:0;padding:0;min-inline-size:0}
.request-project .requests-list{border:1px solid var(--edge);border-radius:12px;background:var(--card);overflow:hidden}
.request-row{min-inline-size:0;border-block-end:1px solid var(--line)}
.request-row:last-child{border-block-end:0}
.request-toggle{display:flex;align-items:flex-start;gap:8px;inline-size:100%;min-inline-size:0;min-block-size:44px;border:0;background:transparent;color:var(--text);text-align:start;cursor:pointer;padding-block:11px;padding-inline:12px}
.request-toggle:focus-visible,.requests-more:focus-visible{outline:2px solid var(--accent);outline-offset:-2px}
.request-toggle>.body{display:block;min-inline-size:0;flex:1}.request-toggle>.lead{flex:none;color:var(--hint);padding-block-start:2px}
.request-toggle>.lead svg{inline-size:16px;block-size:16px}
.request-row .nm{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;line-clamp:2;overflow:hidden;font-size:14px;font-weight:650;line-height:1.5;overflow-wrap:anywhere;white-space:pre-line;min-inline-size:0}
.request-row.open .nm{display:-webkit-box;-webkit-line-clamp:2;line-clamp:2;overflow:hidden}
.request-row .mt{display:flex;align-items:center;gap:8px;flex-wrap:nowrap;min-inline-size:0;margin-block-start:5px;color:var(--hint);font-size:11.5px;line-height:1.5;white-space:nowrap}
.request-row .chip{flex:none;border-radius:6px;background:var(--soft);padding-block:2px;padding-inline:6px;white-space:nowrap}
.request-row .chip.hot{background:var(--warm);color:var(--warn-ink)}
.request-age{flex:none;white-space:nowrap;font-variant-numeric:tabular-nums}
.request-context{display:block;flex:1;min-inline-size:0;font-size:11px;color:var(--hint);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.request-disclosure{display:block;margin-block-start:4px;color:var(--link);font-size:12px;line-height:1.5}
.request-details{padding-block:0 12px;padding-inline:37px 12px;min-inline-size:0}
.request-details[hidden]{display:none}
.request-full{font-size:14px;font-weight:650;line-height:1.6;overflow-wrap:anywhere;white-space:pre-line;margin-block:0 8px}
.request-note{display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;line-clamp:2;overflow:hidden;color:var(--hint);font-size:13px;line-height:1.7;white-space:pre-wrap;overflow-wrap:anywhere;margin-block:0 8px}
.request-row.open .request-note{display:block;-webkit-line-clamp:unset;line-clamp:unset;overflow:visible}
.request-steps{min-inline-size:0;font-size:12px}
.request-steps ol{margin-block:5px 0;padding-inline-start:20px;padding-inline-end:0;font-size:13px;line-height:1.7;overflow-wrap:anywhere}
.requests-more{inline-size:100%;border:0;border-block-start:1px solid var(--line);background:var(--blue-soft);color:var(--link);font-weight:700;cursor:pointer;border-end-start-radius:12px;border-end-end-radius:12px}
.request-project .requests-more{margin-block-start:6px;border-radius:12px}
.request-actions{display:flex;flex-wrap:wrap;gap:6px;padding-block:8px 0;padding-inline:0}
.request-actions .btn{min-block-size:44px;flex:1 1 auto;white-space:normal;overflow-wrap:anywhere}

.rem-actions{display:flex;flex-wrap:wrap;gap:4px;flex:none;max-inline-size:48px}
.rem-actions .rdone{margin-inline-start:0}
</style>
</head>
<body>
<header class="top">
<div class="ttl"><h1 id="t"></h1><div class="sub" id="sub" dir="auto"></div><div class="status" id="st"></div></div>
<button class="icon-btn" id="rf" type="button"></button>
</header>
<div id="ptr"></div>
<div id="tabs"></div>
<div id="radio-player" class="card" hidden></div>
<div id="my-music" class="card my-music" hidden></div>
<div id="app"><div class="sk c"></div><div class="sk c s"></div><div class="sk t"></div><div class="sk c"></div><div class="sk t"></div><div class="sk c s"></div><div class="sk c s"></div></div>
<div id="apps" hidden></div>
<div class="foot" id="foot"></div>
<div id="toast"></div>
<div id="probe" aria-hidden="true"></div>
<script nonce="__NONCE__">
(function(){
"use strict";
var S=__STRINGS__, LANG="__LANG__", wa=window.Telegram&&Telegram.WebApp, app=document.getElementById("app");
var FSI="⁨", PDI="⁩", NS="http://www.w3.org/2000/svg", RTL=document.documentElement.dir==="rtl";
var rtf=new Intl.RelativeTimeFormat(LANG,{numeric:"auto"}), nf=new Intl.NumberFormat(LANG);
var cf=new Intl.NumberFormat(LANG,{notation:"compact",maximumFractionDigits:0}), pr=new Intl.PluralRules(LANG);
var pf=new Intl.NumberFormat(LANG,{style:"percent",maximumFractionDigits:0});
var notes={}, closed={}, answered={}, srcOpen={}, noteOpen={}, seenIds={}, view=null, last=null, loading=false, reloadPending=false;
var radioOp=null;  // the switch this page asked for, kept across refreshes until the state shows it done
var digest=null, digestOff=false, opened={}, seenAt=0, RECENT=3*3600, followups=0, followTimer=null, modelBusy=false;
var cardDone={}, cardBusy={}, shownAt={}, stackAt=0, handled=0, stagger=0, redrawSoon=null, reduce=false;
var renderFingerprint=null,renderedView=null,renderDeferred=null,renderDeferredSince=0,lastInteraction=0,pendingRestore=null,renderNewContent=false,animateEntries=true,renderHasPainted=false;
var forkArmed={}, forkBusy={};
function questionTarget(value){return typeof value==="string"&&/^q[0-9a-f]{16}$/.test(value)?value.slice(1):"";}
function requestTarget(value){return typeof value==="string"&&/^r[0-9a-f]{16}$/.test(value)?value.slice(1):"";}
function questionAvailable(d,id){return (d.decisions||[]).some(function(x){return x.id===id&&(!x.stale||x.stale==="old");})||
 (d.cards||[]).some(function(x){return x.id===id&&(x.kind==="ask"||x.kind==="choice")&&!x.ended;});}
function questionIndex(list,id,fallback){var i=list.findIndex(function(x){return x.id===id;});return i<0?fallback:i;}
function scrollQuestion(id){var node=document.getElementById("question-"+id);if(!node)return false;node.scrollIntoView({block:"center"});return true;}
var target=questionTarget((wa&&wa.initDataUnsafe&&wa.initDataUnsafe.start_param)||new URLSearchParams(location.search).get("tgWebAppStartParam")||"");
var requestTargetKey=requestTarget((wa&&wa.initDataUnsafe&&wa.initDataUnsafe.start_param)||new URLSearchParams(location.search).get("tgWebAppStartParam")||""),requestTargetPending=!!requestTargetKey,requestTargetRow="";
// The list carries no per-row link key; the server says which row a widget link names, then the row opens once.
function resolveRequestTarget(){post("/api/request-target",{key:requestTargetKey}).then(function(res){if(!res.ok)throw res.status;return res.json();})
 .then(function(r){requestTargetRow=r.project+":"+r.id;if(last)render(last,true);}).catch(function(){requestTargetPending=false;});}
var targetPending=!!target, targetClosed=false, priorWaiting=null;
var ASKS={choice:1,ask:1}, TABKEY="ctb-panel-tab";
document.getElementById("t").textContent=S.title;
try{seenAt=+localStorage.getItem("ctb-panel-read")||0;}catch(e){}
try{view=sessionStorage.getItem(TABKEY)||null;}catch(e){}
try{reduce=window.matchMedia("(prefers-reduced-motion: reduce)").matches;}catch(e){}
function el(tag,cls,text){var n=document.createElement(tag);if(cls)n.className=cls;if(text!=null)n.textContent=text;return n;}
function iso(s){return FSI+s+PDI;}
function ver(v){try{return !!(wa&&wa.isVersionAtLeast&&wa.isVersionAtLeast(v));}catch(e){return false;}}
var ICONS={more:"M4 12a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0M10.5 12a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0M17 12a1.5 1.5 0 1 0 3 0a1.5 1.5 0 1 0-3 0",person:"M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM5 20a7 7 0 0 1 14 0",settings:"M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8zM12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6L7 7M17 17l1.4 1.4M18.4 5.6L17 7M7 17l-1.4 1.4",refresh:"M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7",check:"M5 12.5l4.5 4.5L19 7.5",chev:"M9 6l6 6-6 6",
 clock:"M12 7v5l3 2M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18z",x:"M6 6l12 12M18 6L6 18",link:"M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
 box:"M5 5h14v14H5z",bolt:"M13 3L5 13.5h6L10 21l8-10.5h-6z",
 mic:"M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3zM5 11a7 7 0 0 0 14 0M12 18v3",
 inbox:"M4 13l2.5-7h11L20 13M4 13v6h16v-6M4 13h5l1 2h4l1-2h5",cpu:"M8 8h8v8H8zM4 10h2M4 14h2M18 10h2M18 14h2M10 4v2M14 4v2M10 18v2M14 18v2",
 chat:"M5 5h14v10H9l-4 4z",pencil:"M4 20h4L19 9l-4-4L4 16zM13.5 6.5l4 4",send:"M21 3L3 10.5l7.5 2.5L13 21zM21 3L10.5 13",
 layers:"M12 3l9 5-9 5-9-5zM3 13l9 5 9-5",list:"M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01",chart:"M5 20v-8M12 20V5M19 20v-5",film:"M4 5h16v14H4zM8 5v14M16 5v14M4 9h4M4 14h4M16 9h4M16 14h4",minus:"M6 12h12",plus:"M12 6v12M6 12h12"};
var KIND_ICON={progress:"layers",choice:"list",ask:"pencil",link:"link",numbers:"chart"};
function icon(name){var s=document.createElementNS(NS,"svg");s.setAttribute("viewBox","0 0 24 24");s.setAttribute("class","i");
 s.setAttribute("aria-hidden","true");var p=document.createElementNS(NS,"path");p.setAttribute("d",ICONS[name]);s.appendChild(p);return s;}
function svgAdd(host,tag,attrs){var n=document.createElementNS(NS,tag);Object.keys(attrs).forEach(function(k){n.setAttribute(k,attrs[k]);});host.appendChild(n);return n;}
function haptic(kind,v){try{var h=wa&&wa.HapticFeedback;if(!h)return;if(kind==="tap")h.impactOccurred("light");
 else if(kind==="sel")h.selectionChanged();else h.notificationOccurred(v);}catch(e){}}
function ago(sec){if(sec==null)return "–";if(sec<45)return S.now;var m=Math.round(sec/60),h=Math.round(sec/3600);
 var t=m<60?rtf.format(-m,"minute"):h<36?rtf.format(-h,"hour"):rtf.format(-Math.round(sec/86400),"day");
 return t.replace(/\s*\(\d+\)/,"");}  // Hebrew ICU appends a "(1)" disambiguator to singular units
function until(v){if(!v)return "";var t=typeof v==="number"?v*1000:Date.parse(v);if(!t||isNaN(t))return "";
 var s=(t-Date.now())/1000;if(s<60)return rtf.format(1,"minute");var m=Math.round(s/60);if(m<90)return rtf.format(m,"minute");
 var h=Math.round(s/3600);if(h<36)return rtf.format(h,"hour");return rtf.format(Math.round(s/86400),"day");}
function plural(n,one,many){return pr.select(n)==="one"?one:many;}
function kt(n){return n?cf.format(n):"–";}
// a value drawn again moves from where it was last shown (bars, the ring), from zero the first time
function anim(key,v,apply){var p=key in shownAt?shownAt[key]:0;shownAt[key]=v;if(reduce||p===v){apply(v);return;}apply(p);
 requestAnimationFrame(function(){requestAnimationFrame(function(){apply(v);});});}
var toastTimer=null;
function toast(text,bad){var box=document.getElementById("toast");box.textContent="";var d=el("div",bad?"bad":"");
 if(!bad)d.appendChild(icon("check"));d.appendChild(el("span",null,text));box.appendChild(d);
 requestAnimationFrame(function(){d.className+=" show";});clearTimeout(toastTimer);
 toastTimer=setTimeout(function(){d.className=d.className.replace(" show","");},2800);}
function post(path,body){return fetch(path,{method:"POST",headers:{"X-Telegram-Init-Data":wa.initData,"Content-Type":"application/json"},
 body:JSON.stringify(body)});}
// a typed, unsent note or answer asks before the panel closes
// A text field's direction: an empty field follows the page, so a Hebrew placeholder sits on the right (dir="auto"
// on an empty field is always left-to-right); once there is text, the text decides. It also grows with its text.
function tune(ta,grow){
 var f=function(){ta.dir=ta.value?"auto":(RTL?"rtl":"ltr");
  if(grow){ta.style.blockSize="auto";ta.style.blockSize=ta.scrollHeight?Math.min(ta.scrollHeight,168)+"px":"";}};
 ta.addEventListener("input",f);f();setTimeout(f,0);return f;}
// The composer: a field with its send button inside. `send(text,done)` sends; done(true) clears it, done(false) keeps the text.
function composer(key,max,ph,send){
 var row=el("div","askrow"),ta=el("textarea");ta.maxLength=max;ta.rows=1;ta.placeholder=ph;ta.value=notes[key]||"";
 ta.setAttribute("enterkeyhint","enter");ta.setAttribute("autocapitalize","sentences");
 var cnt=el("span","cnt");cnt.dir="ltr";cnt.hidden=true;
 var sb=el("button","send");sb.type="button";sb.appendChild(icon("send"));sb.setAttribute("aria-label",S.send);
 var fit=tune(ta,true);
 var sync=function(){var n=ta.value.length;sb.disabled=!ta.value.trim();cnt.hidden=n<max*.8;cnt.textContent=String(max-n);cnt.className="cnt"+(max-n<=20?" hot":"");};
 var go=function(){var t=ta.value.trim();if(!t||sb.disabled)return;haptic("tap");sb.disabled=true;ta.disabled=true;row.classList.add("busy");
  send(t,function(ok){ta.disabled=false;row.classList.remove("busy");if(ok){delete notes[key];ta.value="";}fit();sync();syncClose();});};
 ta.addEventListener("input",function(){notes[key]=ta.value;sync();syncClose();});
 ta.addEventListener("keydown",function(e){if(e.key==="Enter"&&(e.metaKey||e.ctrlKey)){e.preventDefault();go();}});
 sb.addEventListener("click",go);
 row.addEventListener("click",function(e){if(e.target===row)ta.focus();});
 sync();row.appendChild(ta);row.appendChild(cnt);row.appendChild(sb);return {row:row,ta:ta,sb:sb};}
function syncClose(){if(!ver("6.2"))return;var any=Object.keys(notes).some(function(k){return typeof notes[k]==="string"&&notes[k].trim();});
 try{if(any)wa.enableClosingConfirmation();else wa.disableClosingConfirmation();}catch(e){}}
// Each entry is one reversible UI change. There is no extra root/sentinel entry: once these are
// exhausted, browser/system back is free to leave the document and Telegram shows its close button.
var backEntries=[],backIndex=0,backPending=false,backReplay=false,backHistory=true,backLayers={};
var backToken=String(Date.now())+":"+Math.random(),backTrim=null,moreOpen=false,backSwitch=null;
function backState(index){var state=Object.assign({},history.state);state.cockpitBack={token:backToken,index:index};return state;}
try{history.replaceState(backState(0),"");}catch(e){backHistory=false;}
function pushBack(entry){if(backReplay)return;backEntries.length=backIndex;
 if(entry.key)backEntries.forEach(function(e){if(e.key===entry.key)e.active=false;});
 var reuse=backIndex&&!backEntries[backIndex-1].active;
 if(reuse)backEntries[backIndex-1]=entry;else{backEntries.push(entry);backIndex++;}
 try{if(backHistory)history[reuse?"replaceState":"pushState"](backState(backIndex),"");}catch(e){backHistory=false;}syncBack();}
function backDepth(){return backEntries.slice(0,backIndex).filter(function(e){return e.active;}).length;}
function backTab(from,to){var entry={active:true,home:from,tab:to,
 undo:function(){switchTo(from,false);},redo:function(){switchTo(entry.tab,false);}};return entry;}
function currentBackLayer(key){for(var i=backIndex-1;i>=0;i--){var e=backEntries[i];
 if(e.active&&!e.key)return false;if(e.active&&e.key===key)return true;}return false;}
function trimBack(){clearTimeout(backTrim);if(backReplay||backPending||!backIndex||backEntries[backIndex-1].active)return;
 var to=backIndex;while(to&&!backEntries[to-1].active)to--;travelBack(to);}
function travelBack(to){if(backPending||to===backIndex)return;if(backHistory){backPending=true;history.go(to-backIndex);}else restoreBack(to);}
function restoreBack(to){var from=backIndex,previous=view;backPending=false;backIndex=to;backReplay=true;
 try{if(to<from){for(var i=from-1;i>=to;i--)if(backEntries[i].active)backEntries[i].undo();}
  else{for(var j=from;j<to;j++)if(backEntries[j].active)backEntries[j].redo();}}
 finally{backReplay=false;}
 if(backSwitch!==null){var next=backSwitch;backSwitch=null;
  try{if(backHistory)history.replaceState(backState(backIndex),"");}catch(e){backHistory=false;}
  switchTo(next,false);}
 if(view!==previous)adoptBackLayers();syncBack();trimBack();}
window.addEventListener("popstate",function(e){var state=e.state&&e.state.cockpitBack;
 // Unknown history belongs to the browser, never push the user back into the Mini App.
 if(!state||state.token!==backToken)return;
 if(Number.isInteger(state.index)&&state.index>=0&&state.index<=backEntries.length)restoreBack(state.index);});
function retireBack(key){var layer=backLayers[key];backEntries.slice(0,backIndex).forEach(function(e){var child=backLayers[e.key];
 if(e.key===key||(layer&&layer.node&&child&&child.node&&layer.node.contains(child.node)))e.active=false;});
 syncBack();clearTimeout(backTrim);backTrim=setTimeout(trimBack,0);}
function retireMissingBackLayers(){backEntries.slice(0,backIndex).forEach(function(e){
 // Other views lose their DOM during navigation, without closing their saved layers.
 if(!e.active||!e.key||e.key.indexOf(view+"|")!==0)return;
 var b=backLayers[e.key];if(!b||(b.node&&!b.node.isConnected))e.active=false;});
 syncBack();clearTimeout(backTrim);backTrim=setTimeout(trimBack,0);}
function bindBackLayer(key,node,on,apply){var suffix="|"+key;
 // A shared disclosure keeps its original step when another tab renders the same state.
 var shared=backEntries.slice(0,backIndex).find(function(e){return e.active&&e.key&&e.key.endsWith(suffix);});
 key=shared?shared.key:view+suffix;
 var binding={node:node,on:!!on,apply:apply};backLayers[key]=binding;
 function set(value){var b=backLayers[key];if(!b)return;b.on=value;b.apply(value);}
 function remember(){if(currentBackLayer(key))return;
  pushBack({active:true,key:key,undo:function(){set(false);},redo:function(){set(true);}});}
 return function(value){set(value);if(!backReplay){if(value)remember();else retireBack(key);}syncBack();};}
function adoptBackLayers(){Object.keys(backLayers).filter(function(key){var b=backLayers[key];
 return key.indexOf(view+"|")===0&&b.on&&b.node&&b.node.isConnected&&b.node.getClientRects().length&&getComputedStyle(b.node).visibility!=="hidden";
 }).sort(function(a,b){return backLayers[a].node.compareDocumentPosition(backLayers[b].node)&Node.DOCUMENT_POSITION_FOLLOWING?-1:1;
 }).forEach(function(key){
 if(!currentBackLayer(key)){
  pushBack({active:true,key:key,undo:function(){var x=backLayers[key];if(x){x.on=false;x.apply(false);}},
   redo:function(){var x=backLayers[key];if(x){x.on=true;x.apply(true);}}});}});}
// --- text that agents wrote: Hebrew when ready, the original folded under a toggle ---------------------
function says(key,orig,he,cls){
 var box=el("div",null);var main=el("div",cls,he||orig);main.dir="auto";box.appendChild(main);return box;}
function source(key,parts,host){
 if(!Array.isArray(parts)){
  var pairs=[];function collect(x){if(!x||typeof x!=="object")return;
   Object.keys(x).forEach(function(f){if(f.endsWith("_he"))return;var v=x[f],h=x[f+"_he"];
    if(typeof v==="string"&&f+"_he" in x)pairs.push([v,h]);
    else if(Array.isArray(v)&&Array.isArray(h))pairs.push([v.filter(function(a){return typeof a==="string";}).join("\n"),h.some(function(a){return typeof a==="string"&&a;})?h.filter(function(a){return typeof a==="string";}).join("\n"):h.indexOf(null)>=0?null:undefined]);
    if(v&&typeof v==="object")collect(v);});}collect(parts);parts=pairs;
 }
 // parts: [[original, hebrew-or-null-or-undefined], ...]; adds the toggle + original, or a translating note
 var tr=parts.some(function(p){return p[1]===null;}), any=parts.some(function(p){return typeof p[1]==="string"&&p[1];});
 if(any){var t=el("button","src-t"+(srcOpen[key]?" on":""),srcOpen[key]?S.hideSource:S.source);t.type="button";
  var o=el("div","orig",parts.filter(function(p){return p[0];}).map(function(p){return p[0];}).join("\n\n"));o.dir="ltr";o.hidden=!srcOpen[key];
  var setSource=bindBackLayer("source:"+key,o,srcOpen[key],function(on){srcOpen[key]=on;o.hidden=!on;
   t.textContent=on?S.hideSource:S.source;t.className="src-t"+(on?" on":"");});
  t.addEventListener("click",function(){setSource(!srcOpen[key]);haptic("sel");});host.appendChild(t);host.appendChild(o);}
 else if(tr){var n=el("div","tr");n.appendChild(el("span","dotpulse"));n.appendChild(el("span",null,S.translating));host.appendChild(n);}
}
function he(x,f){return x[f+"_he"];}
function shown(x,f){var h=x[f+"_he"];if(Array.isArray(h)&&Array.isArray(x[f]))return x[f].map(function(v,i){return typeof h[i]==="string"&&h[i]?h[i]:v;});return typeof h==="string"&&h?h:x[f];}
// --- small parts ----------------------------------------------------------------------------------------
function sec(title,n,cls){var h=el("div","sec"+(cls?" "+cls:""));h.appendChild(el("h2",null,title));
 if(n!=null)h.appendChild(el("span","n",nf.format(n)));app.appendChild(h);return h;}
function empty(ic,title,hint,good){var c=el("div","card empty");var i=el("div","ic"+(good?" good":""));i.appendChild(icon(ic));c.appendChild(i);
 var b=el("div");b.appendChild(el("b",null,title));if(hint)b.appendChild(el("small",null,hint));c.appendChild(b);return c;}
function enter(node,key){if(animateEntries&&!seenIds[key]){node.className+=" enter";node.style.animationDelay=Math.min(stagger++,8)*45+"ms";seenIds[key]=1;}return node;}
var BOTS=[["#3b82f6","#dbe8ff"],["#8b5cf6","#ece4ff"],["#f59e0b","#fff0d4"],["#14b8a6","#d3f5ef"],["#ec4899","#fde1ef"],["#6366f1","#e2e4ff"]];
function hash(s){var h=0;s=String(s||"");for(var i=0;i<s.length;i++)h=(h*31+s.charCodeAt(i))|0;return Math.abs(h);}
function robot(sid,state,small){
 // a small robot face, its colours picked from the session id
 var p=BOTS[hash(sid)%BOTS.length],w=el("div","bot"+(state?" "+state:"")+(small?" sm":""));
 var s=document.createElementNS(NS,"svg");s.setAttribute("viewBox","0 0 40 40");s.setAttribute("aria-hidden","true");
 svgAdd(s,"circle",{cx:20,cy:20,r:20,fill:p[1]});
 svgAdd(s,"path",{d:"M9 40v-2.5a11 11 0 0 1 22 0V40z",fill:p[0],opacity:".9"});
 svgAdd(s,"line",{x1:20,y1:6.5,x2:20,y2:10.5,stroke:p[0],"stroke-width":2,"stroke-linecap":"round"});
 svgAdd(s,"circle",{cx:20,cy:5.5,r:2.3,fill:p[0]});
 svgAdd(s,"rect",{x:6,y:16.5,width:3.2,height:7,rx:1.6,fill:p[0]});svgAdd(s,"rect",{x:30.8,y:16.5,width:3.2,height:7,rx:1.6,fill:p[0]});
 svgAdd(s,"rect",{x:8.5,y:10.5,width:23,height:20,rx:8,fill:"#ffffff",stroke:p[0],"stroke-width":2});
 svgAdd(s,"rect",{x:11.5,y:14.5,width:17,height:9.5,rx:4.75,fill:"#1b2340"});
 svgAdd(s,"circle",{cx:16.3,cy:19.2,r:1.9,fill:"#7cf3ff","class":"eye"});svgAdd(s,"circle",{cx:23.7,cy:19.2,r:1.9,fill:"#7cf3ff","class":"eye"});
 svgAdd(s,"path",{d:"M17 26.6q3 1.9 6 0",stroke:p[0],"stroke-width":1.7,fill:"none","stroke-linecap":"round"});
 w.appendChild(s);return w;}
function bar(p){var b=el("div","bar"),i=el("i",p>=85?"bad":p>=60?"warn":"");i.style.inlineSize=Math.max(2,Math.min(100,p))+"%";b.appendChild(i);return b;}
function kv(k,v){var r=el("div","kv");r.appendChild(el("span",null,k));var x=el("span",null,v);r.appendChild(x);return r;}
function line(cls,ic,text){var r=el("div",cls);r.appendChild(icon(ic));var t=el("span",null,text);t.dir="auto";r.appendChild(t);return r;}
// Only the viewer ID namespaces preferences; no auth/session material is stored.
var compactSerial=0;
function compactStorageKey(key){var viewer="local";
 try{var user=wa&&wa.initDataUnsafe&&wa.initDataUnsafe.user;
  if(!user&&wa&&wa.initData)user=JSON.parse(new URLSearchParams(wa.initData).get("user")||"null");
  if(user&&user.id!=null)viewer=String(user.id);}catch(e){}
 return "ctb-panel-block:"+viewer+":"+key;}
function compactBlock(key,title,figures,body,cls,force,startOpen){
 var box=el("div","compact"+(cls?" "+cls:"")),button=el("button","compact-toggle"),reveal=el("div","compact-reveal"),panel=el("div","compact-panel"),inner=body||el("div");
 var storageKey=compactStorageKey(key),open=false,id="compact-"+(++compactSerial);
 open=!!startOpen;
 try{var saved=localStorage.getItem(storageKey);if(saved!==null)open=saved==="1";}catch(e){}
 if(force)open=true;
 box.setAttribute("data-block",key);button.type="button";button.setAttribute("aria-controls",id);panel.id=id;
 button.appendChild(el("span","compact-title",title));
 if(figures){var stats=typeof figures==="string"?el("span","compact-figures"):figures;if(typeof figures==="string")stats.appendChild(el("span","compact-figure",figures));button.appendChild(stats);}
 button.appendChild(icon("chev"));inner.className+=(inner.className?" ":"")+"compact-inner";
 panel.appendChild(inner);reveal.appendChild(panel);box.appendChild(button);box.appendChild(reveal);box.body=inner;
 function draw(){box.className="compact"+(cls?" "+cls:"")+(open?" open":"");button.setAttribute("aria-expanded",String(open));
  button.title=open?S.compactCollapse:S.compactExpand;panel.inert=!open;panel.setAttribute("aria-hidden",String(!open));}
 var setOpen=bindBackLayer("compact:"+key,box,open,function(on){open=on;draw();try{localStorage.setItem(storageKey,on?"1":"0");}catch(e){}});
 button.addEventListener("click",function(){setOpen(!open);haptic("sel");adoptBackLayers();});
 draw();return box;}
function fold(key,title,n,cls){return compactBlock(key,title,n!=null?nf.format(n):null,null,cls);}
function compactLong(host,key,title,body,n){host.appendChild(n>4?compactBlock(key,title,nf.format(n),body):body);}

function openLink(u){try{if(/^https:\/\/t\.me\//i.test(u))wa.openTelegramLink(u);else wa.openLink(u);}catch(e){location.href=u;}}
function topicBtn(url){var b=el("button","link-btn go",S.openTopic);b.type="button";b.prepend(icon("chat"));
 b.addEventListener("click",function(e){e.preventDefault();e.stopPropagation();haptic("tap");openLink(url);});return b;}
function later(){clearTimeout(redrawSoon);redrawSoon=setTimeout(function(){if(last)render(last,false);},1500);}
// --- decisions --------------------------------------------------------------------------------------------
function deliveryLabel(state){return state==="delivered"?S.deliveryDone:state==="in_flight"?S.deliveryFlight:state==="queued"?S.deliveryQueued:S.deliveryUnknown;}
function receiptFor(key){return (((last&&last.deliveries)||[]).filter(function(r){return r.key===key;}))[0];}
function receiptText(key,fallback){var r=key&&receiptFor(key);return r?deliveryLabel(r.state)+(r.retrying?" · "+S.deliveryRetry:""):fallback;}
function deliveryReceipts(d,here){
 var rows=(d.deliveries||[]).filter(function(r){return (!here||r.here)&&!(r.kind==="card"&&cardDone[r.id])&&!(r.kind==="decision"&&answered[r.id]);});
 if(!rows.length)return;var f=fold("deliveries"+(here?"-here":""),S.deliveryTitle,rows.length);
 rows.forEach(function(r){var c=el("div","card"),state=el("b",null,deliveryLabel(r.state));c.appendChild(state);
  var title=el("div",null,shown(r,"title"));title.dir="auto";c.appendChild(title);c.appendChild(sidTag(r.session));
  if(r.retrying)c.appendChild(el("div","err",S.deliveryRetry));f.body.appendChild(c);});app.appendChild(f);}
function doneCard(d,a){var c=el("div","card");var row=el("div","done-c");var ok=el("div","ok"+(a.choice==="yes"?"":" no"));
 ok.appendChild(icon(a.choice==="yes"?"check":"chat"));row.appendChild(ok);var b=el("div");b.style.minWidth="0";
 b.appendChild(el("b",null,receiptText(a.key,a.msg)));b.appendChild(el("small",null,shown(d,"title")));
 if(a.label)b.appendChild(el("small",null,S.chose+": "+a.label));row.appendChild(b);c.appendChild(row);return enter(c,"done"+d.id);}
function answer(card,d,choice,btns,ta){
 haptic("tap");btns.forEach(function(b){b.disabled=true;});ta.disabled=true;
 var old=card.querySelector(".err");if(old)old.remove();
 var label=choice==="yes"?shown(d,"yes"):shown(d,"no");
 function settle(msg,kind,key){closed[d.id]=1;delete notes[d.id];syncClose();handled++;answered[d.id]={choice:choice,msg:msg,key:key,label:kind==="sent"?label:"",d:d};
  var c=doneCard(d,answered[d.id]);card.replaceWith(c);later();}
 post("/api/decide",{id:d.id,choice:choice,note:ta.value.trim()})
  .then(function(r){
   if(r.ok){return r.json().then(function(j){haptic("n","success");settle(deliveryLabel(j.delivery),"sent",j.key);toast(deliveryLabel(j.delivery));});}
   if(r.status===409){return r.json().then(function(j){
     if(j.error==="queue full"){btns.forEach(function(b){b.disabled=false;});ta.disabled=false;card.appendChild(el("div","err",S.queueFull));return;}
     haptic("n","warning");
     settle(j.state==="answered"?S.already+": "+(j.choice==="yes"?shown(d,"yes"):shown(d,"no")):S.gone,"closed");});}
   if(r.status===404){haptic("n","warning");settle(S.gone,"closed");return;}
   haptic("n","error");btns.forEach(function(b){b.disabled=false;});ta.disabled=false;
   card.appendChild(el("div","err",r.status===403?S.reopen:r.status===503?S.busy:S.failed));})
  .catch(function(){haptic("n","error");btns.forEach(function(b){b.disabled=false;});ta.disabled=false;
   card.appendChild(el("div","err",S.failed));});
}
function decision(d,showTopic){
 var c=el("div","card ask"),top=el("div","ask-top");
 c.id="question-"+d.id;if(d.id===target)c.classList.add("question-highlight");
 var where=el("span","where",showTopic&&d.topic?shown(d,"topic"):S.waiting);where.dir="auto";top.appendChild(where);
 if(isNew(d)&&!d.old)top.appendChild(el("span","badge",S.newBadge));  // an old item is never "new"
 top.appendChild(el("span",null,ago(d.age)));if(d.url)top.appendChild(topicBtn(d.url));c.appendChild(top);
 c.appendChild(says("q"+d.id,d.title,he(d,"title"),"q"));
 if(d.detail)c.appendChild(says("d"+d.id,d.detail,he(d,"detail"),"detail"));
 source("s"+d.id,d,c);
 // A Claude Code permission request: its exact text, as is (textContent, never translated, left to right).
 if(typeof d.request==="string"&&d.request){var rq=el("pre","pq-request",d.request);rq.dir="ltr";c.appendChild(rq);}
 var ta=el("textarea");ta.maxLength=300;ta.rows=2;ta.placeholder=S.notePh;ta.value=notes[d.id]||"";var fitNote=tune(ta,true);
 ta.addEventListener("input",function(){notes[d.id]=ta.value;syncClose();});
 var nt=el("button","note-t");nt.type="button";nt.appendChild(icon("pencil"));nt.appendChild(el("span",null,S.addNote));
 if(noteOpen[d.id]||ta.value){nt.hidden=true;}else{ta.hidden=true;}
 var setNote=bindBackLayer("note:"+d.id,ta,!!noteOpen[d.id],function(on){noteOpen[d.id]=on;nt.hidden=on;ta.hidden=!on;fitNote();});
 nt.addEventListener("click",function(){setNote(true);ta.focus();haptic("sel");});
 c.appendChild(nt);c.appendChild(ta);
 var r=el("div","btns"),yes=el("button","btn yes"),no=el("button","btn",shown(d,"no"));
 yes.appendChild(icon("check"));yes.appendChild(el("span",null,shown(d,"yes")));
 no.type=yes.type="button";
 [[yes,"yes"],[no,"no"]].forEach(function(p){p[0].addEventListener("click",function(){answer(c,d,p[1],[no,yes],ta);});r.appendChild(p[0]);});
 if(d.decline_only===true)yes.disabled=true;  // a request the phone cannot show exactly is approved at the computer
 c.appendChild(r);c.appendChild(el("div","safe",S.tapNote));return enter(c,"d"+d.id);}
function isNew(x){return !!last&&(last.now-x.age)*1000>seenAt;}
function markSeen(){seenAt=Date.now();try{localStorage.setItem("ctb-panel-read",String(seenAt));}catch(e){}
 digestOff=true;haptic("n","success");toast(S.markedRead);render(last,true);}
function stack(list,showTopic){
 // the open decisions as a stack: the top one in full, the next ones peeking behind it; swipe or tap a dot
 var n=list.length;if(targetPending)stackAt=questionIndex(list,target,stackAt);if(stackAt>=n)stackAt=n-1;if(stackAt<0)stackAt=0;
 var w=el("div","stack"+(n>1?" multi":""));for(var g=Math.min(2,n-1);g>=1;g--)w.appendChild(el("div","ghost g"+g));
 var top=decision(list[stackAt],showTopic);w.appendChild(top);
 var dots=el("div","dots");
 function go(i){if(i<0||i>=n||i===stackAt)return;haptic("sel");var fwd=i>stackAt;stackAt=i;
  var nc=decision(list[i],showTopic);nc.className+=" swap"+(fwd?"":" back");top.replaceWith(nc);top=nc;
  Array.prototype.forEach.call(dots.children,function(b,k){b.className="dot"+(k===i?" on":"");});}
 if(n>1){list.forEach(function(x,i){var b=el("button","dot"+(i===stackAt?" on":""));b.type="button";
   b.setAttribute("aria-label",nf.format(i+1)+" "+S.of+" "+nf.format(n));b.addEventListener("click",function(){go(i);});dots.appendChild(b);});
  var x0=null,y0s=0;
  w.addEventListener("touchstart",function(e){var t=e.target;x0=(t.closest&&t.closest("textarea"))||e.touches.length!==1?null:e.touches[0].clientX;
   y0s=e.touches[0].clientY;},{passive:true});
  w.addEventListener("touchend",function(e){if(x0==null)return;var p=e.changedTouches[0],dx=p.clientX-x0,dy=p.clientY-y0s;x0=null;
   if(Math.abs(dx)>50&&Math.abs(dx)>Math.abs(dy)*1.4){var fwd=RTL?dx>0:dx<0;go(stackAt+(fwd?1:-1));}},{passive:true});}
 var box=el("div");box.appendChild(w);if(n>1)box.appendChild(dots);return box;}
// a decision is "old" when the server says it waited over three days (x.old; the server's clock, not this phone's)
// and is not already closed for another reason; those are grouped under "Older" instead of the stack
function inOld(x){return !!x.old&&(!x.stale||x.stale==="old");}
function staleBox(old){
 var det=fold("stale",S.staleList,old.length),c=el("div","card group");
 c.appendChild(el("div","safe pad",S.staleHint));
 old.forEach(function(x){var r=el("div","row");var l=el("div","lead");l.appendChild(icon("clock"));r.appendChild(l);
  var b=el("div","body"),n=el("div","nm",shown(x,"title"));n.dir="auto";b.appendChild(n);var m=el("div","mt");
  m.appendChild(el("span","chip hot",S["why_"+x.stale]||x.stale));if(x.topic){var tp=el("span",null,shown(x,"topic"));tp.dir="auto";m.appendChild(tp);}
  m.appendChild(el("span",null,ago(x.age)));b.appendChild(m);r.appendChild(b);c.appendChild(r);});
 var btn=el("button","btn sm",S.clearAll);btn.type="button";
 btn.addEventListener("click",function(){haptic("tap");btn.disabled=true;
  post("/api/dismiss",{ids:old.map(function(x){return x.id;})}).then(function(r){if(!r.ok)throw r.status;return r.json();})
   .then(function(j){(j.closed||[]).forEach(function(id){closed[id]=1;});haptic("n","success");
    toast(S.cleared+": "+nf.format((j.closed||[]).length));render(last,true);})
   .catch(function(st){haptic("n","error");btn.disabled=false;toast(st===403?S.reopen:st===503?S.busy:S.failed,true);});});
 var row=el("div","btns pad");row.appendChild(btn);c.appendChild(row);det.body.appendChild(c);return det;}
function answeredIds(showTopic){return Object.keys(answered).filter(function(id){return showTopic||(answered[id].d&&answered[id].d.here);});}
function decisions(list,showTopic){
 var shownN=0,fresh=list.filter(function(x){return !x.stale&&!inOld(x)&&!closed[x.id];}),old=list.filter(function(x){return x.stale&&x.stale!=="old"&&!closed[x.id];});
 answeredIds(showTopic).forEach(function(id){app.appendChild(doneCard(answered[id].d,answered[id]));});
 if(fresh.length){app.appendChild(stack(fresh,showTopic));shownN=fresh.length;}
 if(old.length)app.appendChild(staleBox(old));
 return shownN;}
// --- agent cards ------------------------------------------------------------------------------------------
function openSafe(u){if(typeof u!=="string"||!/^https:\/\//i.test(u)||!wa)return;try{wa.openLink(u);}catch(e){}}
function hostOf(u){try{return new URL(u).host;}catch(e){return "";}}
function cardDoneBox(x){var row=el("div","done-c"),ok=el("div","ok"+(x.ok?"":" no"));ok.appendChild(icon(x.ok?"check":"chat"));row.appendChild(ok);
 var b=el("div");b.style.minWidth="0";b.appendChild(el("b",null,receiptText(x.key,x.msg)));
 if(x.label){var s=el("small",null,(x.kind==="ask"?S.wrote:S.chose)+": "+x.label);s.dir="auto";b.appendChild(s);}row.appendChild(b);return row;}
function sendCard(c,box,body,label,btns,chosen,ta){
 if(cardBusy[c.id])return;cardBusy[c.id]=1;haptic("tap");
 btns.forEach(function(b){b.disabled=true;});if(ta)ta.disabled=true;if(chosen)chosen.className+=" on";box.className+=" busy";
 var old=box.querySelector(".err");if(old)old.remove();
 function settle(msg,ok,key){delete cardBusy[c.id];delete notes["c"+c.id];syncClose();handled++;
  cardDone[c.id]={msg:msg,key:key,label:ok?label:"",ok:ok,kind:c.kind,c:c};box.replaceWith(agentCard(c,/ in-g\b/.test(box.className)));later();}
 function undo(msg){delete cardBusy[c.id];haptic("n","error");btns.forEach(function(b){b.disabled=false;});
  if(ta){ta.disabled=false;btns.forEach(function(b){b.disabled=!ta.value.trim();});}
  if(chosen)chosen.className=chosen.className.replace(" on","");box.className=box.className.replace(" busy","");box.appendChild(el("div","err",msg));}
 post("/api/card",body)
  .then(function(r){
   if(r.ok){return r.json().then(function(j){haptic("n","success");settle(deliveryLabel(j.delivery),true,j.key);toast(deliveryLabel(j.delivery));});}
   if(r.status===409){return r.json().then(function(j){if(j.error==="queue full"){undo(S.queueFull);return;}haptic("n","warning");settle(S.cardAlready,false);});}
   if(r.status===404){haptic("n","warning");settle(S.cardGone,false);return;}
   undo(r.status===403?S.reopen:r.status===503?S.busy:S.failed);})
  .catch(function(){undo(S.failed);});}
var cardTextOpen={};
function cardText(c,field){
 var box=el("div",null),key=c.id+":"+field,button=el("button","card-copy-toggle"),text=el("span","ac-d card-copy-preview",shown(c,field));
 button.type="button";button.dir="auto";text.dir="auto";button.appendChild(text);box.appendChild(button);
 function draw(){var open=!!cardTextOpen[key];button.className="card-copy-toggle"+(open?" expanded":"");
  button.setAttribute("aria-expanded",String(open));button.title=open?S.cardTextLess:S.cardTextMore;}
 button.addEventListener("click",function(){cardTextOpen[key]=!cardTextOpen[key];draw();haptic("sel");});draw();return box;}
function progressBody(c,box){
 var steps=Array.isArray(c.steps)?c.steps:[],at=typeof c.at==="number"?c.at:0;
 var pct=typeof c.percent==="number"?c.percent:steps.length?at/steps.length*100:null;
 if(pct!=null){var row=el("div","pg"),pb=el("div","pbar"),fill=el("i");pb.appendChild(fill);row.appendChild(pb);
  row.appendChild(el("b","pct",pf.format(Math.max(0,Math.min(100,pct))/100)));box.appendChild(row);
  anim("c"+c.id,pct,function(v){fill.style.inlineSize=Math.max(2,Math.min(100,v))+"%";});}
 if(steps.length){var tr=el("ol","track");tr.style.gridTemplateColumns="repeat("+steps.length+",minmax(0,1fr))";
  steps.forEach(function(s,i){var li=el("li",i<at?"done":i===at?"cur":"");var dot=el("span","sd");if(i<at)dot.appendChild(icon("check"));li.appendChild(dot);
   var lb=el("span","sl",shown(c,"steps")[i]);lb.dir="auto";li.appendChild(lb);tr.appendChild(li);});box.appendChild(tr);}
 // Labels and agent text stay on separate lines, each with its own direction.
 if(c.activity){box.appendChild(el("div","ac-status",S.cardWorking));
  box.appendChild(cardText(c,"activity"));
  box.appendChild(el("div","ac-status",S.cardUpdated+" "+ago(c.activity_age)));}
 else if(c.live){box.appendChild(el("div","ac-status",S.cardNow));
  box.appendChild(cardText(c,"live"));
  var age=typeof c.live_age==="number"?c.live_age:Math.max(0,((last&&last.now)||Date.now()/1000)-c.live_at);
  box.appendChild(el("div","ac-status",S.cardUpdated+" "+ago(age)));}
 if(c.task){box.appendChild(el("div","ac-status",S.cardTask));
  box.appendChild(cardText(c,"task"));}
 if(c.automatic)box.appendChild(el("div","ac-status",S.cardAuto));
 if(c.bg)box.appendChild(el("div","ac-status",nf.format(c.bg)+" "+plural(c.bg,S.bg1,S.bg)));}
function choiceBody(c,box){
 if(Array.isArray(c.images)&&c.images.length===c.options.length&&c.images.every(function(id){return /^[0-9a-f]{32}$/.test(id);})){
  choiceTiles(c,box);return;}
 var opts=Array.isArray(c.options)?c.options:[],long=opts.some(function(o){return String(o).length>14;});
 var g=el("div","opts"+(long?"":opts.length===3||opts.length>4?" c3":" c2"));
 opts.forEach(function(o,i){var label=shown(c,"options")[i],b=el("button","copt");b.type="button";var sp=el("span",null,label);sp.dir="auto";b.appendChild(sp);
  b.addEventListener("click",function(){sendCard(c,box,{id:c.id,pick:i},label,Array.prototype.slice.call(g.children),b,null);});g.appendChild(b);});
 box.appendChild(g);}
var cardImageTokens={},cardImageViewer=null;
function cardImageUrl(id){var cached=cardImageTokens[id];
 if(cached&&cached.expires>Date.now()/1000+30)return Promise.resolve(cached.url);
 if(cached&&cached.pending)return cached.pending;
 var entry={};cardImageTokens[id]=entry;
 entry.pending=post("/api/card-media-token",{id:id}).then(function(r){if(!r.ok)throw new Error(r.status===403?S.reopen:S.cardImageError);return r.json();})
 .then(function(j){entry.url="/card-media/"+id+"?auth="+encodeURIComponent(j.token);entry.expires=j.expires;delete entry.pending;return entry.url;})
 .catch(function(e){if(cardImageTokens[id]===entry)delete cardImageTokens[id];throw e;});return entry.pending;}
function choiceTiles(c,box){var g=el("div","choice-tiles"),answers=[];
 c.options.forEach(function(o,i){var label=shown(c,"options")[i],tile=el("div","choice-tile"),preview=el("button","choice-preview");
  preview.type="button";preview.disabled=true;preview.setAttribute("aria-label",S.cardEnlarge+": "+label);
  var img=el("img");img.width=4;img.height=3;img.alt=label;img.decoding="async";img.loading="lazy";img.draggable=false;preview.appendChild(img);tile.appendChild(preview);
  var title=el("div","choice-label",label);title.dir="auto";tile.appendChild(title);
  var error=el("div","choice-image-error");error.hidden=true;error.setAttribute("role","status");tile.appendChild(error);
  function failed(msg){error.textContent=msg;error.hidden=false;preview.disabled=true;}
  img.addEventListener("load",function(){preview.disabled=false;error.hidden=true;// Portrait pictures (phone screens, posters) get the full width, one per row: in a 2-column grid they are too small to see.
  if(img.naturalHeight>img.naturalWidth*1.15)g.classList.add("tall");});preview.appendChild(el("span","choice-zoom",S.cardEnlarge));img.addEventListener("error",function(){failed(S.cardImageError);});
  cardImageUrl(c.images[i]).then(function(url){img.src=url;}).catch(function(e){failed(e.message);});
  preview.addEventListener("click",function(){cardImageUrl(c.images[i]).then(function(url){openCardImage(url,label,preview);}).catch(function(e){failed(e.message);});});
  var choose=el("button","copt",S.cardChoose);choose.type="button";choose.setAttribute("aria-label",S.cardChoose+": "+label);answers.push(choose);
  choose.addEventListener("click",function(){sendCard(c,box,{id:c.id,pick:i},label,answers,choose,null);});tile.appendChild(choose);g.appendChild(tile);
 });box.appendChild(g);}
function openCardImage(url,label,opener){
 if(!cardImageViewer){
  var dialog=el("dialog","card-image-viewer"),bar=el("div","card-image-bar"),title=el("b","card-image-label"),close=el("button","card-image-close",S.cardImageClose);
  title.id="card-image-title";title.dir="auto";dialog.setAttribute("aria-labelledby",title.id);close.type="button";bar.appendChild(title);bar.appendChild(close);dialog.appendChild(bar);
  var stage=el("div","card-image-stage"),img=el("img");img.width=4;img.height=3;img.draggable=false;stage.appendChild(img);dialog.appendChild(stage);dialog.appendChild(el("p","card-image-hint",S.cardImageHint));document.body.appendChild(dialog);
  var scale=1,x=0,y=0,points={},start=null,returnTo=null,oldOverflow="";
  function draw(){var w=stage.clientWidth,h=stage.clientHeight;
   x=Math.max(-Math.max(0,(img.clientWidth*scale-w)/2),Math.min(Math.max(0,(img.clientWidth*scale-w)/2),x));
   y=Math.max(-Math.max(0,(img.clientHeight*scale-h)/2),Math.min(Math.max(0,(img.clientHeight*scale-h)/2),y));
   img.style.transform="translate("+x+"px,"+y+"px) scale("+scale+")";}
  function reset(){scale=1;x=0;y=0;points={};start=null;draw();}
  var setOpen=bindBackLayer("card-image",dialog,false,function(on){
   if(on){if(!dialog.open){oldOverflow=document.body.style.overflow;document.body.style.overflow="hidden";dialog.showModal();close.focus();}}
   else if(dialog.open){dialog.close();document.body.style.overflow=oldOverflow;reset();if(returnTo&&returnTo.isConnected)returnTo.focus();}});
  close.addEventListener("click",function(){setOpen(false);});dialog.addEventListener("cancel",function(e){e.preventDefault();setOpen(false);});
  function baseline(){var a=Object.keys(points).map(function(k){return points[k];});
   start=a.length?{s:scale,x:x,y:y,cx:a.reduce(function(t,p){return t+p.x;},0)/a.length,cy:a.reduce(function(t,p){return t+p.y;},0)/a.length,
    d:a.length>1?Math.hypot(a[0].x-a[1].x,a[0].y-a[1].y):0}:null;}
  stage.addEventListener("pointerdown",function(e){if(e.pointerType==="mouse"&&e.button!==0)return;
   points[e.pointerId]={x:e.clientX,y:e.clientY};stage.setPointerCapture(e.pointerId);baseline();});
  stage.addEventListener("pointermove",function(e){if(!points[e.pointerId]||!start)return;e.preventDefault();points[e.pointerId]={x:e.clientX,y:e.clientY};
   var a=Object.keys(points).map(function(k){return points[k];}),cx=a.reduce(function(t,p){return t+p.x;},0)/a.length,cy=a.reduce(function(t,p){return t+p.y;},0)/a.length;
   if(a.length>1&&start.d)scale=Math.max(1,Math.min(6,start.s*Math.hypot(a[0].x-a[1].x,a[0].y-a[1].y)/start.d));
   if(scale>1){x=start.x+cx-start.cx;y=start.y+cy-start.cy;}else{x=0;y=0;}draw();});
  function end(e){delete points[e.pointerId];baseline();}
  stage.addEventListener("pointerup",end);stage.addEventListener("pointercancel",end);stage.addEventListener("lostpointercapture",end);
  stage.addEventListener("dblclick",function(){scale=scale===1?2:1;x=0;y=0;draw();});
  stage.addEventListener("wheel",function(e){e.preventDefault();scale=Math.max(1,Math.min(6,scale*(e.deltaY<0?1.15:1/1.15)));draw();},{passive:false});
  window.addEventListener("resize",draw);
  cardImageViewer={show:function(u,l,button){returnTo=button;title.textContent=l;img.alt=l;img.src=u;reset();setOpen(true);}};
 }
 cardImageViewer.show(url,label,opener);
}
function askBody(c,box){
 var f=composer("c"+c.id,300,S.answerPh,function(t,done){done(false);sendCard(c,box,{id:c.id,text:t},t,[f.sb],null,f.ta);});
 box.appendChild(f.row);}
function linkBody(c,box){
 var u=typeof c.url==="string"?c.url:"";if(!/^https:\/\//i.test(u))return;
 var row=el("div","lk"),ic=el("div","lk-i");ic.appendChild(icon("link"));row.appendChild(ic);
 var t=el("div","lk-t"),lb=el("b",null,shown(c,"label")||hostOf(u));lb.dir="auto";t.appendChild(lb);var h=el("small",null,hostOf(u));h.dir="ltr";t.appendChild(h);row.appendChild(t);
 var b=el("button","btn sm go",S.openLink);b.type="button";b.addEventListener("click",function(){haptic("tap");openSafe(u);});row.appendChild(b);box.appendChild(row);}
function numbersBody(c,box){
 var g=el("div","figs");(Array.isArray(c.figures)?c.figures:[]).slice(0,3).forEach(function(f){var x=el("div","fig");
  var v=el("b",null,shown(f,"value"));v.dir="auto";x.appendChild(v);var l=el("span",null,shown(f,"label"));l.dir="auto";x.appendChild(l);g.appendChild(x);});box.appendChild(g);}
var BODIES={progress:progressBody,choice:choiceBody,ask:askBody,link:linkBody,numbers:numbersBody};
// the session's own short id: a topic's name is the agent's to choose, this tag and the robot's colours are not
function sidTag(sid){var t=el("span","sid","#"+String(sid||"").slice(0,4));t.dir="ltr";t.title=S.sidHint;return t;}
function sessionOf(sid){return ((last&&last.sessions)||[]).filter(function(s){return s.id===sid;})[0];}
// inGroup: drawn under its session's head (sessionGroup), so the robot, topic and tag are not repeated
function agentCard(c,inGroup){
 var done=cardDone[c.id],ses=sessionOf(c.session),box=el("div","card ac k-"+c.kind+(inGroup?" in-g":"")+(c.stale?" stale":"")+(c.auto_closed?" auto-closed":"")+(ASKS[c.kind]&&!done&&!c.ended?" asks":""));
 if(ASKS[c.kind]){box.id="question-"+c.id;if(c.id===target)box.classList.add("question-highlight");}
 var head=el("div","ac-h");if(!inGroup)head.appendChild(robot(c.session,ses?ses.state:"",false));var b=el("div","ac-b");
 var k=el("div","ac-k"),kd=el("span","kd");kd.appendChild(icon(KIND_ICON[c.kind]||"box"));kd.appendChild(el("span",null,S["k_"+c.kind]||c.kind));k.appendChild(kd);
 if(c.topic&&!inGroup){var tp=el("span","tp",shown(c,"topic"));tp.dir="auto";k.appendChild(tp);}
 if(!inGroup)k.appendChild(sidTag(c.session));if(ASKS[c.kind]&&!done&&!c.ended&&isNew(c)&&!c.old)k.appendChild(el("span","badge",S.newBadge));k.appendChild(el("span",null,(c.kind==="progress"?S.cardUpdated+" ":"")+ago(c.age)));b.appendChild(k);
 if(c.stale||c.auto_closed)b.appendChild(el("div","ac-status",c.auto_closed?S.cardClosed:S.cardStale));
 var t=el("div","ac-t",unprefixed(shown(c,"title"),c));t.dir="auto";b.appendChild(t);
 if(c.detail){var dd=el("div","ac-d",shown(c,"detail"));dd.dir="auto";b.appendChild(dd);}
 source("k"+c.id,c,b);head.appendChild(b);box.appendChild(head);
 if(c.auto_closed){var dismiss=el("button","btn sm",S.cardDismiss);dismiss.type="button";dismiss.addEventListener("click",function(){dismiss.disabled=true;post("/api/card",{id:c.id,dismiss:true}).then(function(r){if(r.ok||r.status===404){if(last)last.cards=(last.cards||[]).filter(function(x){return x.id!==c.id;});var g=box.parentNode;box.remove();if(g&&/\bsgrp\b/.test(g.className)&&!g.querySelector(".ac"))g.remove();later();}else{dismiss.disabled=false;toast(S.failed);}}).catch(function(){dismiss.disabled=false;toast(S.failed);});});box.appendChild(dismiss);}
 if(done)box.appendChild(cardDoneBox(done));
 else if(!(c.ended&&ASKS[c.kind])&&BODIES[c.kind])BODIES[c.kind](c,box);
 return enter(box,"k"+c.id);}
// a card title that repeats its topic's "<project>:" prefix shows without it; display only, the card keeps its text.
// Compared after trimming and without the invisible direction marks agent text may carry.
var BIDI_MARKS=/[‎‏؜‪-‮⁦-⁩]/g;
function bare(x){return String(x==null?"":x).replace(BIDI_MARKS,"").trim();}
function prefixOf(x){var t=bare(x),i=t.indexOf(":");return i>0?t.slice(0,i).trim():"";}
function unprefixed(title,c){
 var t=bare(title),p=prefixOf(t);if(!p)return title;
 var same=[shown(c,"topic"),c.topic].some(function(x){return prefixOf(x)===p;});if(!same)return title;
 var rest=t.slice(t.indexOf(":")+1).trim();return rest||title;}
// the session's own topic in Telegram: the server's t.me link for the card's session, https only
function topicLinkOf(list,ses){
 var u=list.map(function(c){return c.topic_url;}).concat([ses&&ses.url]).filter(function(x){return typeof x==="string"&&/^https:\/\/t\.me\//i.test(x);})[0];
 return u||"";}
function tgBtn(u){var b=el("button","btn sm go sg-go");b.type="button";b.appendChild(icon("send"));b.appendChild(el("span",null,S.openTg));
 b.addEventListener("click",function(e){e.preventDefault();e.stopPropagation();haptic("tap");openLink(u);});return b;}
// the cards of one list by session: each session's newest card first, the session with the newest card first
// a session's group key: the short id alone could join two sessions, so its topic link goes with it
function sesKey(c){return String(c.session||"")+"|"+String(c.topic_url||"");}
function bySession(list){var order=[],m={};
 list.forEach(function(c){var k=sesKey(c);if(!m[k]){m[k]=[];order.push(k);}m[k].push(c);});
 var age=function(a,b){return a.age-b.age;};
 return order.map(function(k){return m[k].slice().sort(age);}).sort(function(a,b){return age(a[0],b[0]);});}
// one session block: the robot, its topic name and tag once, the way into its topic, then its cards.
// Inside a topic's own view the session card above already says all that, so the head is left out.
function sessionGroup(list,noHead){
 var c=list[0],ses=sessionOf(c.session),g=el("div","sgrp");
 if(!noHead){var h=el("div","sg-h");h.appendChild(robot(c.session,ses?ses.state:"",true));
  var n=el("div","sg-n"),name=shown(c,"topic")||(ses&&ses.name)||"";
  if(name){var tp=el("b","sg-t",name);tp.dir="auto";n.appendChild(tp);}
  n.appendChild(sidTag(c.session));h.appendChild(n);
  var u=topicLinkOf(list,ses);if(u)h.appendChild(tgBtn(u));g.appendChild(h);}
 list.forEach(function(x){g.appendChild(agentCard(x,true));});return g;}
function cardGroups(list,host,noHead){bySession(list).forEach(function(g){host.appendChild(sessionGroup(g,noHead));});}
function allCards(d,here){
 var list=(d.cards||[]).filter(function(c){return !here||c.here;}),ids={};list.forEach(function(c){ids[c.id]=1;});
 Object.keys(cardDone).forEach(function(id){var c=cardDone[id].c;if(!ids[id]&&(!here||c.here))list.push(c);});return list;}
function oldCards(d,here){return allCards(d,here).filter(function(c){return ASKS[c.kind]&&!c.ended&&c.old&&!cardDone[c.id];});}  // an answered card leaves the group (and its count)
function cardSections(d,here){
 var cs=allCards(d,here),asks=cs.filter(function(c){return ASKS[c.kind]&&!c.ended&&!c.old;}).sort(function(a,b){return a.age-b.age;}),shows=cs.filter(function(c){return !ASKS[c.kind]&&!c.ended&&!c.auto_closed&&!(c.finished&&c.old);});
 var old=cs.filter(function(c){return c.ended||c.auto_closed||c.finished&&c.old;}),waitN=asks.filter(function(c){return !cardDone[c.id];}).length;
 var asking={};asks.forEach(function(c){asking[sesKey(c)]=1;});  // a session that asks shows once, its updates with it
 var withAsk=shows.filter(function(c){return asking[sesKey(c)];});shows=shows.filter(function(c){return !asking[sesKey(c)];});
 if(asks.length){var ah=sec(S.cardsAsk,waitN||null,waitN?"hot":"");if(asks.some(isNew)){var mr=el("button","link-btn end",S.markRead);mr.type="button";mr.addEventListener("click",markSeen);ah.appendChild(mr);}cardGroups(asks.concat(withAsk),app,here);}
 if(shows.length){sec(S.cardsShow,shows.length);cardGroups(shows,app,here);}
 if(old.length){var f=fold("cards-old",S.cardsOld,old.length);f.body.appendChild(el("div","safe pad",S.cardsOldHint));
  var w=el("div","pad");cardGroups(old,w,here);f.body.appendChild(w);app.appendChild(f);}
 return cs.length;}
function waitingCount(d){var srv=typeof d.waiting_count==="number";
 var dec=(d.decisions||[]).filter(function(x){return srv?!(x.stale&&x.stale!=="old"):!x.stale&&!inOld(x);}),
  ask=(d.cards||[]).filter(function(c){return ASKS[c.kind]&&!c.ended&&(srv||!c.old);});
 var gone=dec.filter(function(x){return closed[x.id];}).length+ask.filter(function(c){return cardDone[c.id];}).length;
 return srv?Math.max(0,d.waiting_count-gone):dec.length+ask.length-gone;}
// --- the hero: what waits for the owner, as a ring that drains as he answers --------------------------------
function hero(d){
 var n=waitingCount(d),c=el("div","card hero "+(n?"hot":"calm")),tx=el("div","htx"),t=el("div","h-t");
 if(n){t.appendChild(el("b",null,nf.format(n)));t.appendChild(document.createTextNode(" "+plural(n,S.heroOne,S.heroMany)));}else t.textContent=S.allClear;
 tx.appendChild(t);tx.appendChild(el("div","h-s",n?S.heroHint:S.allClearHint));c.appendChild(tx);
 var rg=el("div","ring"),s=document.createElementNS(NS,"svg");s.setAttribute("viewBox","0 0 48 48");s.setAttribute("aria-hidden","true");
 var C=2*Math.PI*20;svgAdd(s,"circle",{cx:24,cy:24,r:20,"class":"rg-t"});
 var a=svgAdd(s,"circle",{cx:24,cy:24,r:20,"class":"rg-a","stroke-dasharray":C.toFixed(2),transform:"rotate(-90 24 24)"});rg.appendChild(s);
 var frac=n?n/(n+handled):1;anim("ring",frac,function(v){a.style.strokeDashoffset=(C*(1-v)).toFixed(2);});
 var mid=el("div","mid");if(n)mid.textContent=nf.format(n);else mid.appendChild(icon("check"));rg.appendChild(mid);c.appendChild(rg);
 return enter(c,"hero");}
// a button that acts on the second tap (the first one arms it for a few seconds): closing and clearing in bulk
function sureBtn(label,run){var b=el("button","btn sm",label),armed=false,tm=null;b.type="button";
 var rest=function(){armed=false;b.disabled=false;b.textContent=label;b.className="btn sm";};
 b.addEventListener("click",function(e){e.preventDefault();e.stopPropagation();
  if(!armed){armed=true;haptic("tap");b.textContent=S.sure;b.className="btn sm arm";clearTimeout(tm);tm=setTimeout(rest,4000);return;}
  clearTimeout(tm);b.disabled=true;run(rest);});return b;}
function sendJson(path,body,ok,rest){post(path,body).then(function(r){if(!r.ok)throw r.status;return r.json();})
 .then(function(j){haptic("n","success");ok(j);load();})
 .catch(function(st){haptic("n","error");if(rest)rest();toast(st===403?S.reopen:st===503?S.busy:S.failed,true);});}
function closeBtn(ids,label){return sureBtn(label,function(rest){sendJson("/api/close",{sessions:ids},function(j){
 if(j.closed)toast(S.closedN+": "+nf.format(j.closed));else{rest();toast(S.notClosed,true);}},rest);});}
function clearRem(refs,done){sendReminderAction(refs,"clear",done);}
function tile(n,label,cls){var t=el("div","tile"+(cls&&n?" "+cls:""));t.appendChild(el("b",null,nf.format(n)));t.appendChild(el("span",null,label));return t;}
function groups(d){var by={working:[],idle:[],ended:[],left:[]};
 (d.sessions||[]).forEach(function(s){(s.left&&s.state!=="working"?by.left:by[s.state]||by.ended).push(s);});
 by.idle.sort(function(a,b){return (a.age==null?1e12:a.age)-(b.age==null?1e12:b.age);});
 by.recent=by.idle.filter(function(s){return s.age!=null&&s.age<RECENT;});by.rest=by.idle.filter(function(s){return by.recent.indexOf(s)<0;});return by;}
// --- sessions -----------------------------------------------------------------------------------------------
function sessionMeta(s,d){var m=[];if(s.model)m.push([iso(s.model),"mdl"]);
 if(s.harness==="codex"){if(s.effort)m.push(iso(s.effort));if(typeof s.codex_tokens==="number")m.push(S.ctx+" "+kt(s.codex_tokens));}
 if(s.credit)m.push(s.credit);
 if(s.context){var hot=s.context>=400000||(d.compact_at&&s.context>=d.compact_at*800);m.push([S.ctx+" "+kt(s.context),hot?"hot":"",d.compact_at?s.context/(d.compact_at*10):null]);}
 if(s.bg)m.push(nf.format(s.bg)+" "+plural(s.bg,S.bg1,S.bg));
 m.push(ago(s.age));return m;}
function activityFeed(s,host){var a=s.activity||{},steps=a.steps||[],agents=a.agents||[];if(!steps.length&&!agents.length)return;
 var box=el("div","reply activity-feed"),title=el("div","lbl",S.liveActivity);box.appendChild(title);
 steps.slice(-8).forEach(function(raw){var text=String(raw).replace(/<[^>]*>/g,"").replace(/&lt;/g,"<").replace(/&gt;/g,">").replace(/&amp;/g,"&");var row=el("div","act",text);row.dir="auto";box.appendChild(row);});
 agents.slice(-6).forEach(function(x){var icon=x.status==="done"?"✅":x.status==="failed"?"❌":"⏳";var label=x.task||x.type||x.name||S.activityAgent;box.appendChild(el("div","act",icon+" "+label));});host.appendChild(box);}
// Words the owner wrote here that the agent has not read yet: each can still be taken back or edited.
function unsay(s,w,edit,btns){
 btns.forEach(function(b){b.disabled=true;});haptic("tap");
 post("/api/unsay",{session:s.id,key:w.key}).then(function(r){if(r.status===409)throw 409;if(!r.ok)throw r.status;return r.json();})
  .then(function(j){haptic("n","success");s.waiting=(s.waiting||[]).filter(function(x){return x.key!==w.key;});
   if(edit){var k="w"+s.id;notes[k]=(notes[k]?notes[k]+"\n":"")+(j.text||w.text);focusSay=s.id;}else toast(S.unsaid);
   render(last,true);syncClose();})
  .catch(function(st){haptic("n",st===409?"warning":"error");
   if(st===409){s.waiting=(s.waiting||[]).filter(function(x){return x.key!==w.key;});toast(S.unsayLate,true);render(last,true);}
   else{btns.forEach(function(b){b.disabled=false;});toast(st===403?S.reopen:S.failed,true);}});}
function waitingBubble(s,w){
 var b=el("div","wait"),t=el("div","wait-t",w.text);t.dir="auto";b.appendChild(t);
 var f=el("div","wait-f"),m=el("span","wait-m");m.appendChild(icon("clock"));m.appendChild(el("span",null,deliveryLabel(w.state||"queued")));f.appendChild(m);
 if(w.retrying)b.appendChild(el("div","err",S.deliveryRetry));
 if(w.state==="in_flight"){b.appendChild(f);return b;}
 var ed=el("button","wait-b");ed.type="button";ed.appendChild(icon("pencil"));ed.appendChild(el("span",null,S.editIt));
 var cx=el("button","wait-b del");cx.type="button";cx.appendChild(icon("x"));cx.appendChild(el("span",null,S.cancelIt));
 ed.addEventListener("click",function(){unsay(s,w,true,[ed,cx]);});cx.addEventListener("click",function(){unsay(s,w,false,[ed,cx]);});
 f.appendChild(ed);f.appendChild(cx);b.appendChild(f);return b;}
var focusSay=null;
// one id per send, kept for every retry of the same words: the server queues an id once, so a retry after a lost
// answer never reaches the session as a second message (a second "yes" could approve its next question)
var sends={};
function sendId(k,t){var c=sends[k];if(c&&c.text===t)return c.id;var a=new Uint8Array(12),id="";
 try{crypto.getRandomValues(a);}catch(e){for(var i=0;i<a.length;i++)a[i]=Math.floor(Math.random()*256);}
 for(var n=0;n<a.length;n++)id+=("0"+a[n].toString(16)).slice(-2);sends[k]={text:t,id:id};return id;}
function sayRow(s){
 var box=el("div","saybox");(s.waiting||[]).forEach(function(w){box.appendChild(waitingBubble(s,w));});
 var c=composer("w"+s.id,600,S.sayPh,function(t,done){
  post("/api/say",{session:s.id,text:t,send:sendId("w"+s.id,t)}).then(function(x){if(!x.ok)return x.json().then(function(j){throw j.error==="queue full"?"queue full":x.status;});return x.json();})
   .then(function(j){haptic("n","success");delete sends["w"+s.id];done(true);toast(deliveryLabel(j.delivery));
    if(j&&j.key&&j.delivery!=="delivered"){s.waiting=(s.waiting||[]).concat([{key:j.key,text:t,age:0,state:j.delivery}]);render(last,true);}})
   .catch(function(st){haptic("n","error");done(false);toast(st==="queue full"?S.queueFull:st===404||st===409?S.sayGone:st===403?S.reopen:S.failed,true);});});
 box.appendChild(c.row);
 if(focusSay===s.id){focusSay=null;setTimeout(function(){c.ta.focus();var n=c.ta.value.length;try{c.ta.setSelectionRange(n,n);}catch(e){}},60);}
 return box;}
// a folder or a branch: left-to-right, its end kept visible (the end of a path is what tells folders apart)
function pathRow(label,value){var r=kv(label,""),v=r.lastChild,full=String(value),parts=full.split("/");
 var short=parts.length>3?"…/"+parts.slice(-2).join("/"):full;
 v.textContent=short;v.dir="ltr";v.className+=" path";v.title=full;
 r.addEventListener("click",function(){v.textContent=v.textContent===short?full:short;haptic("sel");});return r;}
// the session's real terminal screen, read-only: fetched when opened and every few seconds while it stays open
var screens={},screenTimer=null;
function screenFetch(id){post("/api/screen",{session:id}).then(function(r){if(!r.ok)throw r.status;return r.json();})
 .then(function(j){screens[id]=j.text||"";var p=document.getElementById("scr"+id);if(p){var end=p.scrollTop+p.clientHeight>=p.scrollHeight-8;
   p.textContent=screens[id]||S.screenNone;if(end)p.scrollTop=p.scrollHeight;}})
 .catch(function(st){if(st===403)toast(S.reopen,true);});}
function screenTick(){var ids=Object.keys(opened).filter(function(k){return k.indexOf("scr")===0&&opened[k]&&document.getElementById(k);});
 if(!ids.length){clearInterval(screenTimer);screenTimer=null;return;}
 if(!document.hidden)ids.forEach(function(k){screenFetch(k.slice(3));});}
function screenRow(s,host){
 var key="scr"+s.id,b=el("button","link-btn",opened[key]?S.screenHide:S.screen);b.type="button";host.appendChild(b);
 var show=function(){var p=el("pre","scr",screens[s.id]||"…");p.id=key;host.insertBefore(p,b.nextSibling);p.scrollTop=p.scrollHeight;
  screenFetch(s.id);if(!screenTimer)screenTimer=setInterval(screenTick,3000);};
 if(opened[key])show();
 var setScreen=bindBackLayer("screen:"+s.id,b,opened[key],function(on){opened[key]=on;b.textContent=on?S.screenHide:S.screen;
  var p=document.getElementById(key);if(on&&!p)show();else if(!on&&p)p.remove();});
 b.addEventListener("click",function(){haptic("sel");setScreen(!opened[key]);});}
// one session's own automatic-compaction threshold: a tap moves to the next choice (global, never, then the steps)
var COMPACT_STEPS=[null,0,200,250,300,400,600];
function compactOf(s,d){return s.compact==null?d.compact_at:s.compact;}
function compactLabel(s,d){if(s.compact==null)return S.compactGlobal+" ("+(d.compact_at?cf.format(d.compact_at*1000):S.compactOff)+")";
 return s.compact?cf.format(s.compact*1000):S.compactNever;}
function compactRow(s,d){var r=kv(S.compactOwn,""),v=r.lastChild,b=el("button","link-btn",compactLabel(s,d));b.type="button";v.appendChild(b);
 b.addEventListener("click",function(){var i=COMPACT_STEPS.indexOf(s.compact==null?null:s.compact),next=COMPACT_STEPS[(i+1)%COMPACT_STEPS.length];
  haptic("sel");b.disabled=true;
  post("/api/compact",{session:s.id,limit:next}).then(function(x){if(!x.ok)throw x.status;return x.json();})
   .then(function(){s.compact=next;render(last,true);})
   .catch(function(st){b.disabled=false;haptic("n","error");toast(st===403?S.reopen:S.failed,true);});});
 return r;}
var usd=new Intl.NumberFormat(LANG,{style:"currency",currency:"USD",minimumFractionDigits:2,maximumFractionDigits:4});
function cost(u){return u.estimate_usd==null?S.usageUnknown:usd.format(u.estimate_usd);}
function shortUsageModel(m){var x=/^claude-(fable|opus|sonnet|haiku)-(\d{1,2})(?:-(\d{1,2}))?(?:-\d{8})?$/.exec(m);return x?iso(x[1][0].toUpperCase()+x[1].slice(1)+" "+x[2]+(x[3]?"."+x[3]:"")):S.usageModelUnknown;}
function usageTokens(v){return v.tokens!=null?v.tokens:["input_tokens","output_tokens","cache_creation_input_tokens","cache_read_input_tokens"].reduce(function(n,k){return n+(v[k]||0);},0);}
function cacheShare(v){var tokens=usageTokens(v);return tokens?Math.max(0,Math.min(1,((v.cache_creation_input_tokens||0)+(v.cache_read_input_tokens||0))/tokens)):0;}
function usageFigures(v,parentTokens){var figures=el("span","compact-figures"),price=el("span","compact-figure cost",cost(v));
 price.title=S.usageCost;price.setAttribute("aria-label",S.usageCost+": "+cost(v));figures.appendChild(price);
 var tokens=el("span","compact-figure",cf.format(usageTokens(v)));tokens.title=S.usageTokens+": "+nf.format(usageTokens(v));tokens.setAttribute("aria-label",tokens.title);figures.appendChild(tokens);
 var share=cacheShare(v),cache=el("span","compact-figure cache"),ring=el("i","cache-ring");ring.style.setProperty("--share",(share*100)+"%");ring.setAttribute("aria-hidden","true");
 cache.title=S.usageShare;cache.setAttribute("aria-label",S.usageShare+": "+pf.format(share));cache.appendChild(ring);cache.appendChild(el("span",null,pf.format(share)));figures.appendChild(cache);
 if(parentTokens){var mini=el("span","compact-mini"),fill=el("i");mini.setAttribute("aria-hidden","true");fill.style.inlineSize=Math.min(100,100*usageTokens(v)/parentTokens)+"%";mini.appendChild(fill);figures.appendChild(mini);}
 return figures;}
function usageLine(u){return S.usageToday+": "+nf.format(u.tokens)+" "+S.usageTokens+" · "+S.usageCost+": "+cost(u)+(u.unknown_models?" · "+S.usageUnknown:"");}
function usageRows(v,host){
 [[S.usageTokens,usageTokens(v)],[S.usageInput,v.input_tokens],[S.usageOutput,v.output_tokens],[S.usageWrite,v.cache_creation_input_tokens],[S.usageRead,v.cache_read_input_tokens],[S.usageUnpriced,v.unpriced_tokens]].forEach(function(x){if(x[1]>0)host.appendChild(kv(x[0],nf.format(x[1])));});
 if(v.estimate_usd>0||v.estimate_usd==null&&usageTokens(v)>0)host.appendChild(kv(S.usageCost,cost(v)));
 if(cacheShare(v)>0)host.appendChild(kv(S.usageShare,pf.format(cacheShare(v))));}
function usageActive(v){return !!v&&(usageTokens(v)>0||v.estimate_usd>0||v.unpriced_tokens>0);}
function usageBreakdown(periods,host,key){var models=[];
 periods.forEach(function(pair){var v=pair[1];if(!v)return;host.appendChild(el("div","lbl",pair[0]));usageRows(v,host);
  Object.keys(v.models||{}).forEach(function(m){if(usageActive(v.models[m])&&models.indexOf(m)<0)models.push(m);});});
 // One disclosure per model, after the totals; compare periods inside instead of repeating groups.
 models.forEach(function(m){var body=el("div"),summary=null,parentTokens=0;
  periods.forEach(function(pair){var v=pair[1],model=v&&v.models&&v.models[m];if(!usageActive(model))return;
   summary=model;parentTokens=usageTokens(v);body.appendChild(el("div","lbl",pair[0]));usageRows(model,body);});
  host.appendChild(compactBlock(key+"-model-"+m,shortUsageModel(m),usageFigures(summary,parentTokens),body,"usage-model usage-summary"));});}
function usageDetails(u,host,key){if(!u)return;var body=el("div");key="usage-"+key;
 usageBreakdown([[S.usageToday,u.today],[S.usageSession,u.session]],body,key);
 body.appendChild(el("div","safe",S.usageHint));host.appendChild(compactBlock(key,S.usageSession,usageFigures(u.session||u.today),body,"usage-summary"));}
function usageCard(t){if(!t)return;sec(S.usageTitle);var body=el("div");
 usageBreakdown([[S.usageToday,t.today],[S.usageWeek,t.week]],body,"usage-all");
 body.appendChild(el("div","safe",S.usageHint));
 var top=el("div");(t.top||[]).forEach(function(x){var row=kv("",nf.format(x.today.tokens));row.firstChild.textContent=shown(x,"name");row.firstChild.dir="auto";top.appendChild(row);});
 if((t.top||[]).length){if(t.top.length<=4)body.appendChild(el("div","lbl",S.usageTop));compactLong(body,"usage-top",S.usageTop,top,t.top.length);}
 app.appendChild(compactBlock("usage-all",S.usageToday,usageFigures(t.today),body,"usage-summary"));}

function forkRow(s){
 var box=el("div","safe");
 function draw(){box.textContent="";var armed=forkArmed[s.id];
  if(armed)box.appendChild(el("div",null,S.forkConfirm));
  var row=el("div","btns"),go=el("button","btn sm",S.forkAction);go.type="button";go.disabled=!!forkBusy[s.id];
  go.addEventListener("click",function(e){e.preventDefault();e.stopPropagation();
   if(forkBusy[s.id])return;forkBusy[s.id]=true;draw();
   var body=armed?{action:"start",session:s.id,armed:armed}:{action:"arm",session:s.id};
   post("/api/fork",body).then(function(r){return r.json().then(function(j){
    if(!r.ok)throw {status:r.status,message:j.message};
    if(armed){delete forkArmed[s.id];toast(S.forkStarting);}else forkArmed[s.id]=j.armed;
   });}).catch(function(e){delete forkArmed[s.id];toast(e.status===403?S.reopen:e.message||S.failed,true);})
   .then(function(){delete forkBusy[s.id];draw();});});row.appendChild(go);
  if(armed){var no=el("button","btn sm",S.cancelIt);no.type="button";no.disabled=!!forkBusy[s.id];
   no.addEventListener("click",function(e){e.preventDefault();e.stopPropagation();delete forkArmed[s.id];draw();});row.appendChild(no);}
  box.appendChild(row);}
 draw();return box;}
function codexPill(s){
 var he=LANG.startsWith("he");
 if(s.native_status==="waiting_for_approval")return he?"ממתין לאישור":"Waiting for approval";
 if(s.native_status==="unavailable")return he?"לא מחובר":"Disconnected";
 return S[s.state]||s.state;}
function codexDetails(s,host){
 var r=el("div","btns"),he=LANG.startsWith("he");
 if(s.delivery_unconfirmed)host.appendChild(el("div","safe",he?"מסירת תשובה לא אומתה":"Reply delivery unconfirmed"));
 if(s.url)r.appendChild(topicBtn(s.url));
 [[s.stop_url,he?"פתיחת כפתור העצירה":"Open interrupt control"],
  [s.answer_url,he?"פתיחת בקשת האישור":"Open request card"]].forEach(function(a){
  if(!a[0])return;var b=el("button","link-btn go",a[1]);b.type="button";
  b.addEventListener("click",function(){openLink(a[0]);});r.appendChild(b);});
 if(r.children.length)host.appendChild(r);}
function details(s,d,host,skipFork){
 if(s.harness==="codex"){codexDetails(s,host);return;}
 if(!skipFork&&s.forkable&&s.state!=="ended")host.appendChild(forkRow(s));
 activityFeed(s,host);
 usageDetails(s.usage,host,s.id);
 var meta=el("div");
 if(s.model)meta.appendChild(kv(S.model,iso(s.model)));
 meta.appendChild(kv(S.ctx,kt(s.context)));
 var at=compactOf(s,d);if(at&&s.context){meta.appendChild(bar(s.context/(at*10)));
  meta.appendChild(el("div","safe",S.compactAt+cf.format(at*1000)));}
 if(s.state!=="ended")meta.appendChild(compactRow(s,d));
 meta.appendChild(kv(S.bg,s.bg?nf.format(s.bg):S.bgNone));
 if(s.account)meta.appendChild(kv(S.account,iso(s.account)));meta.appendChild(kv(S.last,ago(s.age)));
 if(s.folder)meta.appendChild(pathRow(S.folder,s.folder));if(s.branch)meta.appendChild(pathRow(S.branch,s.branch));
 host.appendChild(compactBlock("session-meta-"+s.id,S.compactDetails,s.model?iso(s.model):kt(s.context),meta));
 if(s.summary){var w=el("div","reply");w.appendChild(el("div","lbl",S.lastReply));var t=el("div",null,shown(s,"summary"));t.dir="auto";w.appendChild(t);
  source("m"+s.id,[[s.summary,he(s,"summary")]],w);compactLong(host,"session-reply-"+s.id,S.lastReply,w,Math.max(s.summary.split("\n").length,Math.ceil(s.summary.length/65)));}
 source("n"+s.id,[[s.name,he(s,"name")],[s.now,he(s,"now")]],host);
 if(s.state!=="ended")host.appendChild(sayRow(s));
 if(s.state!=="ended")screenRow(s,host);
 if(s.url||s.left){var r=el("div","btns");if(s.url)r.appendChild(topicBtn(s.url));if(s.left)r.appendChild(closeBtn([s.id],S.closeTopic));host.appendChild(r);}}
function sessionRow(s,d,lead){
 var det=el("details","srow");det.setAttribute("data-key","session:"+s.id);var key=compactStorageKey("session-"+s.id);det.open=!!opened["s"+s.id];
 try{det.open=det.open||localStorage.getItem(key)==="1";}catch(e){}
 var setDetails=bindBackLayer("session:"+s.id,det,det.open,function(on){opened["s"+s.id]=on;det.open=on;
  try{localStorage.setItem(key,on?"1":"0");}catch(e){}});
 det.addEventListener("toggle",function(){if(opened["s"+s.id]===det.open)return;setDetails(det.open);adoptBackLayers();});
 opened["s"+s.id]=det.open;
 var r=el("summary","row");r.appendChild(robot(s.id,s.state,false));var b=el("div","body");
 var n=el("div","nm",shown(s,"name"));n.dir="auto";b.appendChild(n);
 if(lead==="now"&&s.now)b.appendChild(line("act","bolt",shown(s,"now")));
 else if(lead!=="none"&&s.summary)b.appendChild(line("act sum","chat",shown(s,"summary")));
 var mt=el("div","mt");
 sessionMeta(s,d).forEach(function(x){var a=Array.isArray(x),c=el("span","chip"+(a&&x[1]?" "+x[1]:""));
  if(a&&x[2]!=null){c.className+=" cm";c.appendChild(bar(x[2]));}c.appendChild(el("span",null,a?x[0]:x));mt.appendChild(c);});
 mt.appendChild(sidTag(s.id));b.appendChild(mt);if(s.usage)b.appendChild(el("div","safe",usageLine(s.usage.today)));
 if(s.forkable&&s.state!=="ended")b.appendChild(forkRow(s));
 r.appendChild(b);r.appendChild(el("span","pill "+s.state,s.harness==="codex"?codexPill(s):S[s.state]||s.state));det.appendChild(r);
 var more=el("div","more");details(s,d,more,true);det.appendChild(more);return det;}
function sessionCard(s,d){
 var c=el("div","card");c.setAttribute("data-key","session:"+s.id);var h=el("div","s-head");h.appendChild(robot(s.id,s.state,false));
 var n=el("div","nm",shown(s,"name"));n.dir="auto";h.appendChild(n);h.appendChild(el("span","pill "+s.state,s.harness==="codex"?codexPill(s):S[s.state]||s.state));c.appendChild(h);
 if(s.now)c.appendChild(line("act","bolt",shown(s,"now")));details(s,d,c);return c;}
function item(ic,lead,x,f,meta,cls,key){
 var li=el("div","row"+(cls?" "+cls:""));li.setAttribute("data-key",String(key));var l=el("div","lead"+(lead?" "+lead:""));l.appendChild(icon(ic));li.appendChild(l);
 var b=el("div","body");var n=el("div","nm",shown(x,f));n.dir="auto";b.appendChild(n);
 if(meta&&meta.length){var m=el("div","mt");meta.forEach(function(t){var s=el("span",null,t);s.dir="auto";m.appendChild(s);});b.appendChild(m);}
 source(key,[[x[f],he(x,f)]],b);li.appendChild(b);
 if(x.url){li.className+=" tap";li.appendChild(icon("chev"));li.addEventListener("click",function(e){
  if(e.target.closest&&e.target.closest(".src-t,.orig,.rdone"))return;haptic("tap");openLink(x.url);});}
 return li;}
// --- the topic's own view -------------------------------------------------------------------------------------
function remRow(r,k){var m=[];if(r.due)m.push(iso(r.due));if(r.topic)m.push(shown(r,"topic"));m.push(ago(r.age));
 var li=item("clock","warm",r,"text",m,"","r"+k+r.text);
 if(r.ref){var db=el("button","rdone");db.type="button";db.setAttribute("aria-label",S.doneRem);db.appendChild(icon("check"));
  db.addEventListener("click",function(e){e.preventDefault();e.stopPropagation();haptic("tap");db.disabled=true;
   clearRem([r.ref],function(){db.disabled=false;});});li.insertBefore(db,li.querySelector(".body").nextSibling);}
 if(r.ref&&r.old)reminderRestore(li,r);
 return li;}
function sendReminderAction(refs,action,rest){
 post("/api/remind",action==="clear"?{refs:refs}:{refs:refs,action:action}).then(function(r){if(!r.ok)throw r.status;return r.json();})
 .then(function(j){var n=action==="restore"?j.restored:j.cleared;if(!n){if(rest)rest();toast(S.failed,true);load();return;}
  haptic("n","success");toast(action==="restore"?S.remRestored:refs.length>1?S.cleared+": "+nf.format(n):S.remCleared);load();})
 .catch(function(st){if(rest)rest();toast(st===403?S.reopen:st===503?S.busy:S.failed,true);});}
function reminderRestore(li,r){
 var actions=el("div","rem-actions"),clear=li.querySelector(".rdone"),b=el("button","rdone");
 if(clear){li.insertBefore(actions,clear);actions.appendChild(clear);}else li.appendChild(actions);
 b.type="button";b.setAttribute("aria-label",S.restoreRem);b.title=S.restoreRem;b.appendChild(icon("clock"));
 b.addEventListener("click",function(e){e.preventDefault();e.stopPropagation();b.disabled=true;
  sendReminderAction([r.ref],"restore",function(){b.disabled=false;});});actions.appendChild(b);}
function renderBriefReminders(d){
 var rows=d.reminders||[],fresh=rows.filter(function(r){return !r.old;}),aged=rows.filter(function(r){return r.old;});
 if(fresh.length){sec(S.reminders,fresh.length);var group=el("div","card group");
  fresh.forEach(function(r,k){group.appendChild(remRow(r,"brief"+k));});app.appendChild(group);}
 oldGroup([],[],aged,true);}
// everything that waited for the owner over three days (the server marks it "old"): ONE collapsed group, closed by default,
// its state remembered per viewer. Inside, decisions and cards work exactly as above. Returns the number of items it holds.
function oldGroup(decs,cards,rems,showTopic){
 decs=decs.filter(function(x){return !closed[x.id];});
 var n=decs.length+cards.length+rems.length;if(!n)return 0;
 var det=compactBlock("old",S.oldList,nf.format(n),null,"oldg",decs.concat(cards).some(function(x){return x.id===target;}));
 var body=el("div","oldbody");body.appendChild(el("div","safe",S.oldHint));
 decs.forEach(function(x){body.appendChild(decision(x,showTopic));});
 cardGroups(cards,body,false);
 if(rems.length){var g=el("div","card group");rems.forEach(function(r,k){g.appendChild(remRow(r,"o"+k));});body.appendChild(g);}
 var ids=decs.filter(function(x){return x.stale==="old";}).map(function(x){return x.id;}),
  refs=rems.map(function(r){return r.ref;}).filter(Boolean);
 if(ids.length||refs.length){var row=el("div","btns");row.appendChild(sureBtn(S.clearOld,function(rest){clearOld(ids,refs,rest);}));body.appendChild(row);}
 det.body.appendChild(body);app.appendChild(det);return n;}
function clearOld(ids,refs,rest){
 function ok(r){if(!r.ok)throw r.status;return r.json();}
 var jobs=[];if(ids.length)jobs.push(post("/api/dismiss",{ids:ids}).then(ok));if(refs.length)jobs.push(post("/api/remind",{refs:refs}).then(ok));
 Promise.all(jobs).then(function(rs){var done=0;rs.forEach(function(j){(j.closed||[]).forEach(function(id){closed[id]=1;});done+=(j.closed||[]).length+(j.cleared||0);});
  haptic("n","success");toast(S.cleared+": "+nf.format(done));load();})
 .catch(function(st){haptic("n","error");rest();toast(st===403?S.reopen:st===503?S.busy:S.failed,true);});}
function renderHere(d){
 var tp=d.topic,b=tp.board,me=(d.sessions||[]).filter(function(s){return s.here;})[0];
 var any=0,mine=(d.decisions||[]).filter(function(x){return x.here;});
 var statusCards=tp.status_cards||[];
 if(statusCards.length){sec(S.topicStatusHeading,statusCards.length);var sg=el("div","card group");
  statusCards.forEach(function(x){var r=el("div","row"),body=el("div","body"),n=el("div","nm",x.title);n.dir="auto";body.appendChild(n);
   if(x.note){var note=el("div","safe",x.note);note.dir="auto";body.appendChild(note);}r.appendChild(body);
   r.appendChild(el("span","chip topic-status "+x.status,x.status_label+" · "+ago(x.idle)));sg.appendChild(r);});app.appendChild(sg);any+=statusCards.length;}
 if(mine.filter(function(x){return !closed[x.id]&&!inOld(x);}).length||answeredIds(false).length){sec(S.waiting,null,"hot");any+=decisions(mine,false);}
 sec(S.session);
 if(me){app.appendChild(sessionCard(me,d));any++;}else app.appendChild(empty("cpu",S.noSession));
 any+=cardSections(d,true);
 any+=oldGroup(mine.filter(inOld),oldCards(d,true),[],false);
 [["remind",S.reminders,"clock","warm"],["check",S.checks,"box","blue"],["link",S.links,"link","blue"]].forEach(function(z){
  var list=b[z[0]]||[];if(!list.length)return;any+=list.length;
  sec(z[1],list.length);var c=el("div","card group");
  list.forEach(function(i,k){var key=z[0]+k+i.text;
   if(z[0]==="remind")c.appendChild(remRow(i,key));
   else if(z[0]==="check")c.appendChild(item(i.done?"check":"box",i.done?"ok":"blue",i,"text",[],i.done?"ticked":"",key));
   else{var a=item("link","blue",i,"text",[],"",key);var link=el("a",a.className);link.href=i.url;link.rel="noopener";
    while(a.firstChild)link.appendChild(a.firstChild);
    link.addEventListener("click",function(e){e.preventDefault();if(e.target.closest&&e.target.closest(".src-t,.orig"))return;haptic("tap");openLink(i.url);});c.appendChild(link);}});
  compactLong(app,"topic-"+tp.name+"-"+z[0],z[1],c,list.length);});
 if(!any){app.appendChild(empty("inbox",S.emptyHere,S.emptyHereHint,true));
  var go=el("button","link-btn",S.goAll);go.type="button";go.addEventListener("click",function(){switchTo("me");});app.appendChild(go);}
}
function digestCard(){
 if(!digest||digestOff||!digest.items.length)return;
 var c=el("div","card digest"),h=el("div","hd");h.appendChild(icon("bolt"));h.appendChild(el("span",null,S.digest));
 h.appendChild(el("span","when",ago(digest.age)));var x=el("button","link-btn",S.gotIt);x.type="button";
 x.addEventListener("click",function(){digestOff=true;haptic("sel");c.remove();});h.appendChild(x);c.appendChild(h);
 var ch=el("div","chips");digest.items.forEach(function(it){var t=el("span","c");t.appendChild(el("b",null,nf.format(it[0])));
  t.appendChild(document.createTextNode(plural(it[0],it[1],it[2])));ch.appendChild(t);});c.appendChild(ch);app.appendChild(enter(c,"digest"));}
// --- "For me": what needs the owner first, then what the agents show, then who works ------------------------
var topicLimit=8,topicViewer=null;
function topicText(t,field){return LANG==="he"?shown(t,field):t[field];}
function topicField(host,label,text,cls){var field=el("span","topic-field"+(cls?" "+cls:""));
 field.appendChild(el("b",null,label));var copy=el("span","topic-copy",text);copy.dir="auto";field.appendChild(copy);host.appendChild(field);}
function topicPRs(host,t){if(!(t.prs||[]).length)return;var prs=el("span","topic-prs");prs.setAttribute("aria-label",S.topicPRs);
 t.prs.forEach(function(pr){var ref=el("span",null,"#"+pr.number+(S["topicPR_"+pr.state]?" · "+S["topicPR_"+pr.state]:""));ref.dir="auto";
  if(!pr.state)ref.title=S.topicPRUnknown;prs.appendChild(ref);});host.appendChild(prs);}
function topicCard(t){var card=el("button","topic-card"),head=el("span","topic-heading"),title=el("b","topic-title",topicText(t,"title"));
 card.type="button";card.setAttribute("aria-haspopup","dialog");card.setAttribute("data-topic-key",JSON.stringify(t.key));title.dir="auto";head.appendChild(title);
 var stage=S["topicStage_"+t.stage]||S.topicStage_planning,percent=Number.isFinite(t.percent)?Math.max(0,Math.min(100,t.percent)):10;
 var progress=el("span","topic-progress"),meter=el("span","topic-meter"),fill=el("span","topic-meter-fill");
 meter.setAttribute("role","progressbar");meter.setAttribute("aria-label",stage);meter.setAttribute("aria-valuemin","0");
 meter.setAttribute("aria-valuemax","100");meter.setAttribute("aria-valuenow",String(percent));meter.setAttribute("aria-valuetext",stage+" · "+percent+"%");
 fill.style.inlineSize=percent+"%";meter.appendChild(fill);progress.appendChild(meter);progress.appendChild(el("span","topic-stage",stage));head.appendChild(progress);
 head.appendChild(el("span","chip topic-status "+t.state,S["topic_"+t.state]));card.appendChild(head);
 var project=el("span","topic-project",t.project);project.dir="auto";card.appendChild(project);
 topicField(card,S.topicDecision,topicText(t,"decision")||S.topicNoDecision,"topic-decision");
 topicField(card,topicText(t,"next")&&!t.next_explicit?S.topicLatest:S.topicNext,topicText(t,"next")||S.topicNoNext,"topic-next");topicPRs(card,t);
 card.addEventListener("click",function(){openTopicDetails(t,card);});return card;}
function openTopicDetails(t,opener){
 if(topicViewer){topicViewer.close();topicViewer=null;}
 var dialog=el("dialog","topic-sheet"),head=el("div","topic-heading"),title=el("h2",null,topicText(t,"title")),close=el("button","btn topic-close",S.topicClose);
 dialog.dir=document.documentElement.dir;title.id="topic-detail-title";title.dir="auto";dialog.setAttribute("aria-labelledby",title.id);
 close.type="button";head.appendChild(title);head.appendChild(close);dialog.appendChild(head);
 dialog.appendChild(el("span","chip topic-status "+t.state,S["topic_"+t.state]));
 topicField(dialog,S.topicDecision,topicText(t,"decision")||S.topicNoDecision);
 topicField(dialog,topicText(t,"next")&&!t.next_explicit?S.topicLatest:S.topicNext,topicText(t,"next")||S.topicNoNext);topicPRs(dialog,t);
 dialog.appendChild(el("h3",null,S.topicTimeline));var timeline=el("ol","topic-timeline"),status=el("p","topic-loading",V2.requests.loading);dialog.appendChild(status);
 // The card carries no notes: the timeline is read when the sheet opens.
 post("/api/topic-detail",{key:t.key}).then(function(res){if(!res.ok)throw res.status;return res.json();}).then(function(j){
  (j.notes||[]).forEach(function(note){var li=el("li"),date=note.at?new Date(note.at*1000):null;if(date&&Number.isFinite(date.getTime())){
    var at=el("time",null,new Intl.DateTimeFormat(LANG,{dateStyle:"medium",timeStyle:"short"}).format(date));at.dateTime=date.toISOString();li.appendChild(at);}
   var text=el("span","topic-copy",topicText(note,"text"));text.dir="auto";li.appendChild(text);timeline.appendChild(li);});
  if(timeline.children.length)status.replaceWith(timeline);else status.textContent=S.topicNoNotes;
 }).catch(function(){status.textContent=V2.requests.loadFailed;});
 document.body.appendChild(dialog);var oldOverflow=document.body.style.overflow;
 var setOpen=bindBackLayer("topic-details",dialog,false,function(on){
  if(on){if(!dialog.open){document.body.style.overflow="hidden";dialog.showModal();close.focus();}}
  else{if(dialog.open)dialog.close();document.body.style.overflow=oldOverflow;
   var returnTo=opener&&opener.isConnected?opener:Array.from(document.querySelectorAll(".topic-card")).find(function(card){return card.getAttribute("data-topic-key")===JSON.stringify(t.key);});
   if(returnTo)returnTo.focus();}});
 // Close/escape/back share one path; a refreshed home cannot remove the live sheet.
 topicViewer={close:function(){setOpen(false);dialog.remove();}};
 close.addEventListener("click",function(){setOpen(false);});
 dialog.addEventListener("cancel",function(e){e.preventDefault();setOpen(false);});setOpen(true);}
function renderTopics(d){var rows=d.topic_cards||[],active=rows.filter(function(t){return t.state!=="done";}),done=rows.filter(function(t){return t.state==="done";});
 sec(S.topicsTitle,active.length);var host=el("section","topics");host.setAttribute("aria-label",S.topicsTitle);
 var list=el("div","topic-cards");active.slice(0,topicLimit).forEach(function(t){list.appendChild(topicCard(t));});host.appendChild(list);
 if(active.length>topicLimit){var more=el("button","btn requests-more topics-more",S.more);more.type="button";
  more.addEventListener("click",function(){var start=topicLimit;topicLimit+=8;active.slice(start,topicLimit).forEach(function(t){list.appendChild(topicCard(t));});
   if(active.length<=topicLimit)more.remove();var first=list.children[start];if(first)first.focus();});host.appendChild(more);}
 if(done.length){var completed=fold("topics-done",S.topicsDone,done.length,"topics-done"),body=el("div","topic-cards");
  done.forEach(function(t){body.appendChild(topicCard(t));});completed.body.appendChild(body);host.appendChild(completed);}
 if(!rows.length)host.appendChild(el("div","safe",S.topicsEmpty));app.appendChild(host);return host;}
var requestProject="",requestOwnerOnly=false;
function requestAction(r,action,buttons){
 if(buttons.some(function(b){return b.disabled;}))return;
 buttons.forEach(function(b){b.disabled=true;});
 return post("/api/request",{project:r.project,id:r.id,action:action})
 .then(function(res){if(res.status===409)return res.json().then(function(j){throw j.error==="already closed"?"already closed":409;});
  if(!res.ok)throw res.status;toast(S.requestSaved);load();})
 .catch(function(st){buttons.forEach(function(b){b.disabled=false;});
  toast(st==="already closed"?S.requestClosed:st===403?S.reopen:st===503?S.busy:S.failed,true);
  if(st==="already closed")load();});}
function requestRank(a,b){return Number(!!b.urgent)-Number(!!a.urgent)||(b.updated||0)-(a.updated||0)||a.age-b.age||a.id-b.id;}  // urgent first, then newest
// Keep main's readable row and visible actions; fetch the full text, note and steps only when it opens.
function requestDetail(r,body){
 var R=V2.requests,key=r.project+":"+r.id+":"+r.updated;
 function fill(j){body.textContent="";body.removeAttribute("aria-busy");
  if(shown(j,"title")!==shown(r,"title")){var full=el("div","request-full",shown(j,"title"));full.dir="auto";body.appendChild(full);}
  if(shown(j,"note")){var note=el("div","request-note",shown(j,"note"));note.dir="auto";body.appendChild(note);}
  if(j.steps&&j.steps.length){var steps=el("div","request-steps"),list=el("ol");steps.appendChild(el("b",null,S.requestsSteps));
   j.steps.forEach(function(s){var step=el("li",null,shown(s,"text"));step.dir="auto";list.appendChild(step);});steps.appendChild(list);body.appendChild(steps);}
  body.setAttribute("data-loaded","1");}
 var cache=requestDetail.cache=requestDetail.cache||Object.create(null);
 if(cache[key]){fill(cache[key]);return;}
 body.textContent=R.loading;body.setAttribute("aria-busy","true");
 post("/api/request-detail",{project:r.project,id:r.id}).then(function(res){if(!res.ok)throw res.status;return res.json();})
 .then(function(j){if(Object.keys(cache).length>60)requestDetail.cache=Object.create(null);requestDetail.cache[key]=j;fill(j);})
 .catch(function(){body.textContent=R.loadFailed;body.removeAttribute("aria-busy");});}
function requestRow(r,context){
 var li=el("li","request-row"),done=r.status==="done",button=el("button","request-toggle"),lead=el("span","lead");
 button.type="button";li.setAttribute("data-request",r.project+":"+r.id);li.setAttribute("data-v2-request",r.project+":"+r.id);lead.appendChild(icon(done?"check":"box"));button.appendChild(lead);
 var b=el("span","body"),copy=typeof V2!=="undefined"?V2.now:S;
 var title=el("span","nm",shown(r,"title"));title.dir="auto";b.appendChild(title);
 var disclosure=el("span","request-disclosure");b.appendChild(disclosure);
 var meta=el("span","mt request-status-line");if(context){var project=el("span","request-context",r.project);project.dir="auto";meta.appendChild(project);}
 meta.appendChild(el("span","chip"+(r.wait==="owner"&&!done?" hot":""),done?S.requestsDone:r.wait==="owner"?S.requestsYou:S.requestsTeam));
 meta.appendChild(el("span","request-age",ago(r.age)));b.appendChild(meta);button.appendChild(b);li.appendChild(button);
 var details=el("div","request-details"),body=el("div","request-body"),key="request-row:"+JSON.stringify([r.project,r.id,!!context]),id="request-"+(++compactSerial);
 details.id=id;button.setAttribute("aria-controls",id);details.appendChild(body);li.appendChild(details);
 if(!done){var actions=el("div","request-actions"),buttons=[],armed="",until=0,timer;
  var choices=r.older?[["touch",S.requestTouch],["drop",S.requestDrop]]:[["done",S.requestDone],["drop",S.requestDrop],["push",S.requestPush]];
  function reset(){clearTimeout(timer);armed="";until=0;buttons.forEach(function(b,i){b.textContent=choices[i][1];});}
  choices.forEach(function(pair){var a=el("button","btn sm",pair[1]);a.type="button";
   a.addEventListener("click",function(){if(a.disabled)return;if(pair[0]==="touch"){requestAction(r,pair[0],buttons);return;}
    if(armed===pair[0]&&Date.now()<until){reset();requestAction(r,pair[0],buttons);return;}
    reset();armed=pair[0];until=Date.now()+4000;a.textContent=S.requestConfirm;timer=setTimeout(reset,4000);
   });buttons.push(a);actions.appendChild(a);});li.appendChild(actions);}
 function draw(on){opened[key]=on;li.className="request-row"+(on?" open":"");details.hidden=!on;button.setAttribute("aria-expanded",String(!!on));if(on&&!body.getAttribute("data-loaded")&&!body.getAttribute("aria-busy"))requestDetail(r,body);disclosure.textContent=on?(copy.requestLess||copy.less):(copy.requestMore||copy.more);}
 var setOpen=bindBackLayer(key,li,!!opened[key],draw);draw(!!opened[key]);
 button.addEventListener("click",function(){setOpen(!opened[key]);haptic("sel");});return li;}
// Up to five rows, then one button that shows the rest.
function requestList(rows,key,context){
 var box=el("div"),list=el("ul","requests-list"),sorted=rows.slice().sort(requestRank),limits=requestList.limits=requestList.limits||Object.create(null),limit=limits[key]||5;box.appendChild(list);
 sorted.slice(0,limit).forEach(function(r){list.appendChild(requestRow(r,context));});
 if(sorted.length>limit){var more=el("button","requests-more",requestText("showAll",sorted.length));more.type="button";
  more.addEventListener("click",function(){var first;
   sorted.slice(limit).forEach(function(r){var row=requestRow(r,context);if(!first)first=row;list.appendChild(row);});
   limits[key]=sorted.length;more.remove();if(first)first.querySelector("button").focus({preventScroll:true});haptic("sel");});box.appendChild(more);}
 return box;}
// One collapsible block per project, with its count in the header.
function requestGroups(prefix,rows){
 var box=el("div","request-groups"),names=[];
 rows.forEach(function(r){if(names.indexOf(r.project)<0)names.push(r.project);});
 var groups=names.map(function(n){return {name:n,rows:rows.filter(function(r){return r.project===n;})};});
 groups.sort(function(a,b){return Number(b.rows.some(function(r){return r.urgent;}))-Number(a.rows.some(function(r){return r.urgent;}))||b.rows.length-a.rows.length||a.name.localeCompare(b.name);});
 groups.forEach(function(g){var key=prefix+":"+g.name;
  var block=compactBlock("requests-group:"+key,g.name,nf.format(g.rows.length),requestList(g.rows,key),"request-project",false,true);
  box.appendChild(block);});
 return box;}
function requestText(key,n){return V2.requests[key].replace("{n}",nf.format(n));}
function requestBoard(d,options){
 options=options||{};
 var R=V2.requests,rows=(d.requests||[]).filter(function(r){return !options.omitOwner||!(r.status==="open"&&r.wait==="owner"&&!r.older);});
 var open=rows.filter(function(r){return r.status!=="done";}),projects=Array.from(new Set(open.map(function(r){return r.project;})));
 projects.sort(function(a,b){var ac=open.filter(function(r){return r.project===a&&r.wait==="owner";}).length,bc=open.filter(function(r){return r.project===b&&r.wait==="owner";}).length;return bc-ac||a.localeCompare(b);});
 if(requestProject&&projects.indexOf(requestProject)<0)requestProject="";
 var host=el("section","requests"),filters=el("div","requests-filter");host.setAttribute("aria-label",S.requestsTitle);if(options.omitOwner)host.setAttribute("data-v2-section","requests");
 function refresh(){host.replaceWith(requestBoard(d,options));}
 function pill(name,label,count){var b=el("button","btn");b.type="button";b.setAttribute("aria-pressed",String(requestProject===name));b.appendChild(el("span","requests-pill-name",label));b.appendChild(el("span","requests-count",nf.format(count)));b.addEventListener("click",function(){requestProject=name;requestOwnerOnly=false;refresh();});filters.appendChild(b);}
 pill("",S.requestsAll,open.length);projects.forEach(function(p){pill(p,p,open.filter(function(r){return r.project===p;}).length);});host.appendChild(filters);
 var visible=rows.filter(function(r){return (!requestProject||r.project===requestProject)&&(!requestOwnerOnly||options.omitOwner||r.wait==="owner");});
 var active=visible.filter(function(r){return r.status!=="done";}),done=visible.filter(function(r){return r.status==="done";});
 var fresh=active.filter(function(r){return !r.long&&!r.older;}),mine=fresh.filter(function(r){return r.wait==="owner";}),agents=fresh.filter(function(r){return r.wait!=="owner";}),waiting=active.filter(function(r){return r.long||r.older;});
 if(requestOwnerOnly&&!options.omitOwner){var back=el("button","requests-back",S.requestsBackAll);back.addEventListener("click",function(){requestOwnerOnly=false;refresh();});host.appendChild(back);}
 if(!active.length)host.appendChild(el("div","safe",S.requestsEmpty));
 else if(!options.omitOwner){var needs=el("div","requests-needs"),head=el("div","requests-head"),h=el("h3",null,R.needsYou);h.appendChild(el("span","requests-count",nf.format(mine.length)));head.appendChild(h);
  if(!requestOwnerOnly&&mine.length){var show=el("button","link-btn",S.requestsShowAll.replace("{n}",nf.format(mine.length)));show.addEventListener("click",function(){requestOwnerOnly=true;refresh();});head.appendChild(show);}
  needs.appendChild(head);host.appendChild(needs);
  if(mine.length)host.appendChild(requestGroups("owner",mine));else host.appendChild(el("div","safe",R.nothing));}
 function section(key,title,list,prefix){var body=requestGroups(prefix,list);if(list.some(function(r){return r.older;}))body.appendChild(el("div","requests-old-review",S.requestsReviewOld));var f=compactBlock(key,title,nf.format(list.length),body,"requests-fold");f.setAttribute("data-requests",prefix);if(options.omitOwner&&prefix==="long")f.setAttribute("data-v2-section","dormant");host.appendChild(f);}
 if(agents.length)section("requests-agents",R.agents,agents,"agents");
 if(waiting.length)section("requests-long",R.longWait,waiting,"long");
 if(done.length&&(!requestOwnerOnly||options.omitOwner)){var completed=compactBlock("requests-done",S.requestsDone,nf.format(done.length),requestGroups("done",done),"requests-fold");completed.setAttribute("data-requests","done");if(options.omitOwner)completed.setAttribute("data-v2-section","older");host.appendChild(completed);}
 return host;}
function renderRequests(d){
 sec(S.requestsTitle,(d.requests||[]).filter(function(r){return r.status!=="done";}).length);
 var host=requestBoard(d);app.appendChild(host);return host;}

function macCard(m){
 var c=el("div","card mac"),h=el("div","s-head");h.appendChild(el("b",null,S.macTitle));c.appendChild(h);
 function statusText(prefix,status){var suffix={ok:"Ok",warn:"Warn",critical:"Critical"}[status];return suffix?S[prefix+suffix]:S.macUnavailable;}
 function row(label,value,status,prefix){var r=el("div","mac-row"),a=el("div"),v=el("div","mac-value");a.appendChild(el("div",null,label));if(prefix)a.appendChild(el("div","mac-detail mac-status "+(status||""),statusText(prefix,status)));v.textContent=value;c.appendChild(r);r.appendChild(a);r.appendChild(v);}
 function gb(n){return n==null?S.macUnavailable:nf.format(Math.round(n/1073741824*10)/10)+" "+S.macGb;}
 if(!m||!m.available){c.appendChild(el("div","mac-detail",S.macUnavailable));return c;}
 c.appendChild(el("div","mac-detail mac-status "+m.status,statusText("mac",m.status)));
 var mem=m.memory||{},cpu=m.cpu||{},disk=m.disk||{},bat=m.battery;
 row(S.macMemory,gb(mem.available_bytes),mem.status,"macMemory");
 row(S.macCpu,cpu.load?cpu.load.map(function(n){return nf.format(Math.round(n*100)/100);}).join(" / ")+" · "+nf.format(cpu.cores)+" "+S.macCores:S.macUnavailable,cpu.status,"macCpu");
 row(S.macDisk,gb(disk.free_bytes),disk.status,"macDisk");
 row(S.macBattery,!bat?S.macUnavailable:!bat.present?S.macNoBattery:nf.format(bat.percent)+"% · "+(bat.plugged_in?S.macPower:S.macOnBattery),bat&&bat.status,bat&&bat.present?"macBattery":null);
 row(S.macUptime,m.uptime_seconds==null?S.macUnavailable:nf.format(Math.floor(m.uptime_seconds/3600))+" "+S.macHours);
 var kinds={test:S.macTest,encoder:S.macEncoder,agent:S.macAgent,browser:S.macBrowser,build:S.macBuild,other:S.macOther};
 [[S.macTopCpu,"cpu"],[S.macTopMemory,"memory"]].forEach(function(pair){var p=el("div","mac-processes"),list=m.processes&&m.processes[pair[1]];p.appendChild(el("b",null,pair[0]));
  if(!list||!list.length)p.appendChild(el("div","mac-detail",S.macUnavailable));else list.slice(0,3).forEach(function(x){rowInProcess(p,kinds[x.kind]||S.macOther,pair[1]==="cpu"?nf.format(x.cpu_percent)+"%":gb(x.memory_bytes));});c.appendChild(p);});
 return c;
}
function rowInProcess(parent,label,value){var r=el("div","mac-row");r.appendChild(el("span",null,label));r.appendChild(el("span","mac-value",value));parent.appendChild(r);}
function renderAll(d){
 var by=groups(d),dl=d.decisions||[],open=dl.filter(function(x){return !closed[x.id]&&!x.stale&&!inOld(x);});
 app.appendChild(hero(d));
 var tl=el("div","tiles");tl.appendChild(tile(waitingCount(d),S.nWaiting,"hot"));
 tl.appendChild(tile(by.working.length,S.nWorking,"go"));tl.appendChild(tile(by.recent.length,S.nRecent));app.appendChild(enter(tl,"tiles"));
 digestCard();
 if(open.length||answeredIds(true).length){var h=sec(S.waiting,open.length||null,open.length?"hot":"");
  if(open.some(isNew)||(d.cards||[]).some(function(c){return ASKS[c.kind]&&!c.ended&&!c.old&&isNew(c);})){var mk=el("button","link-btn end",S.markRead);mk.type="button";mk.addEventListener("click",markSeen);h.appendChild(mk);}}
 decisions(dl,true);
 renderTopics(d);
 cardSections(d,false);
 renderRequests(d);
 var rs=d.reminders||[],fresh=rs.filter(function(r){return !r.old;}),aged=rs.filter(function(r){return r.old;});
 if(fresh.length){sec(S.reminders,fresh.length);var c=el("div","card group");
  fresh.slice(0,3).forEach(function(r,k){c.appendChild(remRow(r,k));});app.appendChild(c);
  if(fresh.length>3){var mf=fold("rem",S.more,fresh.length-3),mc=el("div","card group");fresh.slice(3).forEach(function(r,k){mc.appendChild(remRow(r,k+3));});
   mf.body.appendChild(mc);app.appendChild(mf);}}
 var ts=d.tasks||[];
 if(ts.length){var task=function(x,k){var m=[];if(x.topic)m.push(shown(x,"topic"));m.push(ago(x.age));return item("box","blue",x,"text",m,"","t"+k+x.text);};
  sec(S.openTasks,ts.length);var tc=el("div","card group");ts.slice(0,3).forEach(function(x,k){tc.appendChild(task(x,k));});app.appendChild(tc);
  if(ts.length>3){var tf=fold("tasks",S.more,ts.length-3),tm=el("div","card group");ts.slice(3).forEach(function(x,k){tm.appendChild(task(x,k+3));});
   tf.body.appendChild(tm);app.appendChild(tf);}}
 oldGroup(dl.filter(inOld),oldCards(d,false),aged,true);
 var mac=macCard(d.mac);app.appendChild(d.mac&&d.mac.available?compactBlock("mac",S.macTitle,({ok:S.macOk,warn:S.macWarn,critical:S.macCritical}[d.mac.status]||S.macUnavailable),mac):mac);
 sec(S.sessions,by.working.length||null,"go");
 if(!by.working.length)app.appendChild(empty("cpu",S.none,S.noneHint));
 else{var g=el("div","card group");by.working.slice(0,4).forEach(function(s){var r=el("div","row tap");r.appendChild(robot(s.id,s.state,true));
   var b=el("div","body"),n=el("div","nm",shown(s,"name"));n.dir="auto";b.appendChild(n);
   if(s.now)b.appendChild(line("act","bolt",shown(s,"now")));if(s.usage)b.appendChild(el("div","safe",usageLine(s.usage.today)));r.appendChild(b);r.appendChild(el("span","when",ago(s.age)));r.appendChild(icon("chev"));
   r.addEventListener("click",function(){opened["s"+s.id]=true;switchTo("agents");});g.appendChild(r);});app.appendChild(g);}
 var all=el("button","link-btn all");all.type="button";all.appendChild(el("span",null,S.allAgents));all.appendChild(icon("chev"));
 all.addEventListener("click",function(){switchTo("agents");});app.appendChild(all);
}
// --- the Morning Brief home (brief.py): one sentence, what changed, what waits, every line with its age ---------
// Shown instead of "For me" only while its switch is on (the state then carries "brief"). Every fact shows its age
// and greys when the server says it is stale; tones come from the server's facts, never from an agent's words.
var briefSeen=0, rts=new Intl.RelativeTimeFormat(LANG,{numeric:"auto",style:"short"});
var lf=Intl.ListFormat?new Intl.ListFormat(LANG,{type:"conjunction"}):null;
var ASKTO={waiting:"waiting",stuck:"lines",working:"lines",changed:"since",mac:"teams"};
// short minutes, whole hours and days; Hebrew ICU writes "(1)" after a dual or single word ("שעה (1)"): dropped
function agoS(sec){if(sec==null)return "–";if(sec<45)return S.now;var m=Math.round(sec/60),h=Math.round(sec/3600);
 var t=m<60?rts.format(-m,"minute"):h<36?rtf.format(-h,"hour"):rtf.format(-Math.round(sec/86400),"day");return t.replace(/\s*\(\d+\)/,"");}
function word(n,form){var w=S[form==="*"?"numsF":form==="~"?"numsC":"numsA"].split(",");return n>=0&&n<w.length?w[n]:nf.format(n);}
function fill(n,one,many){return (n===1?one:many).replace(/#([~*]?)/,function(m,f){return word(n,f);});}
function joined(parts){return lf?lf.format(parts):parts.join(", ");}
function headline(b){var h=b.headline,p=[];
 if(h.moving)p.push(fill(h.moving,S.hlMoving1,S.hlMoving));
 if(h.stuck)p.push(fill(h.stuck,S.hlStuck1,S.hlStuck));
 if(h.waiting)p.push(fill(h.waiting,S.hlWait1,S.hlWait));
 var s=p.length?joined(p)+S.dot:h.lines?fill(h.lines,S.hlQuiet1,S.hlQuiet)+S.dot:S.hlCalm;
 return s.charAt(0).toUpperCase()+s.slice(1);}
function bCard(cls,key,title,end){var c=el("div","card bf "+cls),h=el("div","bf-h");c.setAttribute("data-k",key);
 h.appendChild(el("span","bf-t",title));if(end!=null)h.appendChild(el("span","bf-e",end));c.appendChild(h);return c;}
function ageRow(parts){var a=el("div","bage");parts.forEach(function(p){if(!p)return;var x=el("span",null,p[0]);if(p[1])x.dir="auto";a.appendChild(x);});return a;}
function bAct(label,soft,run){var b=el("button","bact"+(soft?" soft":""),label);b.type="button";
 b.addEventListener("click",function(e){e.stopPropagation();haptic("tap");run();});return b;}
function bAgent(sid){var n=el("div","b-agent"),id=el("div",null,sid||S.agentUnknown);
 id.dir=sid?"ltr":"auto";n.appendChild(el("div",null,S.agentFrom));n.appendChild(id);return n;}
function agentText(tag,cls,text,sid){var box=el("div","b-agent-text"),t=el(tag,cls,text);t.dir="auto";
 box.appendChild(bAgent(sid));box.appendChild(t);return box;}
function briefHead(b,d){var c=bCard("bhd"+(b.headline.waiting?" hot":""),"headline","",null),at=new Date(d.now*1000),hr=at.getHours();
 var part=hr<5||hr>=21?"night":hr<12?"morning":hr<17?"noon":"evening";
 c.firstChild.firstChild.textContent=S["brief_"+part];c.firstChild.appendChild(el("span","bf-e",new Intl.DateTimeFormat(LANG,{weekday:"long"}).format(at)));
 c.appendChild(el("div","hl",headline(b)));var slot=el("div","bf-slot");slot.setAttribute("data-slot","listen");slot.hidden=true;c.appendChild(slot);return c;}
function sinceStrip(b){var s=b.since,c=bCard("bsince","since",S.since,s?agoS(s.age):null),ch=el("div","bchips");
 if(!s)ch.appendChild(el("span","bchip",S.sinceFirst));
 else{if(s.done)ch.appendChild(el("span","bchip ok",fill(s.done,S.sDone1,S.sDone)));
  if(s.stuck)ch.appendChild(el("span","bchip bad",fill(s.stuck,S.sStuck1,S.sStuck)));
  if(s.new)ch.appendChild(el("span","bchip warn",fill(s.new,S.sNew1,S.sNew)));
  if(!s.done&&!s.stuck&&!s.new)ch.appendChild(el("span","bchip",S.sNone));}
 c.appendChild(ch);return c;}
function waitTarget(w,d){var list=w.kind==="decision"?d.decisions:w.kind==="card"?d.cards:null;
 return (list||[]).filter(function(x){return x.id===w.id;})[0]||null;}
// each waiting item has exactly one button: a question opens its own answer card in place, a ledger item its topic
function waitRow(w,d){var t=waitTarget(w,d),wrap=el("div","bi"),r=el("div","bw"+(w.stale?" stale":"")),body=el("div","bw-b");
 var tx=el("div","bw-t");
 if(w.line_name){var k=el("div","bw-k",w.line_name);k.dir="auto";tx.appendChild(k);}
 var tq=el("div",null,shown(w,"text"));tq.dir="auto";tx.appendChild(tq);body.appendChild(tx);
 if(w.note)body.appendChild(agentText("div","bw-n",shown(w,"note"),w.note_by));
 body.appendChild(ageRow([[agoS(w.age)],w.stale?[S.staleBadge]:null]));r.appendChild(body);
 var key="bw"+w.id,box=el("div","bexp");box.hidden=!opened[key];
 var draw=function(){},setWait=bindBackLayer("wait:"+key,box,opened[key],function(on){opened[key]=on;box.hidden=!on;draw(on);});
 if(t){var label=w.kind==="card"&&t.kind==="choice"?S.bChoose:S.bAnswer;
  var b=bAct(opened[key]?S.bClose:label,!!opened[key],function(){setWait(!opened[key]);});
  draw=function(on){b.textContent=on?S.bClose:label;b.className="bact"+(on?" soft":"");
   if(on&&!box.firstChild)box.appendChild(w.kind==="decision"?decision(t,true):agentCard(t));};
  r.appendChild(b);if(opened[key])box.appendChild(w.kind==="decision"?decision(t,true):agentCard(t));}
 else if(w.url)r.appendChild(bAct(S.openTopic,true,function(){openLink(w.url);}));
 else{var dt=el("div","bl-x");if(w.detail){var dn=el("div",null,shown(w,"detail"));dn.dir="auto";dt.appendChild(dn);}
  dt.appendChild(ageRow([[S.srcFrom+": "+S.src_ledger],[agoS(w.age)]]));box.appendChild(dt);  // at least where it comes from
  var db=bAct(opened[key]?S.bClose:S.bDetails,true,function(){setWait(!opened[key]);});
  draw=function(on){db.textContent=on?S.bClose:S.bDetails;};
  r.appendChild(db);}
 wrap.appendChild(r);wrap.appendChild(box);return wrap;}
function waitBox(b,d){var list=b.waiting.filter(function(w){return !(w.kind==="decision"&&closed[w.id])&&!(w.kind==="card"&&cardDone[w.id])
  &&(w.kind==="ledger"||waitTarget(w,d));});
 var c=bCard("bwait"+(list.length?" hot":""),"waiting",S.waiting,list.length?word(list.length,"a"):null);
 if(!list.length)c.appendChild(el("div","bf-none",S.waitNone));
 list.forEach(function(w){c.appendChild(waitRow(w,d));});return c;}
function muteBtn(sid,on){return bAct(on?S.unmuteIt:S.muteIt,true,function(){
 sendJson("/api/block-mute",{session:sid,on:!on},function(){toast(on?S.unmutedOk:S.mutedOk);});});}
function lineDetails(x){var box=el("div","bl-x");
 var src=[S.srcFrom+": "+(S["src_"+x.source]||S.src_ledger)];if(x.by)src.push("#"+x.by.slice(0,4));src.push(agoS(x.age));
 box.appendChild(ageRow(src.map(function(p,i){return [p,i===1];})));
 if(x.checklist){var cl=x.checklist;if(cl.title)box.appendChild(agentText("div","bn-k",cl.title,cl.session));
  cl.items.forEach(function(i){var r=el("div","ck"+(i.done?"":" no"));r.appendChild(icon(i.done?"check":"box"));
   r.appendChild(agentText("span",null,i.text,cl.session));box.appendChild(r);});
  box.appendChild(ageRow([[agoS(cl.age)],cl.stale?[S.staleBadge]:null]));}
 if(x.source==="status"&&x.by&&!x.muted)box.appendChild(muteBtn(x.by,false));
 (x.muted_by||[]).forEach(function(sid){box.appendChild(muteBtn(sid,true));});
 return box;}
function lineRow(x){var wrap=el("div","bi"),r=el("div","bl t-"+x.tone+(x.stale?" stale":"")),body=el("div","bl-b");
 r.appendChild(el("span","tdot"));var nm=el("div","bl-n"),n=el("b",null,x.name);n.dir="auto";nm.appendChild(n);
 if(x.source==="status"||x.checklist)nm.appendChild(bAgent(x.by||(x.checklist&&x.checklist.session)));
 if(x.stale&&x.text)nm.appendChild(el("span","old-b",S.staleBadge));else if(x.live)nm.appendChild(el("span","live-b",S.liveBadge));
 body.appendChild(nm);
 if(x.text){var s=x.by?agentText("div","bl-s",shown(x,"text"),x.by):el("div","bl-s",shown(x,"text"));s.dir="auto";body.appendChild(s);}
 else if(x.muted)body.appendChild(el("div","bl-s dim",S.mutedLine));
 if(x.why)body.appendChild(el("div","bl-r",S["why_"+x.why]||S.why_no_move));
 if(x.checklist){var dn=x.checklist.items.filter(function(i){return i.done;}).length;
  body.appendChild(el("div","bl-c",S.clDone.replace("{1}",nf.format(dn)).replace("{2}",nf.format(x.checklist.items.length))));}
 body.appendChild(ageRow([[agoS(x.age)]]));
 if(x.tone==="bad"&&x.url)body.appendChild(bAct(S.openTopic,false,function(){openLink(x.url);}));
 r.appendChild(body);var det=compactBlock("brief-line-"+x.key,S.compactDetails,null,lineDetails(x));
 wrap.appendChild(r);wrap.appendChild(det);return wrap;}
function linesBox(b){var c=bCard("blines","lines",S.lines,null);
 if(!b.lines.length)c.appendChild(el("div","bf-none",b.source?S.linesNone:S.linesNoSource));
 b.lines.forEach(function(x){c.appendChild(lineRow(x));});return c;}
function noticedBox(b){var c=bCard("bnot","noticed",S.noticed,null);
 if(!b.noticed.length)c.appendChild(el("div","bf-none",S.noticedNone));
 var list=el("div");b.noticed.forEach(function(n){var wrap=el("div","bi"),r=el("div","bn"+(n.stale?" stale":"")),body=el("div","bn-b");
  body.appendChild(el("div","bn-k",S["n_"+n.kind+(n.kind==="stale_card"?"_"+n.why:"")]||S.n_note));
  if(n.text){var t=el("div","bn-t",shown(n,"text"));t.dir="auto";body.appendChild(t);}
  body.appendChild(ageRow([n.line_name?[n.line_name,true]:null,[agoS(n.age)]]));r.appendChild(body);
  if(n.url)r.appendChild(bAct(S.openTopic,true,function(){openLink(n.url);}));
  wrap.appendChild(r);list.appendChild(wrap);});
 compactLong(c,"brief-noticed",S.noticed,list,b.noticed.length);return c;}
function teamsRow(b){var t=b.teams,r=el("button","card bf trow");r.type="button";r.setAttribute("data-k","teams");
 var p=[t.working?fill(t.working,S.tWork1,S.tWork):S.tNone];if(t.unused.length)p.push(fill(t.unused.length,S.tWaste1,S.tWaste));
 r.appendChild(el("span","tr-a"+(t.stale&&!t.working?" stale":""),S.teams+": "+joined(p)));
 if(b.mac)r.appendChild(el("span","tr-m m-"+b.mac.status+(b.mac.stale?" stale":""),S.mac+": "+(S["mac_"+b.mac.status]||S.mac_unknown)));
 r.addEventListener("click",function(){switchTo("agents");});return r;}
function jumpTo(k){var n=app.querySelector('[data-k="'+k+'"]');if(!n)return;
 try{n.scrollIntoView({behavior:reduce?"auto":"smooth",block:"start"});}catch(e){n.scrollIntoView();}
 n.classList.remove("flash");void n.offsetWidth;n.classList.add("flash");}
// the ask box: the fixed questions jump to the part of the brief that answers them; free text answers come later
function askBox(b){var c=el("div","askbox"+(opened.ask?" open":""));c.setAttribute("data-k","ask");
 var q=el("div","ask-q");b.ask.forEach(function(k){var x=el("button",null,S["q_"+k]);x.type="button";
  x.addEventListener("click",function(){haptic("sel");jumpTo(ASKTO[k]);});q.appendChild(x);});c.appendChild(q);
 var f=el("div","ask-f"),inp=el("input");inp.type="text";inp.maxLength=200;inp.placeholder=S.askPh;inp.setAttribute("aria-label",S.askPh);
 inp.setAttribute("enterkeyhint","send");inp.value=notes.ask||"";tune(inp,false);  // empty: the page's direction; with text: auto
 var soon=el("div","safe",S.askSoon);soon.hidden=true;
 var go=el("button","ask-go");go.type="button";go.appendChild(icon("send"));go.setAttribute("aria-label",S.send);
 var ask=function(){if(!inp.value.trim())return;haptic("sel");soon.hidden=false;};
 inp.addEventListener("focus",function(){opened.ask=true;c.classList.add("open");});
 inp.addEventListener("input",function(){notes.ask=inp.value;soon.hidden=true;});
 inp.addEventListener("keydown",function(e){if(e.key==="Enter"){e.preventDefault();ask();}});go.addEventListener("click",ask);
 f.appendChild(icon("chat"));f.appendChild(inp);f.appendChild(go);c.appendChild(f);c.appendChild(soon);return c;}
function renderBrief(d){var b=d.brief;briefSeen=d.now;
 renderBriefReminders(d);
 app.appendChild(enter(briefHead(b,d),"b-hd"));app.appendChild(enter(sinceStrip(b),"b-since"));
 app.appendChild(enter(waitBox(b,d),"b-wait"));renderTopics(d);app.appendChild(enter(linesBox(b),"b-lines"));
 renderRequests(d);
 app.appendChild(enter(noticedBox(b),"b-noticed"));app.appendChild(enter(teamsRow(b),"b-teams"));app.appendChild(askBox(b));}
// the "since you last looked" marker moves only when the owner leaves the page, so the strip stays put while he reads
function markBrief(){var at=briefSeen;if(!at||!wa||!wa.initData)return;briefSeen=0;
 try{fetch("/api/brief-seen",{method:"POST",keepalive:true,headers:{"X-Telegram-Init-Data":wa.initData,"Content-Type":"application/json"},
  body:JSON.stringify({at:at})}).catch(function(){});}catch(e){}}
document.addEventListener("visibilitychange",function(){if(document.hidden)markBrief();});
window.addEventListener("pagehide",markBrief);
// --- "Agents": every session, then quota and radio -----------------------------------------------------------
function quotaCard(list){
 var qc=el("div","card group");
 list.forEach(function(q){var a=el("div","acct"),ah=el("div","ah"),l=el("div","lead blue");l.appendChild(icon("chart"));ah.appendChild(l);
  ah.appendChild(el("div","nm",iso(q.label)));if(q.weekly!=null)ah.appendChild(el("b","pc",nf.format(Math.round(q.weekly))+"%"));a.appendChild(ah);
  [[S.week,q.weekly,q.weekly_resets]].forEach(function(w){
   if(w[1]==null)return;var mt=el("div","meter"),lb=el("div","lb"),u=until(w[2]);
   mt.appendChild(bar(w[1]));lb.appendChild(el("span",null,w[0]+(u?" · "+S.reset+" "+u:"")));
   mt.appendChild(lb);a.appendChild(mt);});
  if(q.banked!=null){var bm=el("div","meter bk"),bk=el("div","lb");bk.appendChild(el("span",null,S.banked));bk.appendChild(el("b",null,nf.format(q.banked)));
   bm.appendChild(bk);a.appendChild(bm);}
  qc.appendChild(a);});
 return qc;}
function bankCard(list){
 var c=el("div","card group");
 function ymd(d){return d.getFullYear()+"-"+String(d.getMonth()+1).padStart(2,"0")+"-"+String(d.getDate()).padStart(2,"0");}
 // one change at a time: send it, then redraw from the bank the server returns
 function save(body,ctl,undo){haptic("tap");ctl.disabled=true;
  post("/api/bank",body).then(function(res){if(!res.ok)throw res.status;return res.json();})
   .then(function(j){haptic("n","success");if(last&&j.bank){last.bank=j.bank;render(last,true);}})
   .catch(function(st){haptic("n","error");ctl.disabled=false;if(undo)undo();toast(st===403?S.reopen:S.failed,true);});}
 list.forEach(function(r){var row=el("div","row bank"),l=el("div","lead blue");l.appendChild(icon("chart"));row.appendChild(l);
  var b=el("div","body");b.appendChild(el("div","nm",iso(r.name.charAt(0).toUpperCase()+r.name.slice(1))));row.appendChild(b);
  var n=el("b","bn",r.n==null?S.bankUnset:nf.format(r.n));if(r.n==null)n.className+=" unset";
  function step(delta,ic,lbl){var x=el("button","stepb");x.type="button";x.appendChild(icon(ic));x.setAttribute("aria-label",lbl+" "+r.name);
   var to=Math.max(0,Math.min(99,(r.n||0)+delta));x.disabled=(delta<0&&!r.n)||(delta>0&&r.n>=99);
   x.addEventListener("click",function(){save({name:r.name,n:to},x);});
   return x;}
  var ctl=el("div","stepper");ctl.appendChild(step(-1,"minus",S.less));ctl.appendChild(n);ctl.appendChild(step(1,"plus",S.more1));row.appendChild(ctl);c.appendChild(row);
  if(!(r.n>0))return;
  // the date the saved resets expire, set by the owner: a reset not spent by then is lost
  var x=el("div","bankx");x.appendChild(el("span",null,S.bankExpires));
  var d=el("input","bkd");d.type="date";d.value=r.expires||"";d.max=ymd(new Date(Date.now()+400*864e5));
  d.setAttribute("aria-label",S.bankExpires+" "+r.name);
  d.addEventListener("change",function(){if((d.value||null)===(r.expires||null))return;
   save({name:r.name,expires:d.value||null},d,function(){d.value=r.expires||"";});});x.appendChild(d);
  if(r.expires){var k=el("button","link-btn",S.bankClear);k.type="button";k.setAttribute("aria-label",S.bankClear+" "+r.name);
   k.addEventListener("click",function(){save({name:r.name,expires:null},k);});x.appendChild(k);
   if(r.days!=null){var t=r.days<0?S.bankExpired:r.days===0?S.bankToday:r.days===1?S.bankLeft1:S.bankLeft.replace("#",nf.format(r.days));
    x.appendChild(el("span","dl"+(r.days<=7?" soon":""),t));}}
  c.appendChild(x);});
 c.appendChild(el("div","safe bankh",S.bankHint));return c;}
function renderAgents(d){
 var by=groups(d);
 usageCard(d.usage_totals);
 sec(S.sessions,by.working.length||null,"go");
 if(!by.working.length)app.appendChild(empty("cpu",S.none,S.noneHint));
 else{var g=el("div","card group");by.working.forEach(function(s){g.appendChild(sessionRow(s,d,"now"));});app.appendChild(enter(g,"ag-w"));}
 if(by.idle.length){sec(S.idleList,by.idle.length,"hot");
  if(by.recent.length){var rg=el("div","card group");by.recent.forEach(function(s){rg.appendChild(sessionRow(s,d,"summary"));});app.appendChild(rg);}
  if(by.rest.length){var idf=fold("idle",by.recent.length?S.more:S.idleList,by.rest.length),ig=el("div","card group");
   by.rest.forEach(function(s){ig.appendChild(sessionRow(s,d,"summary"));});idf.body.appendChild(ig);app.appendChild(idf);}}
 if(by.left.length){var lf=fold("left",S.leftList,by.left.length),lg=el("div","card group");
  lg.appendChild(el("div","hint pad",S.leftHint));by.left.forEach(function(s){lg.appendChild(sessionRow(s,d,"summary"));});
  if(by.left.length>1){var lrow=el("div","btns pad");lrow.appendChild(closeBtn(by.left.slice(0,20).map(function(s){return s.id;}),S.closeAll));lg.appendChild(lrow);}
  lf.body.appendChild(lg);app.appendChild(lf);}
 if(by.ended.length){var det=fold("ended",S.endedList,by.ended.length);
  var eg=el("div","card group");by.ended.forEach(function(s){eg.appendChild(sessionRow(s,d,"none"));});det.body.appendChild(eg);app.appendChild(det);}
 if((d.quota||[]).length){sec(S.quota);app.appendChild(quotaCard(d.quota));}
 if((d.bank||[]).length){sec(S.bank);app.appendChild(bankCard(d.bank));}
 if(d.radio){sec(S.radio);app.appendChild(radioCard(d.radio,d.radio_music));app.appendChild(programCard());
  if(d.room&&d.room.enabled&&(d.room.room||d.radio.on)){sec(S.room);app.appendChild(roomCard(d));}
  if(d.settings&&"radio_every_reply" in d.settings){var rc=el("div","card group");rc.appendChild(switchRow("radio_every_reply",d));if("radio_board" in d.settings)rc.appendChild(switchRow("radio_board",d));if("radio_music" in d.settings)rc.appendChild(switchRow("radio_music",d));
   if("radio_room" in d.settings)rc.appendChild(switchRow("radio_room",d));app.appendChild(rc);}}
 if(d.radio&&d.podcast){sec(S.podcast);app.appendChild(podcastCard(d.podcast,d.radio));}
 if(d.youtube){sec(S.ytTitle);app.appendChild(youtubeCard(d.youtube));}
}
// --- the YouTube staff radio: connect, switch, projects, two-tap start, stop, disconnect -----------------------
var ytArmed=null;  // {token, channel, until}: the first tap of Start, confirmed by the second
function ytPost(body,btn){haptic("tap");if(btn)btn.disabled=true;
 return post("/api/youtube",body).then(function(r){if(!r.ok)throw r.status;return r.json();})
  .catch(function(st){haptic("n","error");if(btn)btn.disabled=false;
   toast(st===403?S.reopen:st===503?S.ytBridgeOff:st===409?S.ytNotReady:S.failed,true);throw st;});}
function ytLater(){toast(S.ytWait);setTimeout(load,8000);}
function ytSwitch(y){var r=el("div","row"),b=el("div","body");b.appendChild(el("div","nm",S.ytEnable));b.appendChild(el("div","mt",S.ytEnableHint));r.appendChild(b);
 var sw=el("button","sw"+(y.enabled?" on":""));sw.type="button";sw.setAttribute("role","switch");sw.setAttribute("aria-checked",y.enabled?"true":"false");
 sw.setAttribute("aria-label",S.ytEnable);sw.appendChild(el("i"));
 sw.addEventListener("click",function(){ytPost({action:"enable",on:!y.enabled},sw).then(function(){load();}).catch(function(){});});r.appendChild(sw);return r;}
function ytProjects(y){var box=el("div","ytp"),list=y.projects||[];box.appendChild(el("div","nm",S.ytProjects));box.appendChild(el("div","mt",S.ytProjectsHint));
 if(!list.length){box.appendChild(el("div","safe",S.ytNoProjects));return box;}
 list.forEach(function(p){var lab=el("label","row chk"),cb=el("input");cb.type="checkbox";cb.checked=!!p.on;
  cb.addEventListener("change",function(){var keys=list.filter(function(q){return q.key===p.key?cb.checked:q.on;}).map(function(q){return q.key;});
   ytPost({action:"projects",keys:keys},cb).then(function(){load();}).catch(function(){cb.checked=!cb.checked;});});
  lab.appendChild(cb);var t=el("div","body");t.appendChild(el("div","nm",iso(p.name)));
  if(!p.speakable)t.appendChild(el("div","mt warn",S.ytUnspeakable));lab.appendChild(t);box.appendChild(lab);});
 return box;}
function ytConfirm(c,y,go){var box=el("div","ytc");box.appendChild(el("div","nm",ytArmed.mirror?S.ytMirrorConfirm:S.ytConfirm));box.appendChild(el("div","ch",iso(ytArmed.channel||y.channel)));
 var row=el("div","btns"),yes=el("button","btn sm pri",S.ytConfirmGo),no=el("button","btn sm",S.ytCancel);yes.type=no.type="button";
 yes.addEventListener("click",function(){var mirror=ytArmed&&ytArmed.mirror;var t=ytArmed&&Date.now()<ytArmed.until?ytArmed.token:null;ytArmed=null;if(!t){load();return;}
  ytPost({action:mirror?"mirror-on":"start",armed:t},yes).then(function(){haptic("n","success");ytLater();}).catch(function(){});});
 no.addEventListener("click",function(){ytArmed=null;load();});row.appendChild(yes);row.appendChild(no);box.appendChild(row);c.appendChild(box);}
function youtubeCard(y){
 var c=el("div","card yt"),h=el("div","s-head"),l=el("div","lead"+(y.live?" ok":""));l.appendChild(icon("mic"));h.appendChild(l);
 var nb=el("div","nm");nb.appendChild(el("div",null,y.edition==="mirror"?S.ytMirrorTitle:S.ytTitle));var hint=el("div","safe",y.edition==="mirror"?S.ytMirrorHint:S.ytHint);hint.style.margin="0";nb.appendChild(hint);h.appendChild(nb);
 h.appendChild(el("span","pill"+(y.live?" on":""),y.live?S.ytLiveNow:y.connected?S.ytReady:S.ytNotConnected));c.appendChild(h);
 if(!y.setup||!y.setup.client||!y.setup.base){c.appendChild(el("div","safe",S.ytSetup));return c;}
 if(y.bridge_off)c.appendChild(el("div","err",S.ytBridgeOff));
 if(y.pending)c.appendChild(el("div","safe",S.ytWait));
 else if(y.result==="youtube_reconnect_client"||y.result==="youtube_reconnect")c.appendChild(el("div","err",S.ytReconnect));
 else if(y.result==="youtube_disconnected_revoke_failed")c.appendChild(el("div","safe",S.ytRevokeFailed));
 else if(y.failed){var reasons=({"youtube_rules_invalid":S.ytErr_youtube_rules_invalid,"youtube_disabled":S.ytErr_youtube_disabled,"youtube_not_connected":S.ytErr_youtube_not_connected,"youtube_not_enabled":S.ytErr_youtube_not_enabled,"youtube_api_failed":S.ytErr_youtube_api_failed,"youtube_job_failed":S.ytErr_youtube_job_failed,"youtube_busy":S.ytErr_youtube_busy,"youtube_channel_changed":S.ytErr_youtube_channel_changed,"youtube_confirm_missing":S.ytErr_youtube_confirm_missing,"youtube_macos_only":S.ytErr_youtube_macos_only,"youtube_stop_first":S.ytErr_youtube_stop_first,"youtube_no_client":S.ytErr_youtube_no_client,"youtube_connect_failed":S.ytErr_youtube_connect_failed,"youtube_connect_timeout":S.ytErr_youtube_connect_timeout,"youtube_scope_missing":S.ytErr_youtube_scope_missing,"youtube_no_channel":S.ytErr_youtube_no_channel,"youtube_secret_store_failed":S.ytErr_youtube_secret_store_failed,"youtube_connect_stale":S.ytErr_youtube_connect_stale});c.appendChild(el("div","err",Object.prototype.hasOwnProperty.call(reasons,y.result)?reasons[y.result]:S.ytOpFailed));}
 var row=el("div","btns");
 if(!y.connected){
  if(!y.bridge_off){var cn=el("button","btn sm pri",S.ytConnect);cn.type="button";
   cn.addEventListener("click",function(){ytPost({action:"connect"},cn).then(function(j){
    if(j&&typeof j.url==="string"&&/^https:\/\//i.test(j.url)){openSafe(j.url);toast(S.ytOpenBrowser);}cn.disabled=false;}).catch(function(){});});
   row.appendChild(cn);}
  c.appendChild(row);return c;}
 c.appendChild(el("div","mt",S.ytConnectedAs));if(y.channel)c.appendChild(el("div","nm ch",iso(y.channel)));
 if(y.live_enabled===false)c.appendChild(el("div","err",S.ytNoLive));
 if(y.cleanup)c.appendChild(el("div","safe",S.ytCleanup));
 if(y.live){var since=new Date(y.since*1000).toLocaleTimeString(LANG,{hour:"2-digit",minute:"2-digit"});
  // numbers on their own line, never inside a Hebrew sentence
  c.appendChild(el("div","mt",S.ytLiveSince));c.appendChild(el("div","nm ch",since));
  if(y.hours_left!=null){if(y.hours_left<1)c.appendChild(el("div","safe",S.ytLeftLess));
   else{c.appendChild(el("div","mt",S.ytLeft));c.appendChild(el("div","nm ch",new Intl.NumberFormat(LANG,{maximumFractionDigits:0}).format(Math.floor(y.hours_left))));}}
  var sp=el("button","btn sm pri",S.ytStop);sp.type="button";sp.disabled=!!y.pending;
  sp.addEventListener("click",function(){ytPost({action:"stop"},sp).then(function(){haptic("n","success");ytLater();}).catch(function(){});});row.appendChild(sp);}
 else{if(y.ended)c.appendChild(el("div","safe",y.failed?S.ytFailed:S.ytEnded));
  if(!y.bridge_off){var g=el("div","group");g.appendChild(ytSwitch(y));c.appendChild(g);if(y.edition!=="mirror")c.appendChild(ytProjects(y));
   if(!y.enabled)c.appendChild(el("div","safe",S.ytNeedsEnable));
   if(ytArmed&&Date.now()<ytArmed.until)ytConfirm(c,y);
   else{var st=el("button","btn sm pri",S.ytStart);st.type="button";st.disabled=!y.enabled||y.live_enabled===false||!!y.pending;
    st.addEventListener("click",function(){ytPost({action:"arm"},st).then(function(j){ytArmed={token:j.armed,channel:j.channel,until:Date.now()+55000};render(last,true);}).catch(function(){});});
    row.appendChild(st);}}}
 if(y.mirror_owner_start){var mo=el("button","btn sm",S.ytMirrorOff);mo.type="button";mo.disabled=!!y.pending;
  mo.addEventListener("click",function(){ytPost({action:"mirror-off"},mo).then(ytLater).catch(function(){});});row.appendChild(mo);}
 else if(!y.live&&!y.bridge_off){var mi=el("button","btn sm",S.ytMirrorOn);mi.type="button";mi.disabled=!!y.pending||y.live_enabled===false;
  mi.addEventListener("click",function(){ytPost({action:"mirror-arm"},mi).then(function(j){ytArmed={token:j.armed,channel:j.channel,mirror:true,until:Date.now()+55000};render(last,true);}).catch(function(){});});row.appendChild(mi);}
 var dc=el("button","btn sm",S.ytDisconnect);dc.type="button";dc.disabled=!!y.pending||!!y.live;
 dc.addEventListener("click",function(){ytPost({action:"disconnect"},dc).then(function(){ytLater();}).catch(function(){});});row.appendChild(dc);
 c.appendChild(row);return c;}
// Keep one audio element outside the redrawn tab: state polls and tab switches never interrupt it.
var radioAudio=null,radioInfo=null,radioSourceExpiry=0,radioRefreshBusy=false,radioMessage="",radioRenewTimer=null,radioState="paused",radioAttempt=0;
function radioPlayerDraw(){
 var box=document.getElementById("radio-player");box.hidden=!radioInfo||!radioInfo.enabled||view!==home()||targetClosed;
 if(!radioAudio){radioAudio=el("audio");radioAudio.preload="none";radioAudio.setAttribute("playsinline","");
  var position=radioAudio.currentTime;
  radioAudio.addEventListener("timeupdate",function(){var next=radioAudio.currentTime;
   if(next>position&&!radioAudio.paused&&!radioAudio.ended&&!radioAudio.error){radioState="playing";radioPlayerDraw();}position=next;});
  ["play","playing","waiting","pause","ended","error"].forEach(function(event){radioAudio.addEventListener(event,function(){
   if(event==="pause"&&!radioAudio.paused)return; // an old source's queued pause after a fresh Play
   radioState=event==="play"?"waiting":event;position=radioAudio.currentTime;
   if(event==="pause"||event==="ended"||event==="error")radioAttempt++;
   if(event==="error"){radioMessage=S.radioPlayerError;radioSourceExpiry=0;}
   if(typeof myMusicDuck==="function")myMusicDuck(event==="play"||event==="playing"||event==="waiting");radioPlayerDraw();});});
  document.body.appendChild(radioAudio);}
 box.textContent="";var row=el("div","s-head"),name=el("div","nm",S.radio);row.appendChild(name);
 var live=!!radioInfo&&radioInfo.on,active=!radioAudio.paused&&!radioAudio.ended&&!radioAudio.error;
 var playing=active&&radioState==="playing",label=el("span","pill"+(playing?" on":""),
  playing?S.radioLive:radioState==="error"?S.radioPlayerError:active?S.radioPlayerWaiting:live?S.radioPlay:S.radioPlayerOff);
 label.setAttribute("role","status");row.appendChild(label);
 var button=el("button","btn sm",active?S.radioPause:S.radioPlay);button.type="button";
 button.disabled=!live&&!active;button.setAttribute("aria-pressed",String(playing));
 button.addEventListener("click",function(){
  if(!radioAudio.paused&&!radioAudio.ended&&!radioAudio.error){radioAttempt++;radioAudio.pause();radioPlayerDraw();return;}
  radioPlayerPlay(); // stays in the tap handler for iPhone's user-gesture requirement
 });row.appendChild(button);box.appendChild(row);
 if(radioInfo&&radioInfo.listen_link){var ls=el("button","btn sm",S.listenSafari);ls.type="button";ls.setAttribute("data-listen-safari","");
  ls.addEventListener("click",function(){listenSafari(ls);});box.appendChild(ls);}
 if(radioMessage)box.appendChild(el("div","safe",radioMessage));if(typeof v2RadioArtwork==="function")v2RadioArtwork();radioPlayerSchedule();}
// Listen in Safari (listen_link.py): the bridge makes a private link; Safari, not this Mini App, plays it, so the
// audio goes on in the background and on the lock screen. The page must open as a web page, never as Instant View.
function listenSafari(btn){
 if(btn.disabled)return;btn.disabled=true;haptic("tap");
 post("/api/listen-link",{action:"create"}).then(function(r){if(!r.ok)throw r.status;return r.json();}).then(function(j){
  var base=(typeof j.base==="string"&&/^https:\/\//i.test(j.base))?j.base.replace(/\/+$/,""):location.origin;
  if(typeof j.path!=="string"||!/^\/listen\/[A-Za-z0-9_-]{43}$/.test(j.path)||!/^https:\/\//i.test(base))throw 0;
  wa.openLink(base+j.path,{try_instant_view:false});
 }).catch(function(st){if(st===403)authFailed();toast(st===403?S.reopen:S.listenFailed,true);})
 .then(function(){btn.disabled=false;});}
// One tap in Settings closes every listening link (the bridge keeps only their hashes).
function listenRevokeRow(d){
 var l=d&&d.listen_link;if(!l||!l.active)return null;
 var c=el("div","card"),h=el("div","s-head"),nb=el("div","nm");nb.appendChild(el("div",null,S.listenRevoke));
 var hint=el("div","safe",S.listenRevokeHint);hint.style.margin="0";nb.appendChild(hint);h.appendChild(nb);
 var b=el("button","btn sm",S.listenRevoke);b.type="button";b.setAttribute("data-listen-revoke","");
 b.addEventListener("click",function(){if(b.disabled)return;b.disabled=true;haptic("tap");
  post("/api/listen-link",{action:"revoke"}).then(function(r){if(!r.ok)throw r.status;return r.json();}).then(function(){
   haptic("n","success");toast(S.listenRevoked);l.active=0;render(last,true);
  }).catch(function(st){b.disabled=false;toast(st===403?S.reopen:S.listenFailed,true);});});
 h.appendChild(b);c.appendChild(h);return c;}
// The paired widget devices (device_tokens.py): one tap revokes one; the bridge keeps only their hashes.
function devicesRow(d){
 var list=d&&d.widget_devices;if(!list||!list.length)return null;
 var c=el("div","card"),h=el("div","s-head"),nb=el("div","nm");nb.appendChild(el("div",null,S.devicesTitle));
 var hint=el("div","safe",S.devicesHint);hint.style.margin="0";nb.appendChild(hint);h.appendChild(nb);c.appendChild(h);
 var fmt=new Intl.DateTimeFormat(LANG,{dateStyle:"medium"});
 list.forEach(function(dev){
  var r=el("div","row"),body=el("div","body");body.appendChild(el("div","nm",String(dev.label||"")));
  body.appendChild(el("div","mt",S.devicesAdded+" "+fmt.format(new Date((dev.created||0)*1000))));r.appendChild(body);
  var b=el("button","btn sm",S.devicesRevoke);b.type="button";b.setAttribute("data-device-revoke",String(dev.id||""));
  b.addEventListener("click",function(){if(b.disabled)return;b.disabled=true;haptic("tap");
   post("/api/devices",{action:"revoke",id:String(dev.id||"")}).then(function(x){if(!x.ok&&x.status!==404)throw x.status;return x.json();}).then(function(j){
    haptic("n","success");toast(S.devicesRevoked);d.widget_devices=(j&&j.devices)||list.filter(function(o){return o.id!==dev.id;});render(last,true);
   }).catch(function(st){b.disabled=false;if(st===403)authFailed();toast(st===403?S.reopen:S.devicesFailed,true);});});
  r.appendChild(b);c.appendChild(r);});
 return c;}
function radioPlayerSchedule(){
 clearTimeout(radioRenewTimer);
 if(radioAudio&&!radioAudio.paused&&radioSourceExpiry)radioRenewTimer=setTimeout(radioPlayerRefresh,
  Math.max(15000,radioSourceExpiry*1000-Date.now()-120000));}
function radioPlayerSource(){
 if(!radioInfo||!radioInfo.on)return;
 // load() discards a paused live position even when the capability URL is unchanged (no-store).
 radioAudio.src="/radio/live.m3u8?token="+encodeURIComponent(radioInfo.token);radioSourceExpiry=radioInfo.expires;
 radioAudio.load();}
function radioPlayerPlay(){
 var attempt=++radioAttempt;radioMessage="";radioState="waiting";radioPlayerSource();
 var p=radioAudio.play();if(p)p.catch(function(){if(attempt!==radioAttempt)return;
  radioState="error";radioMessage=S.radioPlayerError;if(typeof myMusicDuck==="function")myMusicDuck(false);radioPlayerDraw();});radioPlayerDraw();}
function radioPlayerStop(){
 radioInfo=null;radioSourceExpiry=0;radioMessage="";radioState="paused";radioAttempt++;
 clearTimeout(radioRenewTimer);
 if(radioAudio){radioAudio.pause();radioAudio.removeAttribute("src");radioAudio.load();}
 document.getElementById("radio-player").hidden=true;}
function radioPlayerRefresh(){
 if(radioRefreshBusy)return;radioRefreshBusy=true;
 var attempt=radioAttempt;
 return post("/api/radio-player",{}).then(function(r){if(!r.ok)throw r.status;return r.json();}).then(function(info){
  radioInfo=info;
  if(!info.on){radioMessage="";radioSourceExpiry=0;radioAudio.pause();radioAudio.removeAttribute("src");radioAudio.load();}
  // Polls may renew an actively playing source, never resume an owner/system pause or a failed attempt.
  else if(attempt===radioAttempt&&!radioAudio.paused&&!radioAudio.ended&&!radioAudio.error&&
          radioState!=="error"&&radioSourceExpiry*1000<=Date.now()+120000)radioPlayerPlay();
  radioPlayerDraw();
 }).catch(function(st){if(st===403||st===503)radioPlayerStop();})
 .then(function(){radioRefreshBusy=false;radioPlayerSchedule();});}
function radioCard(r,music){
 var now=Date.now();
 // the switch asked for here is over once the state shows it finished (on/off as wanted, or a result of an
 // operation asked for at or after the click), or after a long timeout
 if(radioOp&&!r.pending&&(radioOp.want===r.on||(r.op_at||0)*1000>=radioOp.at-2000&&r.failed||now>=radioOp.until))radioOp=null;
 var busy=!!r.pending||!!radioOp;
 var c=el("div","card"),h=el("div","s-head"),l=el("div","lead"+(r.on?" ok":""));l.appendChild(icon("mic"));h.appendChild(l);
 var nb=el("div","nm");nb.appendChild(el("div",null,S.radio));var hint=el("div","safe",S.radioHint);hint.style.margin="0";nb.appendChild(hint);h.appendChild(nb);
 h.appendChild(el("span","pill"+(r.on?" on":""),r.on?S.radioOn:S.radioOff));c.appendChild(h);
 var voiceCap=r.listening_detection?r.tts_listening_minutes:r.tts_daily_minutes;
 if(r.tts_minutes_today===null)c.appendChild(el("div","safe",S.radioBudgetUnavailable));
 else if(typeof r.tts_minutes_today==="number"&&typeof voiceCap==="number"&&typeof r.tts_effective_minutes==="number"){var nf=new Intl.NumberFormat(LANG,{maximumFractionDigits:1});
  c.appendChild(el("div","safe",S.radioMinutes+": "+nf.format(r.tts_minutes_today)+" · "+S.radioConfiguredCap+": "+nf.format(voiceCap)+" · "+S.radioActiveCap+": "+nf.format(r.tts_effective_minutes)));}
 var listenerHint=!r.on?S.radioOff:r.listening?S.radioListening:!r.listening_detection?S.radioDetectionOff:r.listening_source==="unknown"?S.radioListenerUnknown:S.radioNotListening;
 if(r.on&&r.listening){var listenerSources=[];if((r.listening_source||"").indexOf("player")>=0)listenerSources.push(S.radioListenerPlayer);
  if((r.listening_source||"").indexOf("call")>=0)listenerSources.push(S.radioListenerCall);listenerHint+=" · "+listenerSources.join(" · ");}
 c.appendChild(el("div","safe",listenerHint));
 if(music){var mh=el("div","music-now"),mt=el("div","nm",S.musicNow),cur=music.now||{};mh.appendChild(mt);
  var title=el("div","safe",cur.id?([cur.title,cur.artist].filter(Boolean).join(" · ")):S.musicNone);title.dir="auto";mh.appendChild(title);
  if(cur.id){var mr=el("div","btns");[[S.musicLike,"like"],[S.musicSkip,"skip"]].forEach(function(a){var mb=el("button","btn sm",a[0]);mb.type="button";
   mb.addEventListener("click",function(){if(mb.disabled)return;mb.disabled=true;post("/api/radio-music",{action:a[1],id:cur.id}).then(function(x){if(!x.ok)throw x.status;if(a[1]==="skip")music.now={};haptic("n","success");render(last,true);}).catch(function(){mb.disabled=false;toast(S.failed,true);});});mr.appendChild(mb);});mh.appendChild(mr);}
  var sw=el("button","btn sm",music.enabled?S.on:S.off);sw.type="button";sw.setAttribute("role","switch");sw.setAttribute("aria-checked",String(!!music.enabled));
  sw.addEventListener("click",function(){sw.disabled=true;post("/api/setting",{name:"radio_music",value:!music.enabled}).then(function(x){if(!x.ok)throw x.status;music.enabled=!music.enabled;if(!music.enabled)music.now={};render(last,true);}).catch(function(){sw.disabled=false;toast(S.failed,true);});});mh.appendChild(sw);c.appendChild(mh);}
 if(r.on){c.appendChild(el("div","safe",r.group_live===false?S.radioModePrivate:S.radioModeGroup));
  if(typeof r.group_wanted==="boolean"&&r.group_wanted!==(r.group_live!==false))c.appendChild(el("div","safe",S.radioModeNext));}
 var b=el("button","btn sm"+(r.on?"":" pri"),r.on?S.radioStop:S.radioStart);b.type="button";
 if(busy){b.disabled=true;c.appendChild(el("div","safe",S.radioWait));}
 else if(r.failed)c.appendChild(el("div","err",S.failed));
 b.addEventListener("click",function(){if(radioOp||r.pending)return;haptic("tap");b.disabled=true;
  var want=!r.on;radioOp={want:want,at:Date.now(),until:Date.now()+90000};
  post("/api/radio",{on:want}).then(function(x){if(!x.ok)throw x.status;haptic("n","success");
    toast(S.radioWait);c.appendChild(el("div","safe",S.radioWait));setTimeout(load,8000);})
   .catch(function(st){radioOp=null;haptic("n","error");b.disabled=false;c.appendChild(el("div","err",st===403?S.reopen:S.failed));});});
 var row=el("div","btns");row.appendChild(b);var mine=el("button","btn sm",S.myMusic);mine.type="button";mine.addEventListener("click",myMusicOpen);row.appendChild(mine);c.appendChild(row);
 if(r.on&&!busy){var f=fold("say",S.radioSay),ta=el("textarea");ta.maxLength=600;ta.rows=2;ta.placeholder=S.radioSayPh;
  ta.value=notes.say||"";var fitSay=tune(ta,true);ta.addEventListener("input",function(){notes.say=ta.value;syncClose();});
  var sb=el("button","btn sm pri",S.radioSayBtn);sb.type="button";
  sb.addEventListener("click",function(){var text=ta.value.trim();if(!text)return;haptic("tap");sb.disabled=true;
   post("/api/radio-say",{text:text}).then(function(x){if(!x.ok)throw x.status;haptic("n","success");notes.say="";ta.value="";fitSay();syncClose();toast(S.radioSaid);})
    .catch(function(st){haptic("n","error");toast(st===409?S.radioIsOff:st===403?S.reopen:S.failed,true);})
    .then(function(){sb.disabled=false;});});
  f.body.appendChild(ta);var sr=el("div","btns");sr.appendChild(sb);f.body.appendChild(sr);c.appendChild(f);}
 return c;}
var programUi=null;
function programPost(body){return post("/api/radio-programs",body).then(function(r){if(!r.ok)throw r.status;return r.json();});}
function programCard(){var c=el("div","card program-card");c.appendChild(el("div","safe",S.programHint));
 if(!programUi){var loadBtn=el("button","btn sm",S.refresh);loadBtn.type="button";loadBtn.addEventListener("click",function(){loadBtn.disabled=true;programPost({action:"view"}).then(function(j){programUi=j.program;render(last,false);}).catch(function(){toast(S.failed,true);});});c.appendChild(loadBtn);return compactBlock("radio-programs",S.programs,null,c);}
 var presets=el("div","btns");Object.keys(programUi.presets||{}).forEach(function(name){var b=el("button","btn sm"+(programUi.preset===name?" pri":""),name);b.type="button";b.addEventListener("click",function(){programPost({action:"preset",preset:name}).then(function(j){programUi=j.program;render(last,false);});});presets.appendChild(b);});c.appendChild(presets);
 (programUi.blocks||[]).forEach(function(block,index){var row=el("div","program-block"),kinds=el("div","seg");["music","news"].forEach(function(k){var choose=el("button","btn sm"+(k===block.kind?" pri":""),k);choose.type="button";choose.setAttribute("aria-pressed",String(k===block.kind));choose.addEventListener("click",function(){block.kind=k;render(last,false);});kinds.appendChild(choose);});row.appendChild(kinds);var mins=el("input");mins.type="number";mins.min="1";mins.max="240";mins.value=block.minutes;mins.setAttribute("aria-label",S.programMinutes);mins.addEventListener("change",function(){block.minutes=Math.max(1,Math.min(240,Number(mins.value)||1));});row.appendChild(mins);var moves=el("div","btns");[[S.programUp,-1],[S.programDown,1]].forEach(function(x){var b=el("button","btn sm",x[0]);b.type="button";b.disabled=index+x[1]<0||index+x[1]>=programUi.blocks.length;b.addEventListener("click",function(){var other=index+x[1],v=programUi.blocks[index];programUi.blocks[index]=programUi.blocks[other];programUi.blocks[other]=v;render(last,false);});moves.appendChild(b);});row.appendChild(moves);c.appendChild(row);});
 var actions=el("div","btns");[[S.programSave,"save"],[S.programStart,"start"]].forEach(function(x){var b=el("button","btn sm"+(x[1]==="start"?" pri":""),x[0]);b.type="button";b.addEventListener("click",function(){b.disabled=true;programPost({action:x[1],preset:programUi.preset,blocks:programUi.blocks}).then(function(j){programUi=j.program;toast(x[0]);render(last,false);}).catch(function(){b.disabled=false;toast(S.failed,true);});});actions.appendChild(b);});c.appendChild(actions);
 return compactBlock("radio-programs",S.programs,null,c);}
// Official YouTube IFrame Player only: no audio is extracted or proxied.
var myMusicPlayer=null,myMusicApi=null,myMusicLoading=false,myMusicPreviousVolume=100,myMusicDucked=false;
function myMusicOpen(){var box=document.getElementById("my-music");box.hidden=false;box.textContent="";box.appendChild(el("div","nm",S.myMusic));box.appendChild(el("div","safe",S.myMusicHint));if(myMusicLoading)return;myMusicLoading=true;post("/api/my-music",{}).then(function(r){if(!r.ok)throw r.status;return r.json();}).then(function(j){if(!j.ok)throw 0;myMusicApi=j;myMusicDraw();}).catch(function(){box.appendChild(el("div","err",S.myMusicFailed));}).then(function(){myMusicLoading=false;});}
function myMusicDraw(){var box=document.getElementById("my-music");box.textContent="";box.appendChild(el("div","nm",S.myMusic));var list=(myMusicApi&&myMusicApi.playlists)||[];if(myMusicApi&&myMusicApi.liked_music&&myMusicApi.liked_music.length)list=[{title:S.myMusicLiked,id:"LM",item_count:myMusicApi.liked_music.length}].concat(list);if(!list.length){box.appendChild(el("div","safe",S.myMusicEmpty));return;}var picks=el("div","btns");list.forEach(function(p){var b=el("button","btn sm",p.title);b.type="button";b.dir="auto";b.addEventListener("click",function(){myMusicLoad(p.id);});picks.appendChild(b);});box.appendChild(picks);var stage=el("div","my-music-player");stage.id="my-music-player";box.appendChild(stage);var controls=el("div","btns");[[S.myMusicPlay,"playVideo"],[S.myMusicPause,"pauseVideo"],[S.myMusicNext,"nextVideo"]].forEach(function(x){var b=el("button","btn sm",x[0]);b.type="button";b.addEventListener("click",function(){if(myMusicPlayer&&myMusicPlayer[x[1]])myMusicPlayer[x[1]]();});controls.appendChild(b);});box.appendChild(controls);}
function myMusicLoad(id){function start(){myMusicDuck(false);myMusicPlayer=new YT.Player("my-music-player",{width:"100%",height:"240",host:"https://www.youtube-nocookie.com",playerVars:{listType:"playlist",list:id,playsinline:1},events:{onReady:function(){if(radioAudio&&!radioAudio.paused&&!radioAudio.ended&&!radioAudio.error)myMusicDuck(true);}}});}if(window.YT&&YT.Player){start();return;}window.onYouTubeIframeAPIReady=start;if(!document.querySelector('script[src="https://www.youtube.com/iframe_api"]')){var s=document.createElement("script");s.src="https://www.youtube.com/iframe_api";document.head.appendChild(s);}}
function myMusicDuck(on){if(on===myMusicDucked||!myMusicPlayer||!myMusicPlayer.setVolume)return;if(on){if(myMusicPlayer.getVolume)myMusicPreviousVolume=myMusicPlayer.getVolume();myMusicPlayer.setVolume(20);myMusicDucked=true;}else{myMusicPlayer.setVolume(myMusicPreviousVolume);myMusicDucked=false;}}
window.addEventListener("pagehide",function(){myMusicDuck(false);});
document.addEventListener("visibilitychange",function(){if(document.hidden)myMusicDuck(false);});
// --- the podcast queue (radio_podcast.py): what is on the air, transport, the queue, add by feed or link ------
var podUi={feed:"",eps:null,loading:false,link:"",busy:false};
function podPost(body){return post("/api/podcast",body).then(function(r){return r.json().catch(function(){return {};}).then(function(j){
 if(!r.ok)throw {status:r.status,error:j.error};return j;});});}
var PODERR={bad:S.podErr_bad,url:S.podErr_url,fetch:S.podErr_fetch,feed:S.podErr_feed,too_big:S.podErr_too_big,unknown:S.podErr_unknown,
 full:S.podErr_full,dup:S.podErr_dup,unsuitable:S.podErr_unsuitable,explicit:S.podErr_explicit,rules:S.podErr_rules,notext:S.podErr_notext,decode:S.podErr_decode};
function podFail(e){haptic("n","error");toast(e&&e.status===403?S.reopen:(e&&PODERR[e.error])||S.failed,true);}
function podClock(n){n=Math.max(0,Math.floor(n||0));var s=n%60;return nf.format(Math.floor(n/60))+":"+(s<10?"0":"")+nf.format(s);}
function podDone(j){if(last&&j.podcast)last.podcast=j.podcast;podUi.busy=false;render(last,true);}
function podDo(body,after){if(podUi.busy)return;podUi.busy=true;haptic("tap");
 podPost(body).then(function(j){haptic("n","success");if(after)after(j);podDone(j);}).catch(function(e){podUi.busy=false;podFail(e);render(last,true);});}
function podcastCard(p,r){
 var c=el("div","card pod"),now=p.now,live=now&&(now.state==="playing"||now.state==="paused"||now.state==="downloading");
 var nm=el("div","podnow");
 if(live&&now.state==="downloading"){nm.textContent=S.podLoading;}
 else if(live){var t=el("span",null,now.title||"");t.dir="auto";nm.appendChild(t);}
 else if(now&&now.state==="error"){nm.textContent=PODERR[now.error]||S.failed;}
 else nm.textContent=(p.queue.length&&!r.on)?S.podRadioOff:S.podIdle;
 c.appendChild(nm);
 if(live&&now.state!=="downloading"){var sub=[];if(now.feed)sub.push(now.feed);
  sub.push(podClock(now.position)+(now.total?" / "+podClock(now.total):""));c.appendChild(el("div","podsub",sub.join(" · ")));}
 var row=el("div","btns"),go=!!(live||p.queue.length);
 function tb(label,body,pri){var b=el("button","btn sm"+(pri?" pri":""),label);b.type="button";b.disabled=!go||podUi.busy;
  b.addEventListener("click",function(){podDo(body);});row.appendChild(b);}
 if(p.paused)tb(S.podPlay,{action:"control",op:"play"},true);else tb(S.podPause,{action:"control",op:"pause"});
 tb(S.podSkip,{action:"control",op:"skip"});tb(S.podStop,{action:"control",op:"stop"});c.appendChild(row);
 if(p.queue.length){c.appendChild(el("div","safe",S.podQueue+" · "+nf.format(p.queue.length)));
  p.queue.forEach(function(q){var pr=el("div","podrow"),t=el("span",null,q.title||"");t.dir="auto";pr.appendChild(t);
   var rb=el("button","btn sm",S.podRemove);rb.type="button";rb.disabled=podUi.busy;
   rb.addEventListener("click",function(){podDo({action:"remove",id:q.id});});pr.appendChild(rb);c.appendChild(pr);});}
 var f=fold("podadd",S.podAdd);
 if((p.feeds||[]).length){
  if(podUi.eps===null&&!podUi.loading){f.body.appendChild(el("div","safe",S.podFeeds));var sg=el("div","seg");
   p.feeds.forEach(function(name){var b=el("button","btn sm",name);b.type="button";b.dir="auto";
    b.addEventListener("click",function(){podUi.feed=name;podUi.loading=true;haptic("tap");render(last,true);
     podPost({action:"episodes",feed:name}).then(function(j){podUi.eps=j.episodes||[];podUi.loading=false;render(last,true);})
      .catch(function(e){podUi.loading=false;podUi.eps=null;podFail(e);render(last,true);});});sg.appendChild(b);});f.body.appendChild(sg);}
  else if(podUi.loading){f.body.appendChild(el("div","safe",S.podFetching));}
  else{var back=el("button","btn sm",S.podBack);back.type="button";
   back.addEventListener("click",function(){podUi.eps=null;render(last,true);});f.body.appendChild(back);
   if(!podUi.eps.length)f.body.appendChild(el("div","safe",S.podNoEpisodes));
   var list=el("div","podeps");podUi.eps.forEach(function(e){var b=el("button","btn",""),t=el("span",null,e.title);t.dir="auto";
    b.appendChild(t);if(e.seconds)b.appendChild(el("span","podsub","· "+podClock(e.seconds)));b.type="button";b.disabled=podUi.busy;
    b.addEventListener("click",function(){podDo({action:"add",feed:podUi.feed,episode:e.id},function(){toast(S.podAdded);podUi.eps=null;});});
    list.appendChild(b);});f.body.appendChild(list);}}
 var lr=el("div","podlink"),inp=el("input");inp.type="url";inp.dir="ltr";inp.maxLength=1000;inp.placeholder=S.podLinkPh;inp.value=podUi.link;
 inp.setAttribute("aria-label",S.podLinkPh);inp.addEventListener("input",function(){podUi.link=inp.value;});
 var ab=el("button","btn sm pri",S.podLinkBtn);ab.type="button";ab.disabled=podUi.busy;
 ab.addEventListener("click",function(){var v=inp.value.trim();if(!v)return;podDo({action:"add",link:v},function(){toast(S.podAdded);podUi.link="";});});
 lr.appendChild(inp);lr.appendChild(ab);f.body.appendChild(lr);c.appendChild(f);return c;}
// --- the brainstorming room (radio_room.py): open, follow, pause, sum up, send a task on ----------------------
var roomPick=null, roomForm={topic:"",project:"",minutes:10}, roomBusy=false;
function roomPost(body,btn,after){if(roomBusy)return;roomBusy=true;haptic("tap");if(btn)btn.disabled=true;
 post("/api/room",body).then(function(r){return r.json().catch(function(){return {};}).then(function(j){
   if(!r.ok)throw {status:r.status,error:j.error};haptic("n","success");if(last&&j.room){last.room=j.room;}if(after)after(j);render(last,true);});})
  .catch(function(e){haptic("n","error");if(btn)btn.disabled=false;var st=e&&e.status;
   toast(st===403?S.reopen:e&&e.error==="radio_off"?S.roomRadioOff:e&&e.error==="busy"?S.roomBusy:S.roomFailed,true);})
  .then(function(){roomBusy=false;});}
function roomSuggestions(d){var out=[],seen={};
 function add(text,topic){text=(text||"").trim();if(!text||seen[text]||out.length>=5)return;seen[text]=1;
  out.push({text:text.length>110?text.slice(0,109)+"\u2026":text,project:String(topic||"").split(":")[0].trim().slice(0,40)});}
 (d.decisions||[]).forEach(function(x){add(shown(x,"title"),x.topic);});
 (d.reminders||[]).forEach(function(x){add(shown(x,"text"),x.topic);});
 (d.tasks||[]).forEach(function(x){add(shown(x,"text"),x.topic);});
 return out;}
function roomUsage(r){var nf1=new Intl.NumberFormat(LANG,{maximumFractionDigits:1});
 return el("div","safe",S.roomMinutes+": "+nf1.format(r.tts_minutes_today||0)+" "+S.radioOf+" "+nf1.format(r.tts_daily_minutes||0));}
function roomCard(d){
 var r=d.room,x=r.room,c=el("div","card room");
 if(!x||x.status==="closed")return roomStart(d,c,r);
 if(x.status==="done"||x.status==="stopped")return roomSummary(d,c,r,x);
 var paused=x.status==="paused",lv=el("div","live"+(paused?" pz":""));lv.appendChild(el("i"));
 lv.appendChild(el("span",null,paused?S.roomPaused:S.roomOpen));c.appendChild(lv);
 var t=el("div","ttl",x.topic);t.dir="auto";c.appendChild(t);
 var rn=el("div","rnds"),idx=x.rounds.indexOf(x.round);
 x.rounds.forEach(function(k,i){var past=x.round==="summary"||(idx>=0&&i<idx);var p=el("span","rnd"+(k===x.round?" on":past?" past":""));
  if(past)p.appendChild(icon("check"));p.appendChild(el("span",null,x.round_names[k]));rn.appendChild(p);});c.appendChild(rn);
 var st=el("div","seats");
 x.seats.concat([]).forEach(function(s){var on=s.name===x.speaking;var row=el("div","seat"+(on?" on":""));row.appendChild(el("i"));
  var n=el("span",null,s.name+(on?" \u00b7 "+S.roomSpeaking:""));n.dir="auto";row.appendChild(n);st.appendChild(row);});
 c.appendChild(st);
 if((x.proposals||[]).length){c.appendChild(el("div","sub",S.roomProposals));var pr=el("div","props");
  x.proposals.forEach(function(p){var e=el("div",null,p);e.dir="auto";pr.appendChild(e);});c.appendChild(pr);}
 if(paused){var cm=composer("room",600,S.roomSayPh,function(text,done){
   post("/api/room",{action:"resume",text:text}).then(function(res){if(!res.ok)throw res.status;return res.json();})
    .then(function(j){haptic("n","success");done(true);if(j.room&&last){last.room=j.room;render(last,true);}})
    .catch(function(st){haptic("n","error");done(false);toast(st===403?S.reopen:S.roomFailed,true);});});
  cm.ta.setAttribute("aria-label",S.roomSayPh);c.appendChild(cm.row);
  var row=el("div","btns");var go=el("button","btn sm",S.roomResume);go.type="button";
  go.addEventListener("click",function(){roomPost({action:"resume"},go);});row.appendChild(go);c.appendChild(row);}
 else{var row2=el("div","btns col");var w=el("button","btn wait",x.pause_asked?S.roomWaitAsked:S.roomWait);w.type="button";w.disabled=!!x.pause_asked;
  w.prepend(icon("mic"));w.addEventListener("click",function(){roomPost({action:"pause"},w);});row2.appendChild(w);c.appendChild(row2);
  c.appendChild(el("div","safe",S.roomPauseNote));}
 var row3=el("div","btns");var sm=el("button","btn sm",S.roomSum);sm.type="button";
 sm.addEventListener("click",function(){roomPost({action:"summarize"},sm);});row3.appendChild(sm);
 var en=el("button","btn sm",S.roomEnd);en.type="button";en.addEventListener("click",function(){roomPost({action:"end"},en);});row3.appendChild(en);
 c.appendChild(row3);c.appendChild(el("div","safe",S.roomVoiceHint));c.appendChild(roomUsage(r));return c;}
function roomStart(d,c,r){
 var h=el("div","s-head"),l=el("div","lead");l.appendChild(icon("chat"));h.appendChild(l);
 var nb=el("div","nm");nb.appendChild(el("div",null,S.room));var hint=el("div","safe",S.roomHint);hint.style.margin="0";nb.appendChild(hint);
 h.appendChild(nb);c.appendChild(h);
 var sug=roomSuggestions(d);
 var cm=composer("roomTopic",200,S.roomOwnPh,function(text,done){roomForm.topic=text;done(false);go.click();});
 cm.sb.hidden=true;
 if(sug.length){c.appendChild(el("div","sub",S.roomSuggest));var ch=el("div","chips");
  sug.forEach(function(sg){var on=roomForm.topic===sg.text;var b=el("button","opt"+(on?" on":""));b.type="button";var sp=el("span",null,sg.text);sp.dir="auto";b.appendChild(sp);
   b.addEventListener("click",function(){haptic("sel");roomForm.topic=sg.text;roomForm.project=sg.project;notes.roomTopic=sg.text;render(last,true);});ch.appendChild(b);});
  c.appendChild(ch);}
 cm.ta.addEventListener("input",function(){roomForm.topic=cm.ta.value;roomForm.project="";});
 cm.ta.setAttribute("aria-label",S.roomOwnPh);c.appendChild(cm.row);
 c.appendChild(el("div","sub",S.roomLength));var sg2=el("div","seg three");
 (r.lengths||[5,10,15]).forEach(function(m){var on=roomForm.minutes===m;var o=el("button","opt"+(on?" on":""),S.roomMin.replace("#",nf.format(m)));o.type="button";
  o.addEventListener("click",function(){haptic("sel");roomForm.minutes=m;render(last,true);});sg2.appendChild(o);});c.appendChild(sg2);
 var row=el("div","btns"),go=el("button","btn pri",S.roomStart);go.type="button";
 go.addEventListener("click",function(){var topic=(cm.ta.value||roomForm.topic||"").trim();if(!topic)return;
  var body={action:"start",topic:topic,minutes:roomForm.minutes};if(roomForm.project)body.project=roomForm.project;
  roomPost(body,go,function(){delete notes.roomTopic;roomForm.topic="";roomForm.project="";syncClose();});});
 row.appendChild(go);c.appendChild(row);
 if(!d.radio.on)c.appendChild(el("div","safe",S.roomRadioOff));
 c.appendChild(roomUsage(r));return c;}
function roomSummary(d,c,r,x){
 var h=el("div","live pz");h.appendChild(icon("check"));h.appendChild(el("span",null,x.status==="stopped"&&!x.summary?S.roomStopped:S.roomSummary));c.appendChild(h);
 var t=el("div","ttl",x.topic);t.dir="auto";c.appendChild(t);var sm=x.summary||{};
 if(sm.decision){c.appendChild(el("div","sub",S.roomDecision));var dd=el("div","dec",sm.decision);dd.dir="auto";c.appendChild(dd);}
 var tasks=sm.tasks||[];c.appendChild(el("div","sub",S.roomTasks));
 if(!tasks.length)c.appendChild(el("div","safe",S.roomNoTasks));
 var live=(d.sessions||[]).filter(function(s){return s.state!=="ended";});
 tasks.forEach(function(tk,i){var row=el("div","task"),tx=el("div","tx",tk.text);tx.dir="auto";row.appendChild(tx);
  if(tk.sent){var sn=el("span","sent",S.roomSent+" "+tk.sent);sn.dir="auto";row.appendChild(sn);}
  else{var b=el("button","btn sm",S.roomSend);b.type="button";b.prepend(icon("send"));
   b.addEventListener("click",function(){haptic("tap");roomPick=roomPick===tk.id?null:tk.id;render(last,true);});row.appendChild(b);}
  c.appendChild(row);
  if(roomPick===tk.id&&!tk.sent){var pk=el("div","pick");pk.appendChild(el("div","sub",S.roomPick));
   if(!live.length)pk.appendChild(el("div","safe",S.roomNoSessions));
   live.slice(0,8).forEach(function(s){var o=el("button","opt");o.type="button";var sp=el("span",null,shown(s,"name"));sp.dir="auto";o.appendChild(sp);
    o.addEventListener("click",function(){roomPost({action:"send",room:x.id,task:tk.id,session:s.id},o,function(){roomPick=null;toast(S.roomSentHint);});});pk.appendChild(o);});
   var cn=el("button","link-btn",S.roomCancel);cn.type="button";cn.addEventListener("click",function(){roomPick=null;render(last,true);});pk.appendChild(cn);
   c.appendChild(pk);}});
 if(tasks.length)c.appendChild(el("div","safe",S.roomSentHint));
 if((sm.questions||[]).length){c.appendChild(el("div","sub",S.roomQuestions));
  sm.questions.forEach(function(q){var e=el("div","q",q);e.dir="auto";c.appendChild(e);});}
 var row=el("div","btns"),cl=el("button","btn sm",S.roomClose);cl.type="button";
 cl.addEventListener("click",function(){roomPost({action:"close"},cl);});row.appendChild(cl);c.appendChild(row);
 c.appendChild(roomUsage(r));return c;}
// --- "Settings" ------------------------------------------------------------------------------------------------
function modelCard(m){
 var c=el("div","card"),h=el("div","s-head"),l=el("div","lead blue");l.appendChild(icon("cpu"));h.appendChild(l);
 var nb=el("div","nm");nb.appendChild(el("div",null,S.defModel));var hint=el("div","safe",S.defModelHint);hint.style.margin="0";nb.appendChild(hint);
 h.appendChild(nb);c.appendChild(h);var g=el("div","seg");
 (m.choices||[]).forEach(function(o){var on=o.id===m.id;var b=el("button","opt"+(on?" on":""));b.type="button";b.disabled=modelBusy;
  if(on)b.appendChild(icon("check"));b.appendChild(el("span",null,iso(o.label)));
  b.addEventListener("click",function(){if(on||modelBusy)return;modelBusy=true;haptic("tap");
   g.querySelectorAll(".opt").forEach(function(x){x.disabled=true;});
   post("/api/model",{model:o.key}).then(function(r){
    if(r.ok){haptic("n","success");m.id=o.id;toast(S.modelSaved+" "+iso(o.label));}
    else{haptic("n","error");toast(r.status===403?S.reopen:r.status===409?S.settingsBroken:S.failed,true);}})
   .catch(function(){haptic("n","error");toast(S.failed,true);})
   .then(function(){modelBusy=false;var nc=modelCard(m);c.replaceWith(nc);});});
  g.appendChild(b);});
 c.appendChild(g);return c;}
var SWITCHES=["live_activity_cockpit","day_briefing","day_nudges","day_auto_plan","auto_cards","auto_account_switch","quota_auto_continue","emergency_credit","session_auto_revive","progress","progress_details","compact_actions","auto_topic_names","state_emoji","away","voice_confirm","voice_copilot","delivery_end","status_repost","agent_notices","agents_topic","general_offer","brief_home","app_wide_cockpit","app_summary_rows"];
var COMPACT=[0,200,250,300,400,600];
function setSetting(name,value,d){haptic("tap");
 post("/api/setting",{name:name,value:value}).then(function(r){if(!r.ok)throw r.status;return r.json();})
  .then(function(j){d.settings=j.settings||d.settings;var failed=j.profiles&&j.profiles.length&&j.profiles.every(function(p){return !p.ok;});haptic("n",failed?"error":j.partial?"warning":"success");toast(failed?S.failed:j.partial?S.partlySaved:S.saved,!!(failed||j.partial));render(last,true);})
  .catch(function(st){haptic("n","error");toast(st===403?S.reopen:S.failed,true);render(last,true);});}
function switchRow(k,d){var st=d.settings||{};var on=!!st[k];var r=el("div","row");var b=el("div","body");
 b.appendChild(el("div","nm",S["set_"+k]));b.appendChild(el("div","mt",S["hint_"+k]));r.appendChild(b);
 var sw=el("button","sw"+(on?" on":""));sw.type="button";sw.setAttribute("role","switch");sw.setAttribute("aria-checked",on?"true":"false");
 sw.setAttribute("aria-label",S["set_"+k]);sw.appendChild(el("i"));
 sw.addEventListener("click",function(){sw.disabled=true;sw.className="sw"+(on?"":" on");setSetting(k,!on,d);});r.appendChild(sw);return r;}
var ACTIVITY=["quiet","normal","full"];
var EFFORT=["low","medium","high","xhigh"];
function v2ModeRow(d){var mode=(d.settings||{}).ui_v2||"off",row=el("div","row col v2-mode-row"),text=el("div","body");text.appendChild(el("div","nm",V2.settings.title));text.appendChild(el("div","mt",V2.settings.hint));var buttons=el("div","seg");
 ["off","preview","on"].forEach(function(value){var b=el("button","opt"+(mode===value?" on":""),V2.settings[value]);b.type="button";b.setAttribute("aria-pressed",String(mode===value));b.setAttribute("data-v2-mode",value);
  b.addEventListener("click",function(){Array.prototype.forEach.call(buttons.children,function(x){x.disabled=true;});post("/api/setting",{name:"ui_v2",value:value}).then(function(r){if(!r.ok)throw r.status;return r.json();}).then(function(){load();}).catch(function(){toast(S.failed,true);render(last,true);});});buttons.appendChild(b);});text.appendChild(buttons);row.appendChild(text);return row;
}
function v2ReturnCard(d){
 if(!d.settings||d.settings.ui_v2!=="off")return null;
 var c=el("div","card v2-return"),title=el("div","nm",S.backToV2),b=el("button","btn sm pri",S.backToV2Button);b.type="button";
 b.addEventListener("click",function(){b.disabled=true;post("/api/setting",{name:"ui_v2",value:"on"}).then(function(r){if(!r.ok)throw r.status;return r.json();}).then(function(){load();}).catch(function(){toast(S.failed,true);b.disabled=false;});});
 c.appendChild(title);c.appendChild(b);return c;
}
function settingsBox(d){
 var st=d.settings||{},c=el("div","card group");
 c.appendChild(v2ModeRow(d));
 if("default_effort" in st){var er=el("div","row col"),eb=el("div","body");eb.appendChild(el("div","nm",S.set_default_effort));
  eb.appendChild(el("div","mt",S.hint_default_effort));if(st.default_effort==="mixed")eb.appendChild(el("div","mt",S.effort_mixed));var eg=el("div","seg");
  EFFORT.forEach(function(v){var on=st.default_effort===v,o=el("button","opt"+(on?" on":""),S["effort_"+v]);o.type="button";o.setAttribute("aria-pressed",on?"true":"false");
   o.addEventListener("click",function(){if(on)return;o.disabled=true;setSetting("default_effort",v,d);});eg.appendChild(o);});eb.appendChild(eg);er.appendChild(eb);c.appendChild(er);}
 if("activity" in st){var ar=el("div","row col"),ab=el("div","body");ab.appendChild(el("div","nm",S.set_activity));
  ab.appendChild(el("div","mt",S.hint_activity));var ag=el("div","seg three");
  ACTIVITY.forEach(function(v){var on=st.activity===v;var o=el("button","opt"+(on?" on":""),S["act_"+v]);o.type="button";
   o.addEventListener("click",function(){if(on)return;o.disabled=true;setSetting("activity",v,d);});ag.appendChild(o);});
  ab.appendChild(ag);ar.appendChild(ab);c.appendChild(ar);}
 if("voice_digest" in st){var dr=el("div","row col"),db=el("div","body");db.appendChild(el("div","nm",S.set_voice_digest));
  db.appendChild(el("div","mt",S.hint_voice_digest));var dg=el("div","seg");
  ["off","important","hourly","every_3_hours"].forEach(function(v){var on=st.voice_digest===v;
   var o=el("button","opt"+(on?" on":""),S["digest_"+v]);o.type="button";o.setAttribute("aria-pressed",on?"true":"false");
   o.addEventListener("click",function(){if(on)return;o.disabled=true;setSetting("voice_digest",v,d);});dg.appendChild(o);});
  db.appendChild(dg);dr.appendChild(db);c.appendChild(dr);}
 var switches=el("div"),keys=SWITCHES.filter(function(k){return k in st;});
 keys.forEach(function(k){switches.appendChild(switchRow(k,d));});
 if(keys.length>4)c.appendChild(compactBlock("settings-options",S.compactSettings,nf.format(keys.filter(function(k){return st[k];}).length)+" / "+nf.format(keys.length)+" "+S.compactEnabled,switches));else c.appendChild(switches);
 if("auto_compact" in st){var r=el("div","row col");var b=el("div","body");b.appendChild(el("div","nm",S.set_auto_compact));
  b.appendChild(el("div","mt",S.hint_auto_compact));var g=el("div","seg three");
  COMPACT.forEach(function(v){var on=st.auto_compact===v;var o=el("button","opt"+(on?" on":""),v?cf.format(v*1000):S.off);o.type="button";
   o.addEventListener("click",function(){if(on)return;o.disabled=true;setSetting("auto_compact",v,d);});g.appendChild(o);});
  b.appendChild(g);r.appendChild(b);c.appendChild(r);}
 if("cold_compact" in st){var cr=el("div","row col"),cb=el("div","body");cb.appendChild(el("div","nm",S.set_cold_compact));cb.appendChild(el("div","mt",S.hint_cold_compact));
  var cg=el("div","seg");["on","off"].forEach(function(v){var ctl=el("button","opt"+(st.cold_compact===v?" on":""),v==="on"?S.coldOn:S.off);ctl.type="button";
   ctl.addEventListener("click",function(){ctl.disabled=true;setSetting("cold_compact",v,d);});cg.appendChild(ctl);});cb.appendChild(cg);cr.appendChild(cb);c.appendChild(cr);}
 return c;}
function renderSettings(d){
 if(d.default_model)app.appendChild(enter(modelCard(d.default_model),"set-m"));
 if(d.settings){sec(S.bridge);app.appendChild(enter(settingsBox(d),"set-b"));}
 var lr=listenRevokeRow(d);if(lr)app.appendChild(enter(lr,"set-l"));
 var dr=devicesRow(d);if(dr)app.appendChild(enter(dr,"set-w"));
}
// --- cockpit apps: a private tool's own page in a sandboxed frame (cockpit_apps.py) ----------------------------
// The full Telegram login stays here. Frames get only a short-lived capability for their own app;
// the parent renews it and proxies their API calls with that same scoped capability.
var appFrames=Object.create(null), appAuth=Object.create(null), appUi={}, backWait={}, helloAt={}, APP_NAME=/^[a-z0-9-]{1,24}$/, APP_BODY=16384, APP_BADGE_MAX=999, BACK_WAIT=1200, HELLO_SAME_PAGE=2000;
function forgetAppAuth(name){var a=appAuth[name];if(a)clearTimeout(a.timer);delete appAuth[name];}
function appAuthError(e){if(e.status===403){authFailed();fail(S.reopen);}}
function getAppAuth(name,renew){
 var a=appAuth[name]||(appAuth[name]={});
 if(a.pending)return a.pending;
 if(!renew&&a.token&&a.expires*1000>Date.now()+5000)return Promise.resolve(a);
 a.pending=fetch("/api/app-token",{method:"POST",headers:{"X-Telegram-Init-Data":wa.initData,"Content-Type":"application/json"},body:JSON.stringify({app:name})})
  .then(function(r){if(r.status!==200){var e=new Error("app auth");e.status=r.status;throw e;}return r.json();})
  .then(function(j){if(appAuth[name]!==a)throw new Error("retired app");
   if(typeof j.token!=="string"||!j.token||typeof j.expires!=="number"||!isFinite(j.expires)||j.expires*1000<=Date.now())throw new Error("app auth");
   a.token=j.token;a.expires=j.expires;clearTimeout(a.timer);
   a.timer=setTimeout(function renew(){getAppAuth(name,true).then(function(v){toApp(name,{cockpit:"auth",token:v.token,expires:v.expires});},function(e){
    appAuthError(e);if(appAuth[name]===a)a.timer=setTimeout(renew,5000);});},
    Math.max(1000,(a.expires*1000-Date.now())*0.8));
   return a;})
  .then(function(v){a.pending=null;return v;},function(e){a.pending=null;throw e;});
 return a.pending;}
// What a frame may ask of the shell besides its API: fold the header and tabs (focus), take over the native back
// button (back; the app confirms each forwarded back with back_done), show a count on its tab (badge) and open this
// topic's chat (open_chat). Each message has a fixed shape: exactly its known fields, anything else is rejected, so
// an app never supplies markup, a URL or a style.
function appCmd(m){
 if(!m||typeof m!=="object"||Array.isArray(m))return null;
 var t=m.cockpit,keys=Object.keys(m);
 function only(list){return keys.every(function(k){return list.indexOf(k)>=0;});}
 if(t==="focus"||t==="back")return only(["cockpit","on"])&&typeof m.on==="boolean"?{t:t,on:m.on}:null;
 if(t==="badge")return only(["cockpit","n"])&&typeof m.n==="number"&&isFinite(m.n)&&Math.floor(m.n)===m.n&&m.n>=0&&m.n<=APP_BADGE_MAX?{t:t,n:m.n}:null;
 if(t==="open_chat"||t==="back_done")return only(["cockpit"])?{t:t}:null;
 return null;}
function uiOf(name){return appUi[name]||(appUi[name]={focus:false,back:false,badge:0});}
function activeApp(){var n=appOf(view);return n&&appFrames[n]?n:null;}
function appOf(v){return typeof v==="string"&&v.indexOf("app:")===0?v.slice(4):null;}
function appsOf(d){return ((d&&d.apps)||[]).filter(function(a){return a&&typeof a.name==="string"&&APP_NAME.test(a.name)
 &&typeof a.title==="string"&&Object.prototype.hasOwnProperty.call(ICONS,a.icon);});}
function appNamed(d,name){return name?appsOf(d).filter(function(a){return a.name===name;})[0]||null:null;}
function appTitle(a){return LANG==="he"&&a.title_he?a.title_he:a.title;}
function appTabs(d){return appsOf(d).map(function(a){return ["app:"+a.name,appTitle(a),a.icon];});}
function showApp(d){
 var box=document.getElementById("apps"),name=appOf(view),a=appNamed(d,name);
 Object.keys(appFrames).forEach(function(k){if(!appNamed(d,k)){if(appFrames[k].parentNode===box)box.removeChild(appFrames[k]);forgetAppAuth(k);delete appFrames[k];delete appUi[k];delete layoutSent[k];}else appFrames[k].hidden=k!==name;});
 box.hidden=!a;if(appErr)appErr.hidden=!a||appErr.getAttribute("data-app")!==name;if(!a||!wa||!wa.initData)return;
 box.setAttribute("aria-label",S.appFrame);
 if(!appFrames[name]){var f=document.createElement("iframe");f.className="appframe";
  f.setAttribute("sandbox","allow-scripts allow-forms");f.setAttribute("referrerpolicy","no-referrer");f.setAttribute("allow","fullscreen; autoplay");
  f.title=appTitle(a);f.addEventListener("load",function(){frameLoaded(name);});
  appFrames[name]=f;
  getAppAuth(name).then(function(a){if(appFrames[name]!==f)return;
   clearAppErr(name);f.appTokenExpires=a.expires;f.src="/app/"+name+"/?auth="+encodeURIComponent(a.token);f.hidden=appOf(view)!==name;box.appendChild(f);},function(e){
   if(appFrames[name]===f){delete appFrames[name];forgetAppAuth(name);appFailed(name,a,e);}appAuthError(e);});}}
// A token that cannot be minted must not leave an empty, silent tab: show the offline state with a retry.
// (A 403 is handled by authFailed: "reopen"). The next state poll retries by itself as well.
var appErr=null;
function clearAppErr(name){if(appErr&&appErr.getAttribute("data-app")===name){if(appErr.parentNode)appErr.parentNode.removeChild(appErr);appErr=null;}}
function appFailed(name,a,e){
 if(e&&e.status===403)return;
 if(appErr&&appErr.getAttribute("data-app")===name){appErr.hidden=appOf(view)!==name;return;}
 clearAppErr(appErr&&appErr.getAttribute("data-app"));
 var c=empty("chat",S.offline,"",false),b=el("button","btn sm pri",S.retry);b.type="button";
 c.className+=" apperr";c.setAttribute("data-app",name);c.setAttribute("role","alert");
 b.addEventListener("click",function(){if(last)showApp(last);});c.appendChild(b);
 c.hidden=appOf(view)!==name;appErr=c;document.getElementById("apps").appendChild(c);}
function frameName(win){var n=null;Object.keys(appFrames).forEach(function(k){if(appFrames[k].contentWindow===win)n=k;});return n;}
// The bottom safe-area inset an app must keep clear: only in focus mode, where the frame reaches the screen's edge
// (otherwise the page's own bottom padding already does).
function appLayout(name){var on=!!uiOf(name).focus,b=0;
 if(on){try{b=Math.max(0,Math.round(parseFloat(getComputedStyle(document.getElementById("probe")).paddingBlockEnd)||0));}catch(x){}}
 return {focus:on,inset:{bottom:b}};}
function toApp(name,msg){var f=appFrames[name];try{if(f&&f.contentWindow)f.contentWindow.postMessage(msg,"*");}catch(x){}}
var layoutSent={};  // what each frame was last told, so an unchanged layout is not sent again
function pushLayout(){var n=activeApp();if(!n)return;var l=appLayout(n),k=(l.focus?"1":"0")+":"+l.inset.bottom;
 if(layoutSent[n]===k)return;layoutSent[n]=k;toApp(n,{cockpit:"layout",focus:l.focus,inset:l.inset});}
// Header, tabs and refresh strip are shown or folded from the page's own state each time, never left behind by an app:
// switching tabs (or the app disappearing) restores them at once.
function applyChrome(){
 var n=activeApp(),on=!!(n&&uiOf(n).focus);
 document.body.classList.toggle("focus",on);
 ["tabs","ptr"].forEach(function(id){document.getElementById(id).hidden=on;});
 document.querySelector("header.top").hidden=on;document.getElementById("foot").hidden=on;
 if(n&&uiOf(n).back&&!backWait[n])appBackLayer(n,true);
 syncBack();pushLayout();}
function openChat(){var me=((last&&last.sessions)||[]).filter(function(s){return s&&s.here&&typeof s.url==="string";})[0];
 if(me&&/^https:\/\/t\.me\//i.test(me.url)){haptic("tap");openLink(me.url);}}
// The shell always owns an exit: an app's focus or back takeover never outlives a (re)loaded page, a forwarded back
// the app does not confirm in time, a second back press, or an expired login.
function resetApp(name,withBadge){var u=appUi[name];if(u){u.focus=false;u.back=false;if(withBadge)u.badge=0;}
 retireBack("app:"+name+"|app-back:"+name);
 clearTimeout(backWait[name]);delete backWait[name];delete layoutSent[name];}
function leaveApp(name){resetApp(name,false);
 if(appOf(view)===name){if(backDepth())backTap();else switchTo(home(),false);}else applyChrome();}
function appBackLayer(name,on){if(!on){retireBack(view+"|app-back:"+name);return;}
 var change=bindBackLayer("app-back:"+name,null,on,function(value){
 if(value)return;toApp(name,{cockpit:"back"});
 backWait[name]=setTimeout(function(){delete backWait[name];leaveApp(name);},BACK_WAIT);});change(on);}
function freshFrame(name){resetApp(name,true);if(last)tabs(last);applyChrome();}
function frameLoaded(name){var f=appFrames[name];
 // A user reload/navigation can reuse the frame's original, expired URL. Retry with the parent's current token
 // only after that navigation; routine token refresh never reloads an app or discards its unsaved state.
 if(f&&f.appTokenExpires&&f.appTokenExpires*1000<=Date.now()){
  getAppAuth(name).then(function(a){if(appFrames[name]!==f)return;
   f.appTokenExpires=a.expires;f.src="/app/"+name+"/?auth="+encodeURIComponent(a.token);},appAuthError);return;}
 if(Date.now()-(helloAt[name]||0)<HELLO_SAME_PAGE)return;freshFrame(name);}  // a hello just before is this same page's
function authFailed(){radioPlayerStop();Object.keys(appAuth).forEach(forgetAppAuth);Object.keys(appFrames).forEach(function(k){resetApp(k,false);});
 backEntries.forEach(function(e){e.active=false;});trimBack();
 document.getElementById("apps").hidden=true;view=home();applyChrome();}
function onAppMessage(e){
 // only one of our own sandboxed frames (opaque origin "null"), never another window or tab
 var name=frameName(e.source),m=e.data,w=e.source;
 if(!name||e.origin!=="null"||!m||typeof m!=="object"||!wa||!wa.initData)return;
 if(m.cockpit==="hello"){helloAt[name]=Date.now();freshFrame(name);var l=appLayout(name);  // a hello starts the page afresh
  getAppAuth(name).then(function(a){if(frameName(w)!==name)return;
   w.postMessage({cockpit:"hello",token:a.token,expires:a.expires,lang:LANG,dir:RTL?"rtl":"ltr",
   theme:document.documentElement.getAttribute("data-theme")||"",focus:l.focus,inset:l.inset},"*");},appAuthError);return;}
 if(m.cockpit!=="api"){
  var c=appCmd(m);if(!c)return;
  // a count on its tab is useful while another tab shows, so a badge is taken from any of our app frames
  if(c.t==="badge"){uiOf(name).badge=c.n;if(last)tabs(last);return;}
  // everything else changes the page around the app: only the visible frame is obeyed
  if(name!==activeApp())return;
  if(c.t==="focus"){uiOf(name).focus=c.on;applyChrome();}
  else if(c.t==="back"){uiOf(name).back=c.on;appBackLayer(name,c.on);}
  else if(c.t==="back_done"){clearTimeout(backWait[name]);delete backWait[name];if(uiOf(name).back)appBackLayer(name,true);}
  else if(c.t==="open_chat")openChat();
  return;}
 if(typeof m.id!=="string"&&typeof m.id!=="number")return;
 var reply=function(st,data){try{w.postMessage({cockpit:"api",id:m.id,status:st,data:data},"*");}catch(x){}};
 var body=null;try{body=JSON.stringify(m.body==null?{}:m.body);}catch(x){}
 if(!body||body.charAt(0)!=="{"||body.length>APP_BODY){reply(400,null);return;}
 getAppAuth(name).then(function(a){return fetch("/app/"+name+"/api",{method:"POST",headers:{"X-Cockpit-App-Token":a.token,"Content-Type":"application/json"},body:body});})
  .then(function(r){if(r.status===403){reply(403,null);authFailed();fail(S.reopen);return;}
   return r.json().then(function(j){reply(r.status,j);},function(){reply(r.status,null);});},function(e){reply(e.status||0,null);appAuthError(e);});
}
window.addEventListener("message",onAppMessage);
window.addEventListener("resize",pushLayout);
// --- tabs and the page around them -----------------------------------------------------------------------------
function home(){return v2Enabled(last)?"me":last&&last.topic?"here":"me";}
function switchTo(v,remember){if(backPending&&remember!==false&&!backReplay)return;
 if(view===v)return;var previous=view;if(moreOpen)setMore(false);
 var oldApp=appOf(previous);if(oldApp){retireBack(previous+"|app-back:"+oldApp);
  clearTimeout(backWait[oldApp]);delete backWait[oldApp];}
 if(remember!==false&&!backReplay){
  var at=backEntries.slice(0,backIndex).findIndex(function(e){return e.active&&e.tab;});
  if(at<0)pushBack(backTab(previous,v));
  else{var tab=backEntries[at];
   // Home layers precede the sole tab step. Retire other views' steps, preserving
   // their open state for adoption on return; switching tabs never undoes them.
   backEntries.slice(at+1,backIndex).forEach(function(e){e.active=false;});
   if(v===tab.home){travelBack(at);return;}tab.tab=v;
   // Retire the old tab's layers before rendering/adopting the destination's layers.
   if(backIndex>at+1){backSwitch=v;travelBack(at+1);return;}
   try{if(backHistory)history.replaceState(backState(backIndex),"");}catch(e){backHistory=false;}}}
 moreOpen=false;view=v;try{sessionStorage.setItem(TABKEY,v);}catch(e){}haptic("sel");render(last,true);window.scrollTo(0,0);}
function setMore(on){var sh=document.getElementById("more-tabs");
 if(!sh)return;var change=bindBackLayer("more",sh,!sh.hidden,function(value){var sheet=document.getElementById("more-tabs"),button=document.querySelector(".tab-more");
  moreOpen=value;if(sheet)sheet.hidden=!value;if(button)button.setAttribute("aria-expanded",String(value));});change(on);}
// one listener for the page: a tap outside the More sheet closes it
document.addEventListener("click",function(e){var sh=document.getElementById("more-tabs"),m=document.querySelector(".tab-more");
 if(sh&&!sh.hidden&&!sh.contains(e.target)&&!(m&&m.contains(e.target)))setMore(false);});
function tabs(d){
 if(v2Enabled(d)){v2Tabs(d);return;}
 var box=document.getElementById("tabs");box.textContent="";
 document.getElementById("sub").textContent=d.topic?shown(d.topic,"name"):"";
 if(view==="all")view="me";
 if(view==="here"&&!d.topic)view=null;
 if(!view||(["here","me","agents","settings"].indexOf(view)<0&&!appNamed(d,appOf(view))))view=home();
 var list=(d.topic?[["here",S.here]]:[]).concat([["me",S.tabMe],["agents",S.tabAgents],["settings",S.settings]]).concat(appTabs(d));
 if(d.brief)list.forEach(function(p){if(p[0]==="me")p[1]=S.tabBrief;});  // the Morning Brief is the home
 var t=el("div","tabs"),n=waitingCount(d),sheet=el("div","more-sheet"),overflow=list.length>5?list.slice(4):[];
 t.setAttribute("role","tablist");sheet.hidden=!moreOpen;sheet.setAttribute("aria-label",S.moreTabs);
 function badge(p){var an=appOf(p[0]);return p[0]==="me"&&view!=="me"?n:(an&&appUi[an]?appUi[an].badge:0);}
 function button(p){var b=el("button","tab"+(view===p[0]?" on":"")),ic=el("span","tab-icon"),count=badge(p);
  b.type="button";b.setAttribute("role","tab");b.setAttribute("aria-selected",view===p[0]?"true":"false");
  if(p[2])b.className+=" app";ic.appendChild(icon(p[2]||({here:"chat",me:"person",agents:"cpu",settings:"settings"})[p[0]]));
  if(count)ic.appendChild(el("span","tb",nf.format(count)));b.appendChild(ic);b.appendChild(el("span","tab-label",p[1]));
  b.addEventListener("click",function(){if(!sheet.hidden)setMore(false);switchTo(p[0]);});return b;}
 var more=el("button","tab tab-more"+(overflow.some(function(p){return p[0]===view;})?" on":""));
 (overflow.length?list.slice(0,4):list).forEach(function(p){t.appendChild(button(p));});
 if(overflow.length){var mi=el("span","tab-icon");mi.appendChild(icon("more"));
  var total=overflow.reduce(function(sum,p){return sum+badge(p);},0);if(total)mi.appendChild(el("span","tb",nf.format(total)));
  more.type="button";more.setAttribute("aria-expanded",String(moreOpen));more.setAttribute("aria-controls","more-tabs");
  more.appendChild(mi);more.appendChild(el("span","tab-label",S.moreTabs));
  more.addEventListener("click",function(){setMore(sheet.hidden);});
  sheet.id="more-tabs";overflow.forEach(function(p){sheet.appendChild(button(p));});t.appendChild(more);box.appendChild(sheet);}
 box.appendChild(t);}

function syncBack(){if(!ver("6.1")||!wa.BackButton)return;try{if(backDepth())wa.BackButton.show();else wa.BackButton.hide();}catch(e){}}
// Native BackButton clicks and OS/browser gestures both unwind through restoreBack/popstate.
function backTap(){haptic("sel");var n=activeApp();
 if(n&&backWait[n]){leaveApp(n);return;} // an unanswered app cannot hold a second press
 if(backPending)return;
 if(!backDepth()){syncBack();syncClose();if(wa&&wa.close)wa.close();else history.back();return;}
 var to=backIndex;while(to&&!backEntries[to-1].active)to--;to--;
 while(to&&!backEntries[to-1].active)to--;travelBack(to);}
function snapshot(d){
 var ss=d.sessions||[];return {t:Date.now(),d:(d.decisions||[]).map(function(x){return x.id;}),
  a:ss.map(function(s){return s.id;}),w:ss.filter(function(s){return s.state==="working";}).map(function(s){return s.id;}),
  e:ss.filter(function(s){return s.state==="ended";}).map(function(s){return s.id;})};}
function makeDigest(d){
 var old=null;try{old=JSON.parse(localStorage.getItem("ctb-panel-seen")||"null");}catch(e){}
 try{localStorage.setItem("ctb-panel-seen",JSON.stringify(snapshot(d)));}catch(e){}
 if(digest||!old||!old.t||Date.now()-old.t<120000)return;
 var has=function(list,id){return (list||[]).indexOf(id)>=0;},ss=d.sessions||[],items=[];
 var nd=(d.decisions||[]).filter(function(x){return !has(old.d,x.id);}).length;
 var done=ss.filter(function(s){return s.state!=="working"&&has(old.w,s.id);}).length;
 var ended=ss.filter(function(s){return s.state==="ended"&&has(old.a,s.id)&&!has(old.e,s.id);}).length;
 var started=ss.filter(function(s){return s.state!=="ended"&&!has(old.a,s.id);}).length;
 if(nd)items.push([nd,S.dNew1,S.dNew]);if(done)items.push([done,S.dDone1,S.dDone]);
 if(started)items.push([started,S.dStarted1,S.dStarted]);if(ended)items.push([ended,S.dEnded1,S.dEnded]);
 digest={age:(Date.now()-old.t)/1000,items:items};}
function status(kind){var st=document.getElementById("st");st.textContent="";
 if(kind==="busy"){var s=el("span","sp spin");s.appendChild(icon("refresh"));st.appendChild(s);st.appendChild(el("span",null,S.updating));}
 else if(kind==="off")st.appendChild(el("span",null,S.offline));
 else if(last)st.appendChild(el("span",null,S.updated+" "+new Date(last.now*1000).toLocaleTimeString(LANG,{hour:"2-digit",minute:"2-digit"})));}
function hookHealthCard(d){
 // Only a broken or just-restored connection is shown on every tab; the reload tool and its outcome live in Settings,
 // as one short line of counts (never a row per session).
 var h=d.hook_health;if(!h||h.ok&&!h.restored&&view!=="settings")return;
 var c=el("section","card hook-health");c.setAttribute("data-k","hook-health");c.appendChild(el("h2",null,S.hooksTitle));
 c.appendChild(el("p",null,!h.ok?S.hooksBroken:h.restored?S.hooksRestored:S.hooksHint));
 var job=h.reload||{};
 if(job.running)c.appendChild(el("p",null,S.hooksRunning));
 else if(job.error)c.appendChild(el("p",null,S.hooksReloadFailed));
 else if(h.reload){
  var n={r:(job.reloaded||[]).length,c:0,w:0,o:0};
  (job.skipped||[]).forEach(function(x){if(x.reason==="current"||x.reason==="not_live")n.c++;else if(x.reason==="busy"||x.reason==="background")n.w++;else n.o++;});
  var parts=[];[[n.r,S.hooksNReloaded],[n.c,S.hooksNCurrent],[n.w,S.hooksNBusy],[n.o,S.hooksNOther]].forEach(function(p){
   if(p[0])parts.push(p[1].replace("#",nf.format(p[0])));});
  c.appendChild(el("p","mt hook-sum",parts.join(" · ")||S.hooksNone));
 }
 var b=el("button","btn sm pri",h.ok?S.hooksReload:S.hooksRestore);b.type="button";b.disabled=!!job.running;
 b.addEventListener("click",function(){b.disabled=true;haptic("tap");post(h.ok?"/api/sessions-reload":"/api/hooks-restore",{}).then(function(r){
  if(r.status===403){authFailed();throw 403;}if(!r.ok)throw 0;load();
 }).catch(function(e){toast(e===403?S.reopen:S.failed,true);b.disabled=false;});});
 c.appendChild(b);app.appendChild(c);
}
// open team items nobody moved for hours (idle_lines.py): a count and up to five rows; nothing when none
function idleLines(d){var x=d.idle_lines;if(!x||!x.count||!(x.items||[]).length)return;
 sec(S.idleTitle,x.count,"hot");var g=el("div","card group idle-lines");g.setAttribute("data-k","idle");
 x.items.forEach(function(r){var m=[iso(r.project),"#"+r.id,S.idleHours.replace("#",nf.format(r.hours))];
  if(r.no_lead)m.push(S.idleNoLead);else if(r.nudged)m.push(S.idleNudged);
  g.appendChild(item("clock","warm",r,"title",m,"idle-line","idle-"+r.project+"-"+r.id));});
 if(x.count>x.items.length)g.appendChild(el("div","mt idle-more",S.idleMore.replace("#",nf.format(x.count-x.items.length))));
 app.appendChild(enter(g,"idle-lines"));}
// Polls must not replace the tree beneath a finger, a moving viewport, or an editor.
function cancelPanelRestore(){if(pendingRestore)pendingRestore.stop();}
function notePanelInteraction(e){
 if(e.type==="scroll"&&pendingRestore&&Math.abs(window.scrollY-pendingRestore.expectedY)<=4)return;
 lastInteraction=Date.now();cancelPanelRestore();}
["touchstart","touchmove","scroll","wheel"].forEach(function(type){window.addEventListener(type,notePanelInteraction,{passive:true});});
function panelEditing(){var a=document.activeElement;return !!(a&&app.contains(a)&&(a.matches("input,textarea,select")||a.isContentEditable));}
// The state response is the render contract.  Ignore only clocks which advance without changing the UI's meaning.
function volatileKey(k){return k==="age"||k==="now"||/_age$/.test(k)||/_ago$/.test(k)||/^server_(?:time|now)$/.test(k);}
function stableCopy(x){if(Array.isArray(x))return x.map(stableCopy);if(!x||typeof x!=="object")return x;var out={};
 Object.keys(x).sort().forEach(function(k){if(!(volatileKey(k)&&typeof x[k]==="number"))out[k]=stableCopy(x[k]);});return out;}
function fingerprint(d){try{return JSON.stringify(stableCopy(d));}catch(e){return null;}}
function updateRelativeTimes(previous){var elapsed=previous&&last&&previous.now&&last.now?Math.max(0,last.now-previous.now):0,ages=[];
 function collect(x){if(Array.isArray(x)){x.forEach(collect);return;}if(!x||typeof x!=="object")return;Object.keys(x).forEach(function(k){if(volatileKey(k)&&typeof x[k]==="number")ages.push(x[k]);else collect(x[k]);});}collect(previous||{});
 ages.sort(function(a,b){return b-a;});var changes=ages.map(function(age){return [ago(age),ago(age+elapsed)];}).filter(function(p){return p[0]!==p[1];});
 var walk=document.createTreeWalker(app,NodeFilter.SHOW_TEXT),n;while((n=walk.nextNode()))changes.forEach(function(p){if(n.nodeValue.indexOf(p[0])>=0)n.nodeValue=n.nodeValue.split(p[0]).join(p[1]);});
 app.querySelectorAll("[data-relative-age]").forEach(function(n){var age=Number(n.getAttribute("data-relative-age")),at=Number(n.getAttribute("data-relative-at"));if(Number.isFinite(age))n.textContent=(n.getAttribute("data-relative-prefix")||"")+ago(age+Math.max(0,(Date.now()-at)/1000))+(n.getAttribute("data-relative-suffix")||"");});}
function ensureStableKeys(){app.querySelectorAll("[data-k],[data-v2-decision],[data-project],[data-v2-idea],[data-topic-key],.srow[id],.card[id]").forEach(function(n){
  if(n.hasAttribute("data-key"))return;for(var i=0;i<n.attributes.length;i++){var a=n.attributes[i];if(a.name==="data-k"||a.name.indexOf("data-v2-")===0||a.name==="data-project"||a.name==="data-topic-key"){n.setAttribute("data-key",a.name+":"+a.value);return;}}if(n.id)n.setAttribute("data-key","id:"+n.id);});}
function keyedSnapshot(){ensureStableKeys();var out={};app.querySelectorAll(":scope > [data-key]").forEach(function(n){out[n.getAttribute("data-key")]=n;});return out;}
function reuseUnchangedKeys(old){app.querySelectorAll(":scope > [data-key]").forEach(function(n){var prior=old[n.getAttribute("data-key")];
 // Keep listeners, focus history, decoded images, and DOM identity when a card did not change in this response.
 if(prior&&prior.isEqualNode(n))n.replaceWith(prior);});}
function firstVisibleAnchor(){ensureStableKeys();var nodes=app.querySelectorAll("[data-key]");for(var i=0;i<nodes.length;i++){var r=nodes[i].getBoundingClientRect();if(r.bottom>0&&r.top<innerHeight)return {key:nodes[i].getAttribute("data-key"),top:r.top};}return null;}
function openState(){ensureStableKeys();var out={};app.querySelectorAll("[data-key]").forEach(function(n){var k=n.getAttribute("data-key"),e=n.querySelectorAll("details,[aria-expanded],.open");if(n.matches("details[open],[aria-expanded=true],.open"))out[k+"|root"]=1;e.forEach(function(x,i){if(x.matches("details[open],[aria-expanded=true],.open"))out[k+"|"+i]=1;});});return out;}
function restoreOpenState(state){app.querySelectorAll("[data-key]").forEach(function(n){var k=n.getAttribute("data-key"),all=n.querySelectorAll("details,[aria-expanded],.open");
 function set(x,on){if(x.tagName==="DETAILS")x.open=on;if(x.hasAttribute("aria-expanded"))x.setAttribute("aria-expanded",String(on));if(on)x.classList.add("open");}
 if(state[k+"|root"])set(n,true);all.forEach(function(x,i){if(state[k+"|"+i])set(x,true);});});}
function settleAndRestore(anchor,y,oldHeight,allowAnchor){
 if(!allowAnchor){app.style.minHeight="";return;}
 var token={expectedY:y,stop:stop},observer=null,quietTimer=null,deadline=null;
 var images=new AbortController(),waiting=0;
 pendingRestore=token;
 function stop(){
  if(observer)observer.disconnect();images.abort();clearTimeout(quietTimer);clearTimeout(deadline);
  if(pendingRestore===token){pendingRestore=null;app.style.minHeight="";}}
 function place(){
  if(pendingRestore!==token)return;
  if(Math.abs(window.scrollY-token.expectedY)>4){stop();return;}
  if(!anchor)return;
  var n=Array.from(app.querySelectorAll("[data-key]")).find(function(x){return x.getAttribute("data-key")===anchor.key;});if(!n)return;
  var delta=n.getBoundingClientRect().top-anchor.top;
  if(Math.abs(delta)>.1){window.scrollBy(0,delta);token.expectedY=window.scrollY;}}
 function settle(){
  place();
  if(pendingRestore!==token||waiting)return;
  clearTimeout(quietTimer);quietTimer=setTimeout(stop,350);}
 // One image can change the layout while another request is still pending.
 if(anchor){
  var options={once:true,signal:images.signal};
  app.querySelectorAll("img").forEach(function(img){
   if(img.complete)return;
   waiting++;
   function done(){waiting--;settle();}
   img.addEventListener("load",done,options);img.addEventListener("error",done,options);});
  if(typeof ResizeObserver!=="undefined"){observer=new ResizeObserver(settle);observer.observe(app);}}
 deadline=setTimeout(stop,2000);
 requestAnimationFrame(settle);}
function focusedEditor(){var a=document.activeElement;if(!a||!app.contains(a)||!panelEditing())return null;var path=[],n=a;while(n!==app){path.unshift(Array.prototype.indexOf.call(n.parentNode.children,n));n=n.parentNode;}return {path:path,value:a.value,start:a.selectionStart,end:a.selectionEnd};}
function restoreEditor(s){if(!s)return;var n=app;s.path.forEach(function(i){n=n&&n.children[i];});if(!n||!("value" in n))return;n.value=s.value;n.focus({preventScroll:true});if(n.setSelectionRange&&s.start!=null)n.setSelectionRange(s.start,s.end);}
function render(d,force){
 var newContent=renderNewContent;renderNewContent=false;
 var changedView=renderedView!==view,fp=fingerprint(d),poll=!force,now=Date.now();
 if(poll&&(panelEditing()||now-lastInteraction<800)&&(!renderDeferredSince||now-renderDeferredSince<10000)){last=d;if(!renderDeferredSince)renderDeferredSince=now;if(!renderDeferred){var delay=panelEditing()?Math.max(50,10000-(now-renderDeferredSince)):Math.max(50,810-(now-lastInteraction));renderDeferred=setTimeout(function(){renderDeferred=null;render(last,false);},delay);}return;}
 clearTimeout(renderDeferred);renderDeferred=null;renderDeferredSince=0;
 if(poll&&!changedView&&fp!==null&&fp===renderFingerprint){var previous=last;last=d;updateRelativeTimes(previous);if(typeof vcpSync==="function")vcpSync(d);return;}
 var y=window.scrollY,anchor=changedView||newContent?null:firstVisibleAnchor(),state=openState(),oldKeys=typeof keyedSnapshot==="function"?keyedSnapshot():{},oldHeight=app.getBoundingClientRect().height,editor=focusedEditor();
 if(pendingRestore)pendingRestore.stop();
 last=d;stagger=0;animateEntries=changedView||renderedView===null;if(changedView)app.style.minHeight="";else app.style.minHeight=Math.ceil(oldHeight)+"px";app.textContent="";
 v2Prepare(d);
 if(typeof vcpSync==="function")vcpSync(d);
 if(targetClosed){
  document.getElementById("radio-player").hidden=true;document.getElementById("tabs").textContent="";document.getElementById("apps").hidden=true;
  var c=empty("check",S.questionClosed,"",false),b=el("button","btn sm pri",S.showOpenQuestions);b.type="button";
  b.addEventListener("click",function(){targetClosed=false;target="";view="me";render(last,true);});c.appendChild(b);app.appendChild(c);retireMissingBackLayers();app.style.minHeight="";return;
 }
 tabs(d);radioPlayerDraw();
 if(view==="me"){var v2Return=v2ReturnCard(d);if(v2Return)app.appendChild(enter(v2Return,"v2-return"));}
 if(!v2Enabled(d))hookHealthCard(d);
 if(view==="here")renderHere(d);else if(v2Enabled(d)&&view==="agents")v2Projects(d);else if(view==="agents")renderAgents(d);
 else if(v2Enabled(d)&&view==="settings")v2Settings(d);else if(view==="settings")renderSettings(d);
 else if(v2Enabled(d)&&(view==="tools"||appOf(view)))v2Tools(d);else if(v2Enabled(d)&&view==="me")v2Now(d);
 else if(!appOf(view)){idleLines(d);if(d.brief)renderBrief(d);else renderAll(d);}
 if(v2Enabled(d)&&view!=="settings")hookHealthCard(d);if(view!=="settings"&&!appOf(view))deliveryReceipts(d,view==="here");
 showApp(d);document.getElementById("foot").textContent="";applyChrome();ensureStableKeys();if(typeof reuseUnchangedKeys==="function")reuseUnchangedKeys(oldKeys);restoreOpenState(state);
 restoreEditor(editor);renderedView=view;renderFingerprint=fingerprint(d);settleAndRestore(anchor,y,oldHeight,!changedView&&!newContent);retireMissingBackLayers();adoptBackLayers();syncBack();syncClose();
 // Deep links may position the first paint. A state poll must never invoke browser scrolling APIs.
 if(typeof renderHasPainted!=="undefined"&&!renderHasPainted&&targetPending&&scrollQuestion(target))targetPending=false;
 if(typeof renderHasPainted!=="undefined"&&!renderHasPainted&&typeof requestTargetPending!=="undefined"&&requestTargetPending&&requestTargetRow){var requestNode=Array.prototype.find.call(document.querySelectorAll("[data-request]"),function(n){return n.getAttribute("data-request")===requestTargetRow;});if(requestNode){var requestButton=requestNode.querySelector("button");if(requestButton&&!requestNode.classList.contains("open"))requestButton.click();requestNode.scrollIntoView({block:"center"});requestTargetPending=false;}}
 renderHasPainted=true;
}
if(window.__CTB_TEST__===true)window.__ctbTest=Object.freeze({fingerprint:fingerprint,render:render});
function fail(m,again){app.textContent="";var c=empty("chat",m,"",false);app.appendChild(c);
 if(again){var b=el("button","btn sm pri",S.retry);b.type="button";b.style.marginBlockStart="12px";
  b.addEventListener("click",function(){haptic("tap");load();});app.appendChild(b);}}
function load(){
 if(!wa||!wa.initData){fail(S.open);status("");return;}
 if(loading){reloadPending=true;return;}loading=true;status("busy");
 var rf=document.getElementById("rf");rf.className="icon-btn spin";
 fetch("/api/state",{method:"POST",headers:{"X-Telegram-Init-Data":wa.initData},body:"{}"})
  .then(function(r){if(r.status===403)throw 403;if(!r.ok)throw 0;return r.json();})
  .then(function(d){try{if(targetPending){if(questionAvailable(d,target))view="all";else{targetClosed=true;targetPending=false;}}
   if(typeof requestTargetPending!=="undefined"&&requestTargetPending===true){view="all";requestTargetPending="shown";resolveRequestTarget();}
   var ids=(d.decisions||[]).filter(function(x){return !x.stale&&!x.old;}).map(function(x){return x.id;}).concat((d.cards||[]).filter(function(x){return ASKS[x.kind]&&!x.ended&&!x.old;}).map(function(x){return x.id;}));
   // A rollback keeps the current v2 tab even if this response also brings a new question.
   if(priorWaiting&&!targetClosed&&!v2Enabled(d)&&!v2Enabled(last)){var fresh=ids.filter(function(id){return priorWaiting.indexOf(id)<0;});if(fresh.length){target=fresh[0];targetPending=true;view="all";}}priorWaiting=ids;
   try{if(window.parent!==window)window.parent.postMessage({cockpit:"badge",n:waitingCount(d)},"*");}catch(e){}
   makeDigest(d);render(d,false);radioPlayerRefresh();status("");
   clearTimeout(followTimer);
   if(d.pending&&!document.hidden){followups++;followTimer=setTimeout(load,followups<=10?2500:10000);}
   else if(!d.pending)followups=0;}catch(err){renderFailed(err);}})
  // A refused login asks to reopen; a reply that never arrived (a cut connection, a server error) is not "no access".
  .catch(function(e){if(e===403){authFailed();fail(S.reopen);status("");}else if(last){status("off");}else{fail(S.offline,true);status("");}})
  .then(function(){loading=false;rf.className="icon-btn";if(reloadPending){reloadPending=false;load();}});
}
// The state arrived but could not be drawn: say so (never "no access"), and report the error's type and the place in
// the page that threw it, once per distinct error. No message, no data.
var renderReported={};
function errorAt(err){var s=String((err&&err.stack)||""),m=/^\s*at (?:([\w$.]+) \()?[^\n]*?:(\d+):(\d+)\)?\s*$/m.exec(s)||/^([\w$.]*)@[^\n]*?:(\d+):(\d+)\s*$/m.exec(s);
 return m?(m[1]||"").slice(0,60)+":"+m[2]+":"+m[3]:"";}
function renderFailed(err){fail(S.renderFailed,true);status("");
 var name=String((err&&err.name)||"Error").replace(/[^A-Za-z0-9]/g,"").slice(0,40)||"Error",at=errorAt(err),k=name+" "+at;
 if(renderReported[k])return;renderReported[k]=true;
 try{post("/api/client-error",{name:/^[A-Za-z]/.test(name)?name:"Error",at:at}).catch(function(){});}catch(e){}}
// --- Telegram look, native buttons, refresh button and pull to refresh ----------------------------------------
var BG={light:"#eef3fb",dark:"#0a1120"};
function theme(){var sch="light";
 try{if(wa&&wa.colorScheme){sch=wa.colorScheme;document.documentElement.setAttribute("data-theme",sch);}
  else if(window.matchMedia("(prefers-color-scheme: dark)").matches)sch="dark";}catch(e){}
 if(!wa)return;var col=BG[sch]||BG.light;
 try{if(ver("6.9"))wa.setHeaderColor(col);else wa.setHeaderColor("secondary_bg_color");}catch(e){}
 try{if(ver("6.1"))wa.setBackgroundColor(col);}catch(e){}
 try{if(ver("7.10")&&wa.setBottomBarColor)wa.setBottomBarColor(col);}catch(e){}}
if(wa){try{wa.ready();wa.expand();}catch(e){}
 try{if(wa.disableVerticalSwipes)wa.disableVerticalSwipes();}catch(e){}
 theme();try{wa.onEvent("themeChanged",theme);}catch(e){}
 try{if(ver("6.1")&&wa.BackButton)wa.BackButton.onClick(backTap);}catch(e){}
 try{if(ver("7.0")&&wa.SettingsButton){wa.SettingsButton.onClick(function(){switchTo("settings");});wa.SettingsButton.show();}}catch(e){}}
var rfb=document.getElementById("rf");rfb.appendChild(icon("refresh"));rfb.setAttribute("aria-label",S.refresh);
rfb.addEventListener("click",function(){haptic("tap");followups=0;load();});
var ptr=document.getElementById("ptr"),y0=null,dy=0,PULL=90;
function ptrText(ready){ptr.textContent="";ptr.appendChild(icon("refresh"));ptr.appendChild(el("span",null,ready?S.release:S.pull));}
document.addEventListener("touchstart",function(e){y0=(window.scrollY<=0&&e.touches.length===1)?e.touches[0].clientY:null;dy=0;},{passive:true});
document.addEventListener("touchmove",function(e){if(y0==null)return;dy=e.touches[0].clientY-y0;if(dy<=0||window.scrollY>0){ptr.style.blockSize="";return;}
 ptr.className="drag"+(dy>PULL?" ready":"");ptrText(dy>PULL);ptr.style.blockSize=Math.min(56,dy*.45)+"px";},{passive:true});
document.addEventListener("touchend",function(){if(y0==null)return;var go=dy>PULL;y0=null;ptr.className="";ptr.style.blockSize="";
 if(go){haptic("tap");followups=0;load();}},{passive:true});
// refresh: often while the page is seen (more often while a session works), backing off while it is hidden
var POLL={work:10000,idle:20000,hidden:60000,most:300000},pollTimer=null,hiddenSince=0,miniActive=!wa||wa.isActive!==false;
function pollDelay(hidden,working,hiddenFor){
 if(hidden)return Math.min(POLL.most,POLL.hidden*Math.pow(2,Math.floor(Math.max(0,hiddenFor||0)/POLL.most)));
 return working?POLL.work:POLL.idle;}
function anyWorking(d){return !!d&&(d.sessions||[]).some(function(s){return s.state==="working";});}
function schedule(){clearTimeout(pollTimer);if(!miniActive)return;var h=!!document.hidden;if(!h)hiddenSince=0;else if(!hiddenSince)hiddenSince=Date.now();
 pollTimer=setTimeout(function(){load();schedule();},pollDelay(h,anyWorking(last),h?Date.now()-hiddenSince:0));}
document.addEventListener("visibilitychange",function(){if(!document.hidden){hiddenSince=0;load();}schedule();});
if(wa&&wa.onEvent){
 try{wa.onEvent("viewportChanged",pushLayout);}catch(e){}
 try{if(ver("8.0")){wa.onEvent("activated",function(){miniActive=true;hiddenSince=0;load();schedule();});
  wa.onEvent("deactivated",function(){miniActive=false;clearTimeout(pollTimer);});}}catch(e){}
 function safeX(){try{var a=wa.contentSafeAreaInset||{},b=wa.safeAreaInset||{};var x=Math.max(a["le"+"ft"]||0,a["ri"+"ght"]||0,b["le"+"ft"]||0,b["ri"+"ght"]||0);document.documentElement.style.setProperty("--hg-safe-x",x+"px");}catch(e){}}
 try{safeX();if(ver("8.0")){wa.onEvent("safeAreaChanged",pushLayout);wa.onEvent("contentSafeAreaChanged",pushLayout);wa.onEvent("safeAreaChanged",safeX);wa.onEvent("contentSafeAreaChanged",safeX);}}catch(e){}
}
load();schedule();
})();
</script>
</body>
</html>
"""


# Inline in the existing nonce-bearing blocks: no new script origin, stylesheet
# origin, inline handler, HTML injection, or weaker Content Security Policy.
_V2_ROOT = Path(__file__).parent
V2_STRINGS = json.loads((_V2_ROOT / "cockpit_v2_strings.json").read_text(encoding="utf-8"))
for _lang in ("he", "en"):
    STRINGS[_lang]["set_voice_copilot"] = V2_STRINGS[_lang]["voiceCopilot"]["setting"]
    STRINGS[_lang]["hint_voice_copilot"] = V2_STRINGS[_lang]["voiceCopilot"]["settingHint"]
_DAY_COPY = {
    "he": (("תדריך בוקר", "תדריך פרטי וכרטיס היום."), ("תזכורות לפני אירוע", "רדיו פרטי והתראה בטלפון, עשר דקות לפני."),
           ("תכנון יום אוטומטי", "מציע תוכנית בלבד; שומר רק אחרי אישור.")),
    "en": (("Morning briefing", "A private briefing and matching day card."), ("Next-up nudges", "Private radio and phone notice ten minutes before."),
           ("Automatic day planning", "Proposes only; saves after confirmation.")),
}
for _lang in ("he", "en"):
    for _key, (_name, _hint) in zip(("day_briefing", "day_nudges", "day_auto_plan"), _DAY_COPY[_lang]):
        STRINGS[_lang]["set_" + _key], STRINGS[_lang]["hint_" + _key] = _name, _hint
PAGE = PAGE.replace("</style>", (_V2_ROOT / "cockpit_v2.css").read_text(encoding="utf-8") + "\n"
                    + (_V2_ROOT / "cockpit_v2_knowledge.css").read_text(encoding="utf-8") + "\n"
                    + (_V2_ROOT / "cockpit_v2_addons.css").read_text(encoding="utf-8") + "\n"
                    + (_V2_ROOT / "cockpit_v2_packs.css").read_text(encoding="utf-8") + "\n"
                    + (_V2_ROOT / "cockpit_v2_schedule.css").read_text(encoding="utf-8") + "\n"
                    + (_V2_ROOT / "cockpit_v2_family.css").read_text(encoding="utf-8") + "\n"
                    + (_V2_ROOT / "cockpit_v2_workmap.css").read_text(encoding="utf-8") + "\n"
                    + (_V2_ROOT / "cockpit_v2_art.css").read_text(encoding="utf-8") + "\n"
                    + (_V2_ROOT / "cockpit_voice_copilot.css").read_text(encoding="utf-8") + "\n</style>", 1)
_V2_JS = (_V2_ROOT / "cockpit_v2.js").read_text(encoding="utf-8").replace(
    "__V2_STRINGS__", json.dumps(V2_STRINGS, ensure_ascii=False))
# The v2 copy table is a top-level var: it goes with the page's other vars, never between two functions, so a function
# sliced out of the page (the node harnesses do that) stays self-contained.
_V2_VAR = next(line for line in _V2_JS.split("\n") if line.startswith("var V2_COPY="))
_PF_VAR = 'var pf=new Intl.NumberFormat(LANG,{style:"percent",maximumFractionDigits:0});\n'
assert _PF_VAR in PAGE
_APP_SWITCHES_VAR = "var APP_SWITCHES=" + json.dumps(cockpit_v2.APP_SWITCHES, ensure_ascii=False, separators=(",", ":")) + ";"
PAGE = PAGE.replace(_PF_VAR, _PF_VAR + _V2_VAR + "\n" + _APP_SWITCHES_VAR + "\n", 1)
PAGE = PAGE.replace("\nfunction render(d,force){", "\n" + _V2_JS.replace(_V2_VAR + "\n", "", 1) + "\n"
                    + (_V2_ROOT / "cockpit_v2_screens.js").read_text(encoding="utf-8") + "\n"
                    + (_V2_ROOT / "cockpit_app_request.js").read_text(encoding="utf-8") + "\n"
                    + (_V2_ROOT / "cockpit_v2_knowledge.js").read_text(encoding="utf-8") + "\n"
                    + (_V2_ROOT / "cockpit_v2_addons.js").read_text(encoding="utf-8") + "\n"
                    + (_V2_ROOT / "cockpit_v2_packs.js").read_text(encoding="utf-8") + "\n"
                    + (_V2_ROOT / "cockpit_v2_schedule.js").read_text(encoding="utf-8") + "\n"
                    + (_V2_ROOT / "cockpit_v2_family.js").read_text(encoding="utf-8") + "\n"
                    + (_V2_ROOT / "cockpit_v2_workmap.js").read_text(encoding="utf-8") + "\n"
                    + (_V2_ROOT / "cockpit_v2_art.js").read_text(encoding="utf-8") + "\n"
                    + (_V2_ROOT / "cockpit_mac_tools.js").read_text(encoding="utf-8") + "\n"
                    + (_V2_ROOT / "cockpit_voice_copilot.js").read_text(encoding="utf-8") + "\nfunction render(d,force){", 1)

_ICONS_AT = PAGE.index("var ICONS={")
ICON_NAMES = frozenset(re.findall(r'([a-z]+):"M', PAGE[_ICONS_AT:PAGE.index("};", _ICONS_AT)]))  # what an app may use


def render_page(lang: str, nonce: str) -> bytes:
    s = STRINGS.get(lang) or STRINGS["en"]
    code = "he" if lang == "he" else "en"
    return (PAGE.replace("__LANG__", code).replace("__DIR__", "rtl" if code == "he" else "ltr")
            .replace("__TITLE__", s["title"]).replace("__NONCE__", nonce)
            .replace("__STRINGS__", json.dumps(s, ensure_ascii=False))).encode("utf-8")


def csp(nonce: str, frame_ancestors: str) -> str:
    return ("default-src 'none'; script-src https://telegram.org/js/telegram-web-app.js 'nonce-%s' https://www.youtube.com/iframe_api https://www.youtube.com/s/player/; style-src 'nonce-%s'; "
            "connect-src 'self'; media-src 'self'; img-src 'self' data:; frame-src 'self' https://www.youtube-nocookie.com; base-uri 'none'; form-action 'none'; "
            "frame-ancestors %s" % (nonce, nonce, frame_ancestors))


_IDLE_STRINGS = {  # the Cockpit's "Idle lines" block (idle_lines.py)
    "he": {"idleTitle": "קווים בלי תנועה", "idleHours": "# שעות בלי תנועה", "idleNudged": "הסשן המוביל קיבל תזכורת",
           "idleNoLead": "אין סשן מוביל פעיל", "idleMore": "ועוד #"},
    "en": {"idleTitle": "Idle lines", "idleHours": "No movement for #h", "idleNudged": "Lead session nudged",
           "idleNoLead": "No lead session running", "idleMore": "and # more"},
}
for _lang, _pairs in _IDLE_STRINGS.items():
    STRINGS[_lang].update(_pairs)
