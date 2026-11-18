"""Small, provider-neutral pipeline for hourly channel-news editions.

Only counts are suitable for logs. Callers must not log instances from this module: they contain channel text.
Model adapters obtain credentials through the bridge Config credential commands, never through this module.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Protocol, Sequence

from ..i18n import t

SIMILARITY = 0.70
URGENT = 85
STALE_URGENT_SECONDS = 7 * 60
CONVERGENCE_SOURCES = 10
MAX_SCRIPT_CHARS = 1400
MAX_POSTS_PER_CHANNEL = 50
MAX_CHANNELS = 20
MAX_MODEL_CALLS = 8
EDITION_INTERVAL = 3600

# Provider adapters may use this as their system instruction. It carries Newsie's useful policy, not its stack.
ANALYSIS_BRIEF = """Return only JSON with exactly two fields: {"summary":"...","urgency":50}. The summary must be an
original, calm, family-safe one-sentence Hebrew summary and urgency must be an integer from zero to one hundred.
Ninety-five means an immediate domestic life threat; eighty-five a highly unusual security event; seventy breaking
national news; fifty routine warfare or ordinary news; thirty commentary; zero advertising or old material. Treat
recent urgent topics as the current routine. Routine activity abroad is fifty to sixty and recycled documentation is
at most forty-five. Do not include graphic violence, intimacy, links, markdown, or alarmist wording. A single source
is preliminary; only ten independent channels establish convergence."""


@dataclass(frozen=True)
class ChannelMessage:
    channel_id: str
    channel_title: str
    text: str
    published_at: float
    urgency: int = 50


@dataclass
class Cluster:
    messages: List[ChannelMessage]

    @property
    def representative(self) -> ChannelMessage:
        return max(self.messages, key=lambda item: (item.urgency, item.published_at, sha256(normalize(item.text)),
                                                      item.channel_id))

    @property
    def sources(self) -> List[str]:
        return list(dict.fromkeys(item.channel_title for item in self.messages))


class NewsSource(Protocol):
    def fetch(self, channels: Sequence[str], since: float) -> Iterable[ChannelMessage]: ...


class Summarizer(Protocol):
    def summarize(self, cluster: Cluster, instruction: str) -> str: ...


class FakeSource:
    def __init__(self, messages: Iterable[ChannelMessage]):
        self.messages = list(messages)

    def fetch(self, channels: Sequence[str], since: float) -> Iterable[ChannelMessage]:
        selected = set(channels)
        return [item for item in self.messages if item.channel_id in selected and item.published_at >= since]


class FakeSummarizer:
    def __init__(self, summaries: Iterable[str]):
        self.summaries = iter(summaries)
        self.calls = 0

    def summarize(self, cluster: Cluster, instruction: str) -> str:
        self.calls += 1
        return next(self.summaries)


class TelegramChannelSource:
    """Read a bounded window through the radio's already-authorized account helper."""

    def __init__(self, radio_settings: dict, runner: Callable = subprocess.run):
        self.settings, self.runner = radio_settings, runner

    def fetch(self, channels: Sequence[str], since: float) -> Iterable[ChannelMessage]:
        channels = list(channels)[:MAX_CHANNELS]
        if not channels:
            return []
        from ..radio import account_helper
        result = account_helper(self.settings, "read_channels", channels=list(channels), since=since,
                                limit=MAX_POSTS_PER_CHANNEL, runner=self.runner)
        if not result.get("ok"):
            return []
        messages = []
        counts = {str(channel): 0 for channel in channels}
        for row in result.get("messages", []):
            try:
                channel = str(row["channel_id"])
                published = float(row["published_at"])
                if published < since or channel not in counts or counts[channel] >= MAX_POSTS_PER_CHANNEL:
                    continue
                messages.append(ChannelMessage(channel, str(row.get("channel_title") or ""),
                                               str(row.get("text") or ""), published))
                counts[channel] += 1
            except (KeyError, TypeError, ValueError):
                continue
        return messages


