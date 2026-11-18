"""Listener-owned Telegram sessions for the Codex app-server."""
from __future__ import annotations

from dataclasses import dataclass, field
import html
import json
import logging
import queue
import re
import secrets
import threading
import time

from . import dialogs, framing, menus, policy
from .codex_app_server import APPROVAL_METHODS, Approval, CodexProtocol, CodexServer, NotDelivered, ProtocolError
from .config import home_chat, read_map, write_private
from .creds import is_secret
from .harness import SessionRef
from .i18n import t
from .mirror import Mirror
from .telegram import TelegramError
from .topics import set_state

LOG = logging.getLogger(__name__)
CARD_MARKER = "Codex · Request"
THREAD_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
CHOICES = {
    "accept": ("Allow once", "אישור חד־פעמי"),
    "acceptForSession": ("Allow for this session", "אישור לשיחה הזאת"),
    "grant": ("Grant for this turn", "אישור לתור הזה"),
    "decline": ("Decline", "דחייה"),
    "cancel": ("Cancel", "ביטול"),
}
REQUEST_LABELS = {
    "item/commandExecution/requestApproval": "Command approval",
    "item/fileChange/requestApproval": "File change approval",
    "item/permissions/requestApproval": "Permission grant",
    "mcpServer/elicitation/request": "MCP request",
}


def _path(server, thread_id):
    if not isinstance(thread_id, str) or not THREAD_ID.fullmatch(thread_id):
        raise ValueError("Expected a Codex thread UUID")
    return server.directory / "threads" / (thread_id + ".json")


def managed(thread_id):
    """A marker claims a thread even when its listener is disconnected."""
    try:
        return _path(CodexServer(), thread_id).is_file()
    except ValueError:
        return False


def _records(server):
    for path in (server.directory / "threads").glob("*.json"):
        if THREAD_ID.fullmatch(path.stem):
            data = read_map(path)
            if data:
                yield path.stem, data


def register(server, state):
    write_private(_path(server, state.thread_id), json.dumps({
        "model": state.model, "effort": state.effort, "updated": time.time(),
    }))


def _ref(thread_id):
    return SessionRef("codex", "default", thread_id).key


def total_tokens(usage):
    return (usage.get("total") or {}).get("totalTokens")


def sessions():
    """Metadata-only snapshot for Cockpit, keyed by the existing harness session key."""
    server = CodexServer()
    live = read_map(server.directory / "listener.json")
    generation = live.get("generation") if live.get("until", 0) > time.time() else None
    rows = {}
    for ident, rec in _records(server):
        connected = bool(generation and rec.get("generation") == generation)
        row = {k: rec.get(k) for k in ("model", "effort", "updated", "usage")}
        row["delivery_unconfirmed"] = bool(rec.get("unconfirmed"))
        row.update(status=rec.get("status") if connected else "unavailable")
        for action in ("stop", "answer"):
            row[action] = rec.get(action) if connected else None
        rows[_ref(ident)] = row
    return rows


@dataclass
class Card:
    protocol: CodexProtocol
    approval: Approval
    thread_id: str
    chat: int
    thread: int
    message: int
    choices: tuple
    typed_only: bool = False
    question: str = ""
    allow_text: bool = False


@dataclass
class RequestCards:
    approval: Approval
    answers: dict = field(default_factory=dict)
    cards: dict = field(default_factory=dict)
    complete: bool = False


