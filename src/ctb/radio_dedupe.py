"""Short-lived hashes of replies actually delivered to an alerts channel."""
import hashlib
import html
import re
import time
import unicodedata

from .config import locked_map, read_map, state_dir

WINDOW = 15 * 60


def normalized(text):
    text = html.unescape(re.sub(r'<[^>]*>', '', str(text)))
    text = ''.join(' ' if unicodedata.category(c)[0] in ('S', 'C') else c for c in text)
    return ' '.join(text.casefold().split()).strip()


def key(chat, text):
    return hashlib.sha256((str(chat) + '\0' + normalized(text)).encode()).hexdigest()


def reply_id(text):
    return hashlib.sha256(str(text).strip().encode()).hexdigest()


def id_key(chat, identity):
    return hashlib.sha256(("reply-id\0" + str(chat) + "\0" + identity).encode()).hexdigest()


def remember(chat, text, now=None, reply_id=""):
    now = time.time() if now is None else now
    if not normalized(text):
        return
    try:
        with locked_map(state_dir() / 'radio-reply-dedupe.json', keep=1000) as data:
            for k, at in list(data.items()):
                if not isinstance(at, (int, float)) or not 0 <= now - at <= WINDOW:
                    data.pop(k, None)
            data[key(chat, text)] = now
            if reply_id:
                data[id_key(chat, reply_id)] = now
            prefix, sep, rest = text.partition(":")
            if sep and len(prefix) <= 80 and "\n" not in prefix and rest.strip():
                data[key(chat, rest)] = now
    except OSError:
        pass


def duplicate(chat, text, now=None, reply_id=""):
    now = time.time() if now is None else now
    data = read_map(state_dir() / 'radio-reply-dedupe.json')
    keys = [key(chat, text)] + ([id_key(chat, reply_id)] if reply_id else [])
    return any(type(at) in (int, float) and 0 <= now - at <= WINDOW
               for at in (data.get(k) for k in keys))