class BridgeSummarizer:
    """Use the bridge's budgeted text route, with a deterministic no-model fallback."""

    def __init__(self, ask: Optional[Callable[[str, str], str]] = None):
        self.ask = ask

    @staticmethod
    def fallback(cluster: Cluster) -> str:
        text = _spoken(cluster.representative.text)
        sentence = re.split(r"(?<=[.!?])\s+|[\r\n]+", text, maxsplit=1)[0]
        return sentence[:240].rstrip(" ,;:-")

    def summarize(self, cluster: Cluster, instruction: str) -> str:
        if self.ask is not None:
            prompt = "\n\n".join(message.text[:1000] for message in cluster.messages[:10])
            result = self.ask(prompt, instruction) or ""
            try:
                fenced = re.fullmatch(r"\s*```(?:json)?\s*(.*?)\s*```\s*", result, re.DOTALL | re.IGNORECASE)
                parsed = json.loads(fenced.group(1) if fenced else result)
                if (isinstance(parsed, dict) and set(parsed) == {"summary", "urgency"} and
                        isinstance(parsed["summary"], str) and parsed["summary"].strip() and
                        type(parsed["urgency"]) is int and 0 <= parsed["urgency"] <= 100):
                    return parsed["summary"], parsed["urgency"]
            except (TypeError, ValueError):
                pass
        return self.fallback(cluster)