class CodexBridge:
    def __init__(self, cfg, bot, topics, server=None):
        self.cfg, self.bot, self.topics = cfg, bot, topics
        self.server = server or CodexServer()
        self.protocol = None
        self.generation = None
        self.attached = set()
        self.events = queue.Queue(maxsize=2048)
        self.commands = queue.Queue(maxsize=256)
        self._command_worker = threading.Thread(target=self._command_loop, daemon=True,
                                                name="codex-bridge-commands")
        self._command_worker.start()
        self.cards = {}
        self.requests = {}
        self.progress = {}
        self.next_probe = 0.0
        self.unknown = {}
        self.file_changes = {}
        self._lock = threading.RLock()

    def _enabled(self):
        return self.cfg.codex_harness_enabled is True and self.cfg.ready and self.cfg.mirror and policy.on()

    def run(self, stop):
        try:
            while not stop.is_set():
                try:
                    if self._enabled():
                        self.tick()
                    else:
                        self.disconnect()
                except Exception:
                    LOG.warning("Codex listener connection or delivery failed")
                    self.disconnect()
                stop.wait(1 if self.protocol else 3)
        finally:
            self.disconnect()

    def _event(self, generation, method, params):
        # The socket reader must remain free to receive RPC responses.
        # Drop excess streaming deltas before any terminal event.
        if method == "item/agentMessage/delta" and self.events.qsize() >= 1024:
            return
        try:
            self.events.put_nowait((generation, method, params))
        except queue.Full:
            raise ProtocolError("Codex listener event buffer full") from None

    def _connect(self):
        self.server.ensure_started()
        generation = secrets.token_hex(12)
        self.protocol = self.server.connect(owner_id=self.cfg.owner_chat_id,
                                            on_event=lambda m, p: self._event(generation, m, p))
        self.generation = generation
        self.next_probe = time.monotonic() + 15

    def disconnect(self):
        with self._lock:
            protocol, self.protocol = self.protocol, None
            self.attached.clear()
            self.generation = None
            cards, self.cards = self.cards, {}
            self.requests.clear()
            progress, self.progress = list(self.progress.values()), {}
            self.unknown.clear()
            self.file_changes.clear()
        if protocol:
            protocol.close()
            self._lease()
        for card in cards.values():
            self._clear_buttons(card.chat, card.message)
        for turn_id, chat, mid, count in progress:
            self._clear_buttons(chat, mid)
        while True:
            try:
                self.events.get_nowait()
            except queue.Empty:
                break

    def _lease(self):
        write_private(self.server.directory / "listener.json", json.dumps({
            "generation": self.generation, "until": time.time() + 30,
        }))

    def tick(self):
        cutoff = time.monotonic() - 3600
        with self._lock:
            self.unknown = {key: value for key, value in self.unknown.items() if value[0] >= cutoff}
        records = dict(_records(self.server))
        if not records:
            return
        if self.protocol and self.protocol.closed.is_set():
            self._drain()
            self.disconnect()
        if not self.protocol:
            self._connect()
        if time.monotonic() >= self.next_probe:
            self.protocol.request("thread/loaded/list", {"limit": 1}, timeout=3)
            self.next_probe = time.monotonic() + 15
        self._lease()
        for thread_id, rec in records.items():
            if thread_id in self.attached or rec.get("retired"):
                continue
            if rec.get("retry", 0) > time.time():
                continue
            try:
                state = self.protocol.attach(thread_id, rec.get("effort"))
            except ProtocolError:
                if self.protocol.closed.is_set():
                    raise
                self._store(thread_id, status="unavailable", retry=time.time() + 30)
                continue
            self.attached.add(thread_id)
            self._store(thread_id, retry=0, stop=None, answer=None)
            if rec.get("active_turn") and rec["active_turn"] != state.active_turn_id and not rec.get("recover_turn"):
                self._store(thread_id, recover_turn=rec["active_turn"], recover_after=0)
            self._place(thread_id)
            self._sync_state(thread_id)
            if state.active_turn_id:
                self._progress(thread_id, state.active_turn_id)
        self._drain()
        self._expire_cards()
        for request_id, approval in self.protocol.approvals.copy().items():
            thread_id = approval.params.get("threadId")
            request = self.requests.get(request_id)
            if thread_id in self.attached and (request is None or not request.complete):
                try:
                    self._show_request(thread_id, approval)
                except (OSError, TelegramError, ProtocolError):
                    LOG.warning("Codex request card delivery failed")
        for thread_id in self.attached:
            rec = read_map(_path(self.server, thread_id))
            with self._lock:
                progress = self.progress.get(thread_id)
            if progress and progress[3] < 0 and rec.get("stop"):
                self._store(thread_id, stop=None)
            if not rec.get("topic"):
                self._place(thread_id)
            if rec.get("recover_turn") and rec.get("recover_after", 0) <= time.time():
                # Preserve the recovery target if a history or delivery call fails.
                self._store(thread_id, recover_after=time.time() + 30)
                if self._recover_reply(thread_id, rec["recover_turn"]):
                    self._store(thread_id, recover_turn=None, recover_after=0)

    def _recover_reply(self, thread_id, turn_id):
        """Replay only the turn known to be live before the disconnect."""
        try:
            turns = self.protocol.request("thread/turns/list", {
                "threadId": thread_id, "limit": 5, "itemsView": "full",
            }).get("data", [])
        except ProtocolError:
            if self.protocol.closed.is_set():
                raise
            return False
        for turn in turns:
            if turn["id"] != turn_id:
                continue
            if turn.get("status") != "completed":
                return turn.get("status") != "inProgress"
            messages = [item for item in turn["items"] if item["type"] == "agentMessage"]
            final = [item for item in messages if item.get("phase") == "final_answer"]
            if not final and messages and messages[-1].get("phase") is None:
                final = messages[-1:]
            return all(self._reply(thread_id, turn_id, item) for item in final)
        return True  # the target is outside the bounded history window

    def _drain(self):
        for _ in range(2048):
            try:
                generation, method, params = self.events.get_nowait()
            except queue.Empty:
                break
            if generation != self.generation:
                continue
            try:
                self._handle_event(method, params)
            except (OSError, TelegramError, ValueError, ProtocolError):
                LOG.warning("Codex event delivery failed")

    def _store(self, thread_id, **fields):
        path = _path(self.server, thread_id)
        rec = read_map(path)
        if any(rec.get(key) != value for key, value in fields.items()):
            rec.update(fields, updated=time.time())
            write_private(path, json.dumps(rec))
        return rec

    def _state(self, thread_id):
        state = self.protocol.threads.get(thread_id)
        if state and thread_id in self.attached:
            return state

    def _sync_state(self, thread_id):
        state = self._state(thread_id)
        if state:
            self._store(thread_id, status=state.status, model=state.model, effort=state.effort,
                        active_turn=state.active_turn_id, generation=self.generation,
                        **({"usage": state.usage} if state.usage else {}))

    def _title(self, ident):
        return f"Codex: {ident[:8]}"

    def _place(self, ident):
        key = _ref(ident)
        where = home_chat(self.cfg)
        rec = read_map(_path(self.server, ident))
        if rec.get("topic_chat") not in (None, where):
            return None
        found = self.topics.place_of(key)
        if found is None:
            self.topics.ensure(key, self._title(ident), thread=rec.get("topic"), chat=where)
            found = self.topics.place_of(key)
        if found and found.chat == where:
            self._store(ident, topic=found.thread, topic_chat=where)
            return found

    def _handle_event(self, method, params):
        ident = params.get("threadId")
        if ident not in self.attached:
            return
        if method in APPROVAL_METHODS:
            pass  # Published after draining item details and request resolutions.
        elif method == "turn/started":
            if self._place(ident) is not None:
                set_state(self.cfg, _ref(ident), "busy", bot=self.bot)
                self._progress(ident, params["turn"]["id"])
        elif method == "item/agentMessage/delta":
            with self._lock:
                active = self.progress.get(ident)
            if active and active[0] == params["turnId"] and active[3] >= 0:
                self._progress(ident, active[0], active[3] + 1)
            return
        elif method == "item/started":
            item = params["item"]
            if item.get("type") == "fileChange":
                paths = tuple(change["path"] for change in item.get("changes", []))
                self.file_changes[ident, params["turnId"], item["id"]] = paths
            return
        elif method == "item/completed":
            self.file_changes.pop((ident, params["turnId"], params["item"]["id"]), None)
            self._agent_item(ident, params)
        elif method == "turn/completed":
            turn_id = params["turn"]["id"]
            for key in [key for key in self.file_changes if key[:2] == (ident, turn_id)]:
                self.file_changes.pop(key)
            with self._lock:
                unknown = self.unknown.pop((ident, turn_id), None)
            if unknown and params["turn"].get("status") == "completed":
                self._reply(ident, turn_id, unknown[1])
            with self._lock:
                active = self.progress.get(ident)
            if active and active[0] == turn_id:
                with self._lock:
                    self.progress.pop(ident, None)
                self._finish_progress(active, params["turn"].get("status"))
                self._store(ident, stop=None)
            if not self._state(ident).active_turn_id:
                set_state(self.cfg, _ref(ident), "done", bot=self.bot)
        elif method in ("thread/closed", "thread/archived", "thread/deleted"):
            with self._lock:
                active = self.progress.pop(ident, None)
            if active:
                self._clear_buttons(active[1], active[2])
            self._store(ident, stop=None, retired=method != "thread/closed")
            self._expire_cards()
            set_state(self.cfg, _ref(ident), "done", bot=self.bot)
        elif method == "serverRequest/resolved":
            self._expire_cards()
        elif method not in ("thread/tokenUsage/updated", "thread/settings/updated", "thread/status/changed"):
            return
        self._sync_state(ident)

    def _agent_item(self, ident, params):
        item, turn_id = params["item"], params["turnId"]
        if item.get("type") != "agentMessage":
            return
        phase = item.get("phase")
        if phase == "final_answer":
            with self._lock:
                self.unknown.pop((ident, turn_id), None)
            self._reply(ident, turn_id, item)
        elif phase is None:
            with self._lock:
                self.unknown[ident, turn_id] = (time.monotonic(), item)
                if len(self.unknown) > 512:
                    oldest = min(self.unknown, key=lambda key: self.unknown[key][0])
                    self.unknown.pop(oldest)
        elif phase == "commentary" and self.cfg.codex_commentary:
            self._reply(ident, turn_id, item)

    def _reply(self, ident, turn_id, item):
        rec = read_map(_path(self.server, ident))
        seen = rec.get("seen", [])
        key = [turn_id, item["id"]]
        text = dialogs.clean(item.get("text", "")).strip()
        moderation = item.get("moderation") or {}
        if key in seen or not text or moderation.get("display"):
            return True
        if not self._enabled() or not self._place(ident):
            return False
        # Telegram has no idempotency key. Claim before sending so a restart
        # never repeats a reply whose delivery outcome is unknown.
        unconfirmed = (rec.get("unconfirmed", []) + [key])[-32:]
        self._store(ident, seen=(seen + [key])[-512:], unconfirmed=unconfirmed)
        tokens = total_tokens(self.protocol.threads[ident].usage)
        footer = f"Codex · {tokens:,} tokens" if type(tokens) is int else ""
        mirror = Mirror(self.cfg, self.bot, self.topics)
        sent = mirror.send_reply(self._title(ident), _ref(ident), "Codex\n\n" + text, footer=footer)
        if sent and not mirror.dropped:
            self._store(ident, unconfirmed=unconfirmed[:-1])
        else:
            LOG.warning("Codex reply delivery was not confirmed")
        return True

    def _progress(self, ident, turn_id, count=0):
        with self._lock:
            old = self.progress.get(ident)
            if old and old[0] == turn_id:
                if not count or old[3] != 0:
                    return
                self.progress[ident] = (*old[:3], count)
        if old and old[0] == turn_id:
            text = "Codex · " + ("כותב תשובה…" if self.cfg.ui_language == "he" else "Writing a reply…")
            self.bot.call("editMessageText", {"chat_id": old[1], "message_id": old[2],
                                              "text": text, "reply_markup": self._stop_markup()})
            return
        if old:
            self._clear_buttons(old[1], old[2])
        where = self._place(ident)
        if not where:
            return
        text = "Codex · " + ("עובד…" if self.cfg.ui_language == "he" else "Working…")
        mid = self._send(where, text, self._stop_markup(), silent=True)
        with self._lock:
            self.progress[ident] = (turn_id, where.chat, mid, 0)
        self._store(ident, stop=mid)

    def _finish_progress(self, progress, status):
        labels = {
            "completed": ("Done", "הסתיים"),
            "interrupted": ("Interrupted", "נעצר"),
            "failed": ("Failed", "נכשל"),
        }
        label = labels.get(status)
        if label:
            try:
                self.bot.call("editMessageText", {"chat_id": progress[1], "message_id": progress[2],
                                                  "text": "Codex · " + label[self.cfg.ui_language == "he"],
                                                  "reply_markup": {"inline_keyboard": []}})
                return
            except (TelegramError, OSError):
                pass
        self._clear_buttons(progress[1], progress[2])

    def _stop_markup(self):
        label = "עצירת תור" if self.cfg.ui_language == "he" else "Interrupt turn"
        return {"inline_keyboard": [[{"text": label, "callback_data": "cx:stop"}],
                                    menus.unbuttons_row(self.cfg.ui_language)]}

    def _send(self, where, text, markup, *, html=False, silent=False):
        params = {"chat_id": where.chat, "message_thread_id": where.thread, "text": text,
                  "disable_notification": silent}
        if markup.get("inline_keyboard"):
            params["reply_markup"] = markup
        if html:
            params["parse_mode"] = "HTML"
        mid = self.bot.call("sendMessage", params).get("message_id")
        if type(mid) is not int:
            raise ProtocolError("Codex card delivery not confirmed")
        return mid

    def _clear_buttons(self, chat, mid):
        try:
            self.bot.call("editMessageReplyMarkup", {"chat_id": chat, "message_id": mid,
                                                     "reply_markup": {"inline_keyboard": []}})
        except (TelegramError, OSError):
            pass

    def _show_request(self, thread_id, approval):
        if not self._enabled() or (place := self._place(thread_id)) is None:
            return
        with self._lock:
            request = self.requests.setdefault(approval.request_id, RequestCards(approval))
            posted = {card.question for card in request.cards.values()}
        params = approval.params
        if approval.method == "item/tool/requestUserInput":
            for question in params["questions"]:
                question_id = question["id"]
                if question_id in posted:
                    continue
                options = question.get("options") or []
                labels = tuple(option["label"] for option in options)
                detail = "\n".join([question["question"]] +
                                   [f"{option['label']} — {option['description']}" for option in options])
                if question.get("isSecret"):
                    self._show_card(request, place, "Sensitive input requires Codex", (), question_id, native=True)
                else:
                    self._show_card(request, place, detail, labels, question_id,
                                    allow_text=not labels or bool(question.get("isOther")))
        else:
            fields = ("command", "cwd", "reason", "grantRoot", "permissions",
                      "additionalPermissions", "networkApprovalContext", "serverName")
            paths = self.file_changes.get((thread_id, params.get("turnId"), params.get("itemId")), ())
            detail = "\n".join([REQUEST_LABELS[approval.method]] +
                               [str(params[k]) for k in fields if params.get(k)] + list(paths))
            explain = bool(params.get("command") or params.get("permissions") or paths)
            native = (not explain or bool(params.get("additionalPermissions") or params.get("networkApprovalContext")
                                          or params.get("grantRoot")))
            self._show_card(request, place, detail, approval.choices, "", permission=True, native=native)
        request.complete = True

    def _show_card(self, request, place, body, choices, question, *, permission=False,
                   allow_text=False, native=False):
        lang = self.cfg.ui_language
        visible = dialogs.clean(body)
        escaped_detail = html.escape(CARD_MARKER + "\n" + visible + "\n\n")
        sensitive = (native or visible != body or len(escaped_detail) + 1200 > dialogs.CARD_LIMIT or
                     any(len(choice) > 140 for choice in choices) or is_secret(body) or
                     bool(dialogs._SENSITIVE.search(body) or dialogs._SECRET_PATH.search(body)))
        typed = permission or allow_text or bool(dialogs._TAP_BLOCK.search(body))
        nonce = secrets.token_hex(10)
        labels = [dialogs.clean(CHOICES[x][lang == "he"] if permission else x) for x in choices]
        options = [dialogs.Option(i, label[:140]) for i, label in enumerate(labels, 1)]
        instruction = ("יש להשיב ישירות לכרטיס עם מספר הבחירה." if lang == "he"
                       else "Reply directly to this card with the option number.")
        if allow_text:
            instruction = ("יש להשיב ישירות לכרטיס עם התשובה." if lang == "he"
                           else "Reply directly to this card with your answer.")
        if sensitive:
            instruction = ("את האישור או התשובה יש לתת ב־Codex." if lang == "he"
                           else "Use Codex directly to approve or answer.")
        def render(is_sensitive):
            rendered_dialog = dialogs.Dialog(question="", options=options, sensitive=is_sensitive,
                                              perm=permission)
            return dialogs.card_text(rendered_dialog,
                                     CARD_MARKER + "\n" + visible[:2700] + "\n\n" + instruction,
                                     lang, proof={}, relay=False)

        text = render(sensitive)
        if not sensitive and html.escape(visible) not in text:
            sensitive = True
            instruction = ("את האישור או התשובה יש לתת ב־Codex." if lang == "he"
                           else "Use Codex directly to approve or answer.")
            text = render(True)
        buttons = []
        if not typed and not sensitive and len(options) <= 12:
            buttons = [[{"text": x.label[:64], "callback_data": f"cx:{nonce}:{x.num}"}] for x in options]
            if buttons:
                buttons.append(menus.unbuttons_row(lang))
        mid = self._send(place, text, {"inline_keyboard": buttons}, html=True)
        # For a sensitive approval, a bound typed reply may only decline or cancel.
        if sensitive:
            choices = tuple(choice if permission and choice in ("decline", "cancel") else None for choice in choices)
        card = Card(self.protocol, request.approval, request.approval.params["threadId"],
                    place.chat, place.thread, mid, choices, typed or sensitive, question, allow_text and not sensitive)
        with self._lock:
            self.cards[nonce] = card
            request.cards[nonce] = card
        self._store(card.thread_id, answer=mid)

    def _expire_cards(self):
        for ident, request in list(self.requests.items()):
            if self.protocol.approvals.get(ident) is request.approval:
                continue
            with self._lock:
                del self.requests[ident]
                for nonce in request.cards:
                    self.cards.pop(nonce, None)
            for card in request.cards.values():
                self._clear_buttons(card.chat, card.message)
                self._sync_state(card.thread_id)
                other = next((c.message for c in self.cards.values() if c.thread_id == card.thread_id), None)
                self._store(card.thread_id, answer=other)

    def _owner(self, actor, chat):
        return (self._enabled() and chat == home_chat(self.cfg) and
                type(actor) is int and actor == self.cfg.owner_chat_id)

    def _respond(self, card, answer, actor, *, tapped):
        approval, protocol = card.approval, card.protocol
        with self._lock:
            request = self.requests.get(approval.request_id)
            if (protocol is not self.protocol or request is None or request.approval is not approval
                    or protocol.approvals.get(approval.request_id) is not approval
                    or (tapped and card.typed_only) or card.question in request.answers):
                raise ProtocolError("Codex card is stale or already answered")
            if re.fullmatch(r"[0-9]{1,3}", answer) and 1 <= int(answer) <= len(card.choices):
                choice = card.choices[int(answer) - 1]
            elif card.question and card.allow_text and not tapped:
                choice = answer
            else:
                raise ProtocolError("Choose one of the offered answers")
            if not choice or is_secret(choice):
                raise ProtocolError("Answer unavailable on Telegram")
            if card.question:
                request.answers[card.question] = choice
                if request.answers.keys() != {question["id"] for question in approval.params["questions"]}:
                    return
                choice = request.answers
            self._queue_command(protocol.answer, approval.request_id, choice,
                                actor_id=actor, expected=approval,
                                on_failure=lambda error: self._answer_failed(request, approval, card))

    def _answer_failed(self, request, approval, card):
        with self._lock:
            if self.requests.get(approval.request_id) is not request or request.approval is not approval:
                return
            request.answers.clear()
        text = ("Codex · שליחת התשובה נכשלה. ניתן לנסות שוב."
                if self.cfg.ui_language == "he" else "Codex · Answer failed. Please try again.")
        try:
            self.bot.call("editMessageText", {"chat_id": card.chat, "message_id": card.message,
                                               "text": text})
        except (TelegramError, OSError):
            pass

    def _queue_command(self, function, *args, on_failure=None, **kwargs):
        try:
            self.commands.put_nowait((function, args, kwargs, on_failure))
        except queue.Full:
            raise ProtocolError("Codex command queue is full") from None

    def _command_loop(self):
        while True:
            function, args, kwargs, on_failure = self.commands.get()
            try:
                function(*args, **kwargs)
            except Exception as error:
                LOG.warning("Codex queued command failed")
                if on_failure:
                    try:
                        on_failure(error)
                    except Exception:
                        LOG.warning("Codex queued command failure notice failed")

    def callback(self, thread_id, q):
        message = q.get("message") or {}
        actor = (q.get("from") or {}).get("id")
        chat = (message.get("chat") or {}).get("id")
        thread, mid = message.get("message_thread_id"), message.get("message_id")
        if not self._owner(actor, chat):
            return False
        data = q.get("data", "")
        if data == "more:unbuttons":
            with self._lock:
                progress = self.progress.get(thread_id)
                if progress and progress[1:3] == (chat, mid):
                    self.progress[thread_id] = (*progress[:3], -1)
            if not progress or progress[1:3] != (chat, mid):
                card = self._card_at(thread_id, chat, thread, mid)
                if not card:
                    raise ProtocolError("Codex card is stale")
                card.typed_only = True
            self._clear_buttons(chat, mid)
            return True
        if data == "cx:stop":
            protocol = self.protocol
            with self._lock:
                progress = self.progress.get(thread_id)
                if (not protocol or not progress or progress[1:3] != (chat, mid)
                        or (place := self.topics.place_of(_ref(thread_id))) is None or place.thread != thread
                        or progress[3] < 0):
                    raise ProtocolError("Codex interrupt is stale")
                self.progress[thread_id] = (*progress[:3], -1)
            self._queue_command(protocol.interrupt, thread_id, progress[0])
            self._clear_buttons(chat, mid)
            return True
        match = re.fullmatch(r"cx:([0-9a-f]{20}):([0-9]{1,3})", data)
        card = self.cards.get(match[1]) if match else None
        if card is None or (card.thread_id, card.chat, card.thread, card.message) != (thread_id, chat, thread, mid):
            raise ProtocolError("Codex card is stale")
        self._respond(card, match[2], actor, tapped=True)
        return True

    def _card_at(self, thread_id, chat, thread, mid):
        return next((c for c in self.cards.copy().values()
                     if (c.thread_id, c.chat, c.thread, c.message) == (thread_id, chat, thread, mid)), None)

    def answer_message(self, thread_id, message):
        reply = message.get("reply_to_message") or {}
        actor = (message.get("from") or {}).get("id")
        chat = (message.get("chat") or {}).get("id")
        thread = message.get("message_thread_id")
        mid = reply.get("message_id")
        if not self._owner(actor, chat) or type(mid) is not int:
            return False
        card = self._card_at(thread_id, chat, thread, mid)
        if card is None:
            # A reply to an expired card must never turn into an actionable prompt.
            if reply.get("from", {}).get("is_bot") and CARD_MARKER in (reply.get("text") or ""):
                raise ProtocolError("Codex card is stale")
            return False
        answer = (message.get("text") or "").strip()
        if not answer or len(answer) > 2000:
            raise ProtocolError("Codex card answer is unavailable")
        self._respond(card, answer, actor, tapped=False)
        return True

    def _undelivered(self, chat, message, error):
        """Reply to the owner's message when queued work did not reach the session: certainly not, after a
        refusal; perhaps not, after a timeout or a lost connection, since the server may have taken it."""
        if not message or type(message.get("message_id")) is not int:
            return
        key = "codex_not_delivered" if isinstance(error, (NotDelivered, KeyError)) else "codex_maybe_not_delivered"
        params = {"chat_id": chat, "text": t(key, lang=self.cfg.ui_language),
                  "reply_parameters": {"message_id": message["message_id"], "allow_sending_without_reply": True}}
        if message.get("message_thread_id"):
            params["message_thread_id"] = message["message_thread_id"]
        try:
            self.bot.call("sendMessage", params)
        except (TelegramError, OSError):
            LOG.warning("Codex delivery failure notice failed")

    def send(self, thread_id, items, actor, chat, message=None):
        protocol = self.protocol
        if not self._owner(actor, chat) or not protocol or thread_id not in self.attached:
            raise ProtocolError("Codex listener is unavailable")
        text = framing.deliver("[Message via Telegram]", items, "Respond in the existing session.",
                               nonce=secrets.token_hex(8))
        if text:
            self._queue_command(protocol.send_text, thread_id, text,
                                on_failure=lambda error: self._undelivered(chat, message, error))

    def compact(self, thread_id, actor, chat, message=None):
        protocol = self.protocol
        if not self._owner(actor, chat) or not protocol or thread_id not in self.attached:
            raise ProtocolError("Codex listener is unavailable")
        self._queue_command(protocol.compact, thread_id,
                            on_failure=lambda error: self._undelivered(chat, message, error))