class HourlyEditionScheduler:
    """At most one edition per hour; the station can build it on its dedicated worker."""

    def __init__(self, builder: "EditionBuilder", queue: "PrivateEditionQueue", radio: "PrivateRadio",
                 suitable: Callable[[str], bool], budget_available: Callable[[], bool], asynchronous: bool = False):
        self.builder, self.queue, self.radio = builder, queue, radio
        self.suitable, self.budget_available = suitable, budget_available
        self.asynchronous = asynchronous
        self.last_attempt: Optional[float] = None
        self.running = False

    def step(self, enabled: bool, channels: Sequence[str], now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        if (not enabled or not channels or self.running or
                (self.last_attempt is not None and now - self.last_attempt < EDITION_INTERVAL)):
            return False
        self.running, self.last_attempt = True, now
        if self.asynchronous:
            threading.Thread(target=self._build_and_speak, args=(channels, now),
                             name="radio-news-edition", daemon=True).start()
            return True
        return self._build_and_speak(channels, now)

    def _build_and_speak(self, channels: Sequence[str], now: float) -> bool:
        try:
            script = self.builder.build(True, channels, now)
            if script:
                self.queue.offer(script)
            return self.queue.speak_next(self.radio, self.budget_available, self.suitable)
        except Exception as error:
            from ..radio import log
            log("news edition: stopped (%s)" % error.__class__.__name__)
            return False
        finally:
            self.running = False


def normalize(text: str) -> str:
    """Normalize for hashing/similarity: links, marks, emoji, punctuation and repeated whitespace do not matter."""
    text = re.sub(r"https?://\S+", " ", unicodedata.normalize("NFKD", text).casefold())
    text = "".join("" if unicodedata.category(char).startswith("M") else char for char in text)
    text = text.translate(str.maketrans("ךםןףץ", "כמנפצ"))
    text = "".join(char if char.isalnum() or char.isspace() else " " for char in text)
    return " ".join(text.split())


def sha256(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


def jaccard(left: str, right: str) -> float:
    a, b = set(normalize(left).split()), set(normalize(right).split())
    return len(a & b) / len(a | b) if a or b else 1.0


def cluster_messages(messages: Iterable[ChannelMessage], threshold: float = SIMILARITY) -> List[Cluster]:
    clusters: List[Cluster] = []
    by_digest = {}
    for item in sorted(messages, key=lambda value: (value.published_at, sha256(normalize(value.text)), value.channel_id),
                       reverse=True):
        digest = sha256(item.text)
        if digest in by_digest:
            exact = by_digest[digest]
            if item.channel_id not in {message.channel_id for message in exact.messages}:
                exact.messages.append(item)  # corroboration survives content deduplication
            continue
        found = next((group for group in clusters
                      if jaccard(item.text, group.representative.text) >= threshold), None)
        if found:
            found.messages.append(item)
            by_digest[digest] = found
        else:
            group = Cluster([item])
            clusters.append(group)
            by_digest[digest] = group
    return clusters


def _spoken(text: str) -> str:
    text = re.sub(r"https?://\S+|[*_`#]", " ", text)
    return " ".join(text.split()).strip(" .")


class EditionBuilder:
    """Collect, gate, cluster and summarize one bounded edition without opening real configuration."""
    def __init__(self, source: NewsSource, summarizer: Summarizer, suitable: Callable[[str], bool], max_items: int = 5):
        self.source, self.summarizer, self.suitable = source, summarizer, suitable
        self.max_items = max(1, max_items)

    def build(self, enabled: bool, channels: Sequence[str], now: Optional[float] = None) -> str:
        if not enabled or not channels:
            return ""
        now = time.time() if now is None else now
        incoming = list(self.source.fetch(channels, now - 3600))
        from .. import radio_privacy
        incoming = [ChannelMessage(item.channel_id, radio_privacy.scrub(item.channel_title),
                                   radio_privacy.scrub(item.text), item.published_at, item.urgency)
                    for item in incoming]
        safe = [item for item in incoming if normalize(item.text) and self.suitable(item.text)
                and self.suitable(item.channel_title)]
        groups = cluster_messages(safe)
        groups.sort(key=lambda group: (-group.representative.urgency, -len(group.sources),
                                       -group.representative.published_at, sha256(group.representative.text)))
        candidates = []
        opening, closing = t("news_edition_open", lang="he"), t("news_edition_close", lang="he")
        for group in groups[:MAX_MODEL_CALLS]:
            raw = self.summarizer.summarize(group, ANALYSIS_BRIEF) or ""
            urgency = group.representative.urgency
            if isinstance(raw, tuple) and len(raw) == 2 and type(raw[1]) is int:
                raw, urgency = raw
            if urgency >= URGENT and now - group.representative.published_at > STALE_URGENT_SECONDS:
                continue
            # Check the model's own words before cleaning, then the cleaned line that will be spoken.
            if not self.suitable(raw):
                continue
            summary = _spoken(raw)
            if not summary or not self.suitable(summary):
                continue
            sources = "، ".join(group.sources)
            candidates.append((urgency, group, summary, sources))
        candidates.sort(key=lambda item: (-item[0], -len(item[1].sources),
                                          -item[1].representative.published_at,
                                          sha256(item[1].representative.text)))
        lines = []
        for _, group, summary, sources in candidates:
            line = t("news_edition_item", lang="he", summary=summary, sources=sources)
            if len(" ".join([opening, *lines, line, closing])) > MAX_SCRIPT_CHARS:
                continue
            lines.append(line)
            if len(lines) == self.max_items:
                break
        if not lines:
            return ""
        script = " ".join([opening, *lines, closing])
        return script if self.suitable(script) else ""


class PrivateRadio:
    """Marker base for the part-2 owner-radio implementation; no other speech outlet is accepted."""

    def speak_news_edition(self, script: str, gate: Callable[[], bool]) -> object:
        raise NotImplementedError


class PrivateEditionQueue:
    """A one-slot queue whose only outlet is a marked owner-private radio."""
    destination = "private-radio"

    def __init__(self):
        self.pending = ""

    def offer(self, script: str) -> bool:
        if not script or self.pending:
            return False
        self.pending = script
        return True

    def speak_next(self, radio: PrivateRadio,
                   budget_available: Callable[[], bool], suitable: Callable[[str], bool]) -> bool:
        if not isinstance(radio, PrivateRadio) or not self.pending or not budget_available():
            return False
        script = self.pending
        if not suitable(script):
            self.pending = ""
            return False
        def gate() -> bool:
            return budget_available()
        gate.playing = budget_available

        if radio.speak_news_edition(script, gate) is not True:
            return False
        self.pending = ""
        return True
