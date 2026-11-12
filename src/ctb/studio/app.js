// The Studio page: shell, home screen, the desk of one video (storyboard, suggestions, sentence map) and their states.
// Runs inside the Cockpit's sandboxed frame (opaque origin):
// it talks to the panel with postMessage only (hello, api, focus, back, badge, open_chat; see docs/panel.md) and
// draws everything with textContent and DOM nodes, never as markup. Words come from the strings table (strings.py).
(function () {
"use strict";
var TABLE = __STRINGS__;
var SCENES = __SCENES__;
var HELLO_RETRY = 1500, HELLO_TRIES = 3, API_TIMEOUT = 20000, POLL = 15000, LIMIT = 40;
var st = {lang: "he", dir: "rtl", ready: false, status: "loading", desk: null, view: "home", ctx: null, filter: "all",
  card: 0, sig: "", auth: "", stack: [],
  // the desk of one video: its id, the home item it was opened from, loaded videos (with the owner's draft marks) and
  // the desk's own state: load status, mode, the sentence in the player, the suggestion shown, the sentence being edited
  pid: null, item: null, proj: {}, pst: "loading", mode: "suggest", cur: 1, cue: null, edit: null, editText: "",
  playing: false, timer: null, toast: null, scrollScene: null};
var root = document.getElementById("app"), parentWin = window.parent, pending = {}, seq = 0, busy = false, helloTries = 0;
var nf = new Intl.NumberFormat("he"), pf = new Intl.NumberFormat("he", {style: "percent"}), plural = new Intl.PluralRules("he");

// --- words ------------------------------------------------------------------------------------------------------
function dict() { return TABLE[st.lang] || TABLE.en; }
function lookup(key) {
  var parts = key.split("."), v = dict()[parts[0]];
  for (var i = 1; i < parts.length && v != null; i++) v = v[parts[i]];
  return v;
}
function fill(s, vars) {
  return String(s).replace(/\{(\w+)\}/g, function (m, k) { return vars && vars[k] != null ? vars[k] : ""; });
}
function t(key, vars) { var v = lookup(key); return typeof v === "string" ? fill(v, vars) : ""; }
function tn(key, n, vars) {  // a count: the plural category of the language, "other" as the fallback
  var map = lookup(key) || {}, v = map[plural.select(n)] || map.other || "";
  var all = {n: n}; for (var k in vars) all[k] = vars[k];
  return fill(v, all);
}
function num(n, gender) {  // small counts in words where the language has them (Hebrew lines), else digits
  var w = dict().words, list = w && w[gender];
  return list && list[n] ? list[n] : nf.format(n);
}
function minutesText(sec) {
  if (!(sec > 0)) return "";
  if (sec < 60) return t("home.lessThanMinute");
  var m = Math.ceil(sec / 60), map = dict().words.minutes, v = map[plural.select(m)] || map.other;
  return fill(v, {n: num(m, "fem")});
}
function aboutLength(sec) {
  var a = dict().words.about, halves = Math.round(sec / 30);
  var key = ["half", "half", "one", "oneHalf", "two", "twoHalf"][Math.max(0, halves)];
  return key ? a[key] : fill(a.other, {n: num(Math.round(sec / 60), "fem")});
}
// A text carries its own language, and a line never mixes two (a Hebrew line holds no English words, an English line
// no Hebrew): a text is {he, en} or a plain string; the page's language wins, else the one that exists, and it is then
// shown in that language with its own direction. A short name (up to three words, no digits or links) of the other
// language may stay inside the line, bidi-isolated; anything longer goes on its own line.
var SCRIPT_HE = /[\u0590-\u05ff]/, SCRIPT_EN = /[A-Za-z]/;
function part(v) {
  if (typeof v === "string") { var s = v.trim(); return {text: s, lang: SCRIPT_HE.test(s) ? "he" : SCRIPT_EN.test(s) ? "en" : st.lang}; }
  if (v && typeof v === "object") {
    var mine = typeof v[st.lang] === "string" ? v[st.lang].trim() : "", other = st.lang === "he" ? "en" : "he";
    if (mine) return {text: mine, lang: st.lang};
    if (typeof v[other] === "string" && v[other].trim()) return {text: v[other].trim(), lang: other};
  }
  return {text: "", lang: st.lang};
}
function markLang(e, lang) { e.setAttribute("lang", lang); e.setAttribute("dir", lang === "he" ? "rtl" : "ltr"); return e; }
function shortName(p) { return p.text.split(/\s+/).filter(Boolean).length <= 3 && !/[0-9\/:@\\]/.test(p.text); }
function runs(p) {  // a text that holds both languages, cut into runs of one language each (signs stay with their run)
  var out = [], cur = null, lead = "";
  p.text.split(/(\s+)/).forEach(function (w) {
    var lang = SCRIPT_HE.test(w) ? "he" : SCRIPT_EN.test(w) ? "en" : null;
    if (!cur && !lang) { lead += w; return; }
    if (lang && (!cur || cur.lang !== lang)) { cur = {text: lead + w, lang: lang}; lead = ""; out.push(cur); return; }
    cur.text += w;
  });
  if (!out.length) return [p];
  out.forEach(function (r) { r.text = r.text.trim(); });
  return out;
}
function mixed(p) { return SCRIPT_HE.test(p.text) && SCRIPT_EN.test(p.text); }
function txt(tag, cls, parts, sep) {  // one element holding the parts of a line, never mixing the two languages
  parts = parts.filter(function (p) { return p.text; });
  var e = h(tag, cls);
  var groups = parts.map(function (p) { return mixed(p) ? runs(p) : [p]; }), flat = [].concat.apply([], groups);
  var other = flat.filter(function (p) { return p.lang !== st.lang; });
  var join = function (list) { return list.map(function (p) { return p.text; }).join(sep || ""); };
  if (!other.length) { add(e, join(parts)); return e; }
  if (other.length === flat.length && flat.length === parts.length) { markLang(e, other[0].lang); add(e, join(parts)); return e; }
  var inline = [], lines = [];
  groups.forEach(function (list) {
    var mine = [];
    list.forEach(function (p) {
      if (p.lang === st.lang) mine.push(p.text);
      else if (shortName(p)) mine.push("\u2068" + p.text + "\u2069");
      else lines.push(p);
    });
    if (mine.length) inline.push(mine.join(" "));
  });
  add(e, inline.join(sep || ""));
  lines.forEach(function (p) { e.appendChild(markLang(h("span", "ln", p.text), p.lang)); });
  return e;
}
function clock(sec) { sec = Math.max(0, Math.round(sec)); return Math.floor(sec / 60) + ":" + ("0" + (sec % 60)).slice(-2); }
function ago(sec) {
  var days = Math.max(0, Math.floor(sec / 86400));
  if (days < 1) return t("home.publishedNow");
  var weeks = days >= 7, n = weeks ? Math.floor(days / 7) : days;
  return t("home.publishedAgo", {ago: tn(weeks ? "home.agoWeek" : "home.agoDay", n, {n: num(n, "masc")})});
}

// --- DOM helpers ------------------------------------------------------------------------------------------------
var SVGNS = "http://www.w3.org/2000/svg";
function add(e, k) {
  if (k == null || k === false) return;
  if (Array.isArray(k)) { k.forEach(function (x) { add(e, x); }); return; }
  e.appendChild(typeof k === "object" && k.nodeType ? k : document.createTextNode(String(k)));
}
function h(tag, cls, kids, attrs) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (attrs) for (var k in attrs) e.setAttribute(k, attrs[k]);
  add(e, kids);
  return e;
}
function keep(e, key) { e.setAttribute("data-keep", key); return e; }
function svg(tag, attrs) {
  var e = document.createElementNS(SVGNS, tag);
  for (var k in attrs) e.setAttribute(k, attrs[k]);
  return e;
}
function ic(name, flip) {
  var s = svg("svg", {"class": "i" + (flip ? " flip" : ""), "aria-hidden": "true"});
  s.appendChild(svg("use", {href: "#i-" + name}));
  return s;
}
function button(cls, icon, label, onTap, flip) {
  var b = h("button", cls, [icon ? ic(icon, flip) : null, label], {type: "button"});
  if (onTap) b.addEventListener("click", onTap);
  return b;
}
function scene(id, kids, cls, img) {  // a placeholder picture, the real still over it when there is one, then the overlays
  var f = h("div", "frame" + (cls ? " " + cls : ""));
  var s = svg("svg", {"class": "scene", viewBox: "0 0 160 90", preserveAspectRatio: "xMidYMid slice"});
  s.appendChild(svg("use", {href: "#s-" + (SCENES.indexOf(id) >= 0 ? id : "q")}));
  f.appendChild(s);
  add(f, img);
  add(f, kids);
  return f;
}
function playBtn(small, onTap) {
  var b = h("button", "play" + (small ? " sm" : ""), null, {type: "button", "aria-label": t("common.play")});
  var s = svg("svg", {viewBox: "0 0 24 24"});
  s.appendChild(svg("use", {href: "#i-play"}));
  b.appendChild(s);
  if (onTap) b.addEventListener("click", onTap);
  return b;
}
function bot(cls) {
  var s = svg("svg", {viewBox: "0 0 44 44"});
  s.appendChild(svg("use", {href: "#bot"}));
  return h("span", "bot " + cls, s);
}
function art(id) {
  var tpl = document.getElementById(id);
  return tpl ? document.importNode(tpl.content.firstElementChild, true) : null;
}
function section(cls, title, count, extra) {
  return h("div", "sec " + cls, [h("h2", null, title), count == null ? null : h("span", "n", nf.format(count)), extra]);
}

// --- messages with the panel ------------------------------------------------------------------------------------
function post(msg) { try { parentWin.postMessage(msg, "*"); } catch (e) { /* no panel around */ } }
function call(body) {  // the answer is {status, data}; status 0 = nothing came back in time
  return new Promise(function (resolve) {
    var id = "s" + (++seq), timer = setTimeout(function () { delete pending[id]; resolve({status: 0, data: null}); }, API_TIMEOUT);
    pending[id] = function (r) { clearTimeout(timer); resolve(r); };
    post({cockpit: "api", id: id, body: body});
  });
}
function setLayout(m) {
  var b = m && m.inset && typeof m.inset.bottom === "number" ? Math.max(0, Math.min(120, m.inset.bottom)) : 0;
  document.documentElement.style.setProperty("--inset-bottom", b + "px");
}
window.addEventListener("message", function (e) {
  var m = e.data;
  if (e.source !== parentWin || !m || typeof m !== "object") return;
  if (m.cockpit === "hello" && typeof m.initData === "string") { onHello(m); return; }
  if (m.cockpit === "layout") { setLayout(m); return; }
  if (m.cockpit === "back") { onBack(); return; }
  if (m.cockpit === "api" && typeof m.id === "string" && pending[m.id]) {
    var done = pending[m.id]; delete pending[m.id];
    done({status: typeof m.status === "number" ? m.status : 0, data: m.data});
  }
});
function onHello(m) {
  if (st.ready) return;
  st.ready = true;
  st.auth = m.initData;
  st.lang = m.lang === "he" ? "he" : "en";
  st.dir = m.dir === "rtl" ? "rtl" : "ltr";
  var de = document.documentElement;
  de.lang = st.lang; de.dir = st.dir;
  if (m.theme === "dark" || m.theme === "light") de.setAttribute("data-theme", m.theme);
  nf = new Intl.NumberFormat(st.lang); pf = new Intl.NumberFormat(st.lang, {style: "percent"});
  plural = new Intl.PluralRules(st.lang);
  document.title = t("common.title");
  root.setAttribute("aria-label", t("common.frame"));
  setLayout(m);
  render();
  load();
  setInterval(function () { if (visible() && st.view === "home") load(); }, POLL);
  document.addEventListener("visibilitychange", function () { if (visible() && st.view === "home") load(); });
}
function visible() { return !document.hidden && window.innerWidth > 0 && window.innerHeight > 0; }

// --- navigation: a stack of pages over home (the desk of a video, its sentences, and one "coming next" page per entry
// point that a later step builds). Leaving home folds the panel's header and takes over its back button; coming home
// gives both back. Every back the panel forwards is confirmed (back_done), so the shell never has to force the exit.
function enter(view, ctx) {
  stopPlay();
  st.stack.push({view: st.view, ctx: st.ctx, y: window.scrollY});
  if (st.stack.length === 1) { post({cockpit: "focus", on: true}); post({cockpit: "back", on: true}); }
  st.view = view; st.ctx = ctx || null;
  window.scrollTo(0, 0); render();
}
function go(kind, item) { enter("next", {kind: kind, item: item || null}); }
function goBack() {
  stopPlay();
  var prev = st.stack.pop() || {view: "home", y: 0};
  st.edit = null;
  if (prev.view === "home") {
    st.stack = []; st.view = "home"; st.ctx = null;
    post({cockpit: "back", on: false}); post({cockpit: "focus", on: false});
  } else { st.view = prev.view; st.ctx = prev.ctx; }
  render(); window.scrollTo(0, prev.y || 0);
}
function onBack() {
  if (st.view !== "home") goBack();
  post({cockpit: "back_done"});  // always confirmed, so the shell never has to force the exit
}
function openChat() { post({cockpit: "open_chat"}); }

// --- loading the desk --------------------------------------------------------------------------------------------
function list(v, max) { return Array.isArray(v) ? v.filter(function (x) { return x && typeof x === "object"; }).slice(0, max || LIMIT) : []; }
function cleanDesk(d) {
  if (!d || typeof d !== "object") return null;
  var out = {sample: d.sample === true, series: list(d.series), waiting: list(d.waiting), working: list(d.working),
    shelf: list(d.shelf), templates: list(d.templates)};
  return out;
}
function apply(r) {
  var d = r.data, prev = st.status;
  if (r.status === 403) st.status = "expired";
  else if (r.status === 503) st.status = "off";
  else if (r.status === 0 || r.status === 504) st.status = "offline";
  else if (r.status === 200 && d && d.ok === true && cleanDesk(d.desk)) {
    var desk = cleanDesk(d.desk), sig = JSON.stringify(desk);
    st.status = "ok";
    if (sig === st.sig && prev === "ok") { busy = false; return; }  // nothing new: leave the page (and its scroll) alone
    st.sig = sig; st.desk = desk; st.card = Math.min(st.card, Math.max(0, desk.waiting.length - 1));
  } else if (r.status === 200 && d && d.ok === false && d.error === "no_permission") st.status = "noperm";
  else st.status = "error";
  busy = false;
  badge();
  render();
}
function badge() { post({cockpit: "badge", n: st.desk && st.status === "ok" ? Math.min(999, st.desk.waiting.length) : 0}); }
function load() {
  if (busy) return;
  busy = true;
  if (!st.desk) { st.status = "loading"; render(); }
  call({op: "desk.get"}).then(apply);
}
function retry() { if (st.view === "desk" || st.view === "words") loadProject(); else load(); }

// --- screens -------------------------------------------------------------------------------------------------------
function render() {
  var y = window.scrollY, sub = st.view !== "home";
  root.textContent = "";
  root.className = "appframe page" + (sub ? "" : " nodock");
  root.parentNode.className = "shell" + (sub ? " pad" : "");
  root.setAttribute("aria-busy", (sub ? st.view !== "next" && st.pst === "loading" : st.status === "loading") ? "true" : "false");
  st.scrollEl = null;
  if (st.view === "next") renderNext();
  else if (st.view === "desk" || st.view === "words") renderProject();
  else if (st.status === "loading") renderLoading();
  else if (st.status === "expired") renderExpired();
  else if (st.status === "off") renderOff();
  else if (st.status === "noperm") renderNoPerm();
  else if (st.status === "offline") renderOffline();
  else if (st.status === "error" && !st.desk) renderError();
  else if (st.status === "error") { root.appendChild(errorBanner()); renderDesk(); }
  else renderDesk();
  if (st.toast) root.appendChild(h("div", "toast", [h("span", "lead " + st.toast.cls, ic(st.toast.icon)), h("span", null, st.toast.text)],
    {role: "status"}));
  window.scrollTo(0, y);
  if (st.scrollEl && st.scrollEl.scrollIntoView) st.scrollEl.scrollIntoView({block: "start"});
}
function renderDesk() {
  var d = st.desk;
  if (d && !d.waiting.length && !d.working.length && !d.shelf.length) renderEmpty(); else renderHome();
}
function seriesPart(id) {
  var s = (st.desk ? st.desk.series : []).filter(function (x) { return x.id === id; })[0];
  return part(s ? s.name : null);
}
function sampleNotice() {
  return st.desk && st.desk.sample ? h("div", "banner blue", [ic("film"), h("span", null, t("common.sampleNotice"))]) : null;
}

// home: what waits, what works, the way to a new video, the shelf
function waitingCard(w) {
  var n = Math.max(0, Math.round(w.suggestions) || 0), len = Math.max(0, Number(w.length_s) || 0);
  var ord = dict().words.ord[w.draft];
  var tag = ord ? t("home.draftReady", {ord: ord}) : st.lang === "en" && w.draft > 0 ? t("home.draftReady", {ord: nf.format(w.draft)}) : t("home.draftReadyN");
  var detail = n ? tn("home.suggest", n, {n: num(n, "masc")}) + " " + t("home.takes", {t: minutesText(w.est_s) || t("home.lessThanMinute")})
    : t("home.suggestNone");
  var preview = keep(scene(w.scene, [h("span", "tag", tag), playBtn(false, function () { openProject(w); }),
    h("span", "dur", clock(len))]), "slate-preview");
  var fields = h("div", "fields", [
    h("div", null, [h("small", null, t("home.series")), txt("b", null, [seriesPart(w.series)])]),
    h("div", null, [h("small", null, t("home.episode")), h("b", null, nf.format(w.episode || 0))]),
    h("div", null, [h("small", null, t("home.length")), h("b", "tc", clock(len))])]);
  var actions = keep(h("div", "btns"), "slate-actions");
  actions.appendChild(button("btn yes big", "pencil", t("home.openDirect"), function () { openProject(w); }));
  actions.appendChild(button("btn big", "check", t("home.approveAsIs"), function () { go("approve", w); }));
  return h("article", "card slate", [h("div", "clapper", null, {"aria-hidden": "true"}), preview, fields,
    txt("div", "q", [part(w.title)]), h("div", "detail", detail), actions]);
}
function waitingStack(items) {
  var n = items.length, i = Math.min(st.card, n - 1), box = keep(h("div", "stack" + (n > 1 ? " multi" : "")), "waiting-stack");
  if (n > 1) { box.appendChild(h("div", "ghost g2")); box.appendChild(h("div", "ghost")); }
  box.appendChild(waitingCard(items[i]));
  var wrap = h("div");
  wrap.appendChild(box);
  if (n > 1) {
    var dots = h("div", "dots", null, {role: "group"});
    for (var k = 0; k < n; k++) (function (k) {
      var b = h("button", null, h("i", k === i ? "on" : ""), {type: "button", "aria-pressed": k === i ? "true" : "false",
        "aria-label": t("home.stackLabel", {i: nf.format(k + 1), n: nf.format(n)})});
      b.addEventListener("click", function () { st.card = k; render(); });
      dots.appendChild(b);
    })(k);
    wrap.appendChild(dots);
    var x0 = null;  // a swipe toward the start of the line shows the next card
    box.addEventListener("touchstart", function (e) { x0 = e.touches.length === 1 ? e.touches[0].clientX : null; }, {passive: true});
    box.addEventListener("touchend", function (e) {
      if (x0 == null || !e.changedTouches.length) return;
      var dx = e.changedTouches[0].clientX - x0; x0 = null;
      if (Math.abs(dx) < 48) return;
      var forward = st.dir === "rtl" ? dx > 0 : dx < 0;
      st.card = Math.max(0, Math.min(n - 1, st.card + (forward ? 1 : -1)));
      render();
    }, {passive: true});
  }
  return wrap;
}
var STAGES = ["script", "voice", "art", "captions", "checks"];
function jobCard(j) {
  var pct = Math.max(0, Math.min(1, Number(j.pct) || 0)), at = STAGES.indexOf(j.stage);
  var track = h("ol", "track");
  STAGES.forEach(function (s, i) {
    var cls = i < at ? "done" : i === at ? "cur" : "";
    track.appendChild(h("li", cls, [h("span", "sd", cls === "done" ? ic("check") : null), h("span", "sl", t("home.stage." + s))]));
  });
  var eta = minutesText(j.eta_s), line = eta ? t("home.workerLine", {t: eta}) : t("home.workerNoEta");
  var bar = h("div", "pbar", h("i")); bar.firstChild.style.inlineSize = Math.round(pct * 100) + "%";
  return keep(h("article", "card job", [
    h("div", "job-h", [scene(j.scene), h("div", "jb", [txt("div", "k", [seriesPart(j.series)]), txt("div", "t", [part(j.title)])]),
      h("span", "pct", pf.format(pct))]),
    h("div", "pg", bar), track,
    h("div", "row", [bot("sm working"), h("div", "body", h("div", "mt", line))])]), "working-job");
}
function statusPill(s) {
  if (["ready", "writing", "producing", "published"].indexOf(s) < 0) s = "producing";
  return h("span", {ready: "pill ok", writing: "pill blue", producing: "pill warm", published: "pill"}[s], t("home.status." + s));
}
function shelfMeta(it, short) {
  if (it.status === "published") return ago(Number(it.ago_s) || 0);
  return t((short ? "home.tileMeta." : "home.meta.") + (it.status === "ready" || it.status === "writing" ? it.status : "producing"));
}
function shelfName(it) { return txt("div", "nm", [seriesPart(it.series), part(it.title)], t("common.sep")); }
function shelfRows(items) {
  var box = h("div", "card group shelf phone-only");
  items.slice(0, 3).forEach(function (it) {
    var r = h("button", "row", [scene(it.scene), h("div", "body", [shelfName(it), h("div", "mt", shelfMeta(it))]),
      statusPill(it.status)], {type: "button"});
    r.addEventListener("click", function () { go("item", it); });
    box.appendChild(r);
  });
  return box;
}
function shelfTiles(items) {
  var box = h("div", "tiles desk-only");
  items.slice(0, 6).forEach(function (it) {
    var b = h("button", "tile", [scene(it.scene), shelfName(it),
      h("div", "mt", [h("span", null, shelfMeta(it, true)), statusPill(it.status)])], {type: "button"});
    b.addEventListener("click", function () { go("item", it); });
    box.appendChild(b);
  });
  return box;
}
function filters() {
  var box = keep(h("div", "filters", null, {role: "group", "aria-label": t("home.filterLabel")}), "series-filter");
  var chips = [["all", part(t("home.all"))]].concat(st.desk.series.map(function (s) { return [String(s.id), part(s.name)]; }));
  chips.forEach(function (c) {
    var on = st.filter === c[0];
    var b = txt("button", "fchip" + (on ? " on" : ""), [c[1]]);
    b.setAttribute("type", "button"); b.setAttribute("aria-pressed", on ? "true" : "false");
    b.addEventListener("click", function () { st.filter = c[0]; render(); });
    box.appendChild(b);
  });
  return box;
}
function newRow() {
  var b = h("button", "newrow", [h("span", "lead", ic("wand")), h("span", "body", [h("b", null, t("home.newTitle")),
    h("small", null, t("home.newSub"))]), ic("fwd", true)], {type: "button"});
  b.addEventListener("click", function () { go("new"); });
  return keep(b, "new-video");
}
function renderHome() {
  var d = st.desk, left = h("section"), right = h("section");
  if (d.waiting.length) {
    left.appendChild(section("hot", t("home.waiting"), d.waiting.length));
    left.appendChild(waitingStack(d.waiting));
  }
  if (d.working.length) {
    left.appendChild(section("go", t("home.working"), d.working.length));
    d.working.forEach(function (j) { left.appendChild(jobCard(j)); });
  }
  right.appendChild(newRow());
  if (st.filter !== "all" && !d.series.some(function (s) { return String(s.id) === st.filter; })) st.filter = "all";
  var shelf = d.shelf.filter(function (it) { return st.filter === "all" || String(it.series) === st.filter; });
  var more = button("link-btn end", "", t("home.shelfAll"), function () { go("more"); });
  more.appendChild(ic("fwd", true));
  right.appendChild(section("dim", t("home.shelf"), shelf.length, more));
  right.appendChild(filters());
  right.appendChild(shelfRows(shelf));
  right.appendChild(shelfTiles(shelf));
  add(root, sampleNotice());
  root.appendChild(h("div", "cols wide-side", [left, right]));
}

// states
function stateCard(key, cls, lead, title, text, buttons, note) {
  var c = keep(h("div", "card empty"), key);
  c.setAttribute("data-state", cls);
  c.appendChild(h("span", "lead big " + lead[1], ic(lead[0])));
  c.appendChild(h("h3", null, title));
  c.appendChild(h("p", null, text));
  var row = h("div", "btns"); add(row, buttons); c.appendChild(row);
  if (note) c.appendChild(h("div", "note", note));
  return h("div", "cols wide-side", [h("section", null, c), h("section")]);
}
function renderLoading() {
  var sk = function (c, s) { return h("div", "sk " + c, null, s ? {style: s} : null); };
  var left = h("section", null, [section("hot", t("home.waiting")),
    keep(h("div", "card", [sk("l s"), sk("f", "margin-block:10px"), sk("l m"), sk("l s"),
      h("div", "btns", [sk("b", "flex:1"), sk("b", "flex:1")])]), "skeleton"),
    section("go", t("home.working")),
    h("div", "card", h("div", null, [sk("", "inline-size:76px;aspect-ratio:16/9"), h("div", null, [sk("l m"), sk("l s")], {style: "flex:1"})], {style: "display:flex;gap:12px"}))]);
  var right = h("section", null, [
    keep(h("div", "loadline", [h("span", "spin", null, {"aria-hidden": "true"}), t("states.loadLine")]), "loading-line"),
    h("div", "card group", [0, 1].map(function (i) {
      return h("div", "row", [sk("", "inline-size:72px;aspect-ratio:16/9"), h("div", null, sk(i ? "l s" : "l m"), {style: "flex:1"})]);
    }))]);
  root.appendChild(h("div", "cols wide-side", [left, right]));
  left.setAttribute("aria-hidden", "true");
}
function renderEmpty() {
  var d = st.desk, c = keep(h("div", "card empty"), "empty-desk");
  c.appendChild(art("art-clapper"));
  c.appendChild(h("h3", null, t("states.emptyTitle")));
  c.appendChild(h("p", null, t("states.emptyText")));
  c.appendChild(h("div", "btns", keep(button("btn pri big", "wand", t("home.newTitle"), function () { go("new"); }), "new-video")));
  c.appendChild(button("link-btn", "chat", t("states.emptyTeam"), openChat));
  var tpls = h("div", "tpls");
  d.templates.slice(0, 4).forEach(function (tp) {
    var b = h("button", "tpl", [scene(tp.scene), txt("b", null, [part(tp.name)]),
      h("small", null, t("states.tplMeta", {len: aboutLength(Number(tp.length_s) || 0), fmt: dict().words.formats[tp.format] || ""}))], {type: "button"});
    b.addEventListener("click", function () { go("new"); });
    tpls.appendChild(b);
  });
  var right = h("section", null, d.templates.length ? [section("dim", t("states.templates"), Math.min(4, d.templates.length)), tpls] : null);
  add(root, sampleNotice());
  root.appendChild(h("div", "cols wide-side", [h("section", null, c), right]));
}
function retryBtn(cls, key) { return button("btn " + cls, "refresh", t(key), retry); }
function renderExpired() {
  root.appendChild(stateCard("expired", "expired", ["lock", "warm"], t("states.expiredTitle"), t("states.expiredText"),
    retryBtn("pri big", "states.expiredAction"), t("states.expiredNote")));
}
function renderOff() {
  root.appendChild(stateCard("bridge-off", "off", ["stop", "warm"], t("states.offTitle"), t("states.offText"),
    retryBtn("pri big", "states.offCheck")));
}
function renderNoPerm() {
  root.appendChild(stateCard("no-permission", "noperm", ["lock", "red"], t("states.noPermTitle"), t("states.noPermText"),
    retryBtn("big", "states.offCheck"), t("states.noPermNote")));
}
function offlineBanner() {
  return keep(h("div", "banner red", [ic("screen"), h("span", null, t("states.offlineBanner")),
    button("link-btn", "", t("common.retry"), retry)], {role: "status"}), "offline-banner");
}
function renderOffline() {
  if (st.desk) { root.appendChild(offlineBanner()); renderDesk(); return; }
  root.appendChild(offlineBanner());
  root.appendChild(stateCard("offline-card", "offline", ["screen", "red"], t("states.offlineTitle"), t("states.offlineText"),
    retryBtn("pri big", "common.retry")));
}
function errorBanner() { return h("div", "banner red", [ic("alert"), h("span", null, t("states.errorWhy"))], {role: "status"}); }
function renderError() {
  var card = keep(h("article", "card err"), "error-card");
  card.setAttribute("data-state", "error");
  card.appendChild(h("div", "err-h", [h("span", "lead red", ic("alert")), h("div", null, [h("b", null, t("states.errorTitle")),
    h("small", null, t("states.errorWhen"))])]));
  card.appendChild(keep(h("div", "why-box", [h("b", null, t("states.whatHappened") + " "), t("states.errorWhy")]), "plain-reason"));
  card.appendChild(keep(h("div", "btns", [retryBtn("pri", "common.retry"), button("btn", "chat", t("common.askTeam"), openChat)]), "error-actions"));
  root.appendChild(h("div", "cols wide-side", [h("section", null, card), h("section")]));
}

// the page every not-yet-built entry point leads to: honest about what is next, nothing sent, nothing changed
function renderNext() {
  var c = st.ctx || {}, kind = c.kind || "new", it = c.item, fromDesk = kind === "render" || kind === "review";
  var bar = {new: t("next.barNew"), approve: t("next.barApprove"), render: t("next.barRender"), review: t("next.barReview")}[kind] || t("next.barOpen");
  var sub = kind === "new" ? part(t("next.barNewSub")) : it ? part(it.title) : null;
  var text = {new: "next.textNew", open: "next.textOpen", approve: "next.textApprove", item: "next.textItem", more: "next.textMore",
    render: "next.textRender", review: "next.textReview"}[kind] || "next.textNew";
  root.appendChild(backBar([h("b", null, bar), sub && sub.text ? txt("small", null, [sub]) : null]));
  var card = keep(h("div", "card empty"), "coming-next");
  card.appendChild(art("art-clapper"));
  card.appendChild(h("h3", null, t("next.title")));
  card.appendChild(h("p", null, t(text)));
  card.appendChild(h("div", "note", t(fromDesk ? "next.noteMarks" : "next.note")));
  var rows = [["shield", "next.safe1"], ["lock", "next.safe2"]];
  if (fromDesk) rows.unshift(["check", "next.safe3"]);
  var safe = h("div", "card group", rows.map(function (r) {
    return h("div", "row", [h("span", "lead ok", ic(r[0])), h("div", "body", h("div", "nm", t(r[1])))]);
  }));
  var dock = keep(h("div", "dock", h("div", "panel", [button("btn pri wide big", "back", t(fromDesk ? "next.backVideo" : "next.home"), onBack, true),
    button("btn ghost wide", "chat", t("next.chat"), openChat)])), "primary-dock");
  dock.firstChild.lastChild.style.marginBlockStart = "8px";
  root.appendChild(h("div", "cols", [h("section", null, card), h("section", null, [h("div", "sec", h("h2", null, t("next.safe"))), safe, dock])]));
}
function backBar(title, end) {
  var b = keep(h("button", "icon-btn", ic("back", true), {type: "button", "aria-label": t("common.back")}), "back");
  b.addEventListener("click", function () { onBack(); });
  return h("div", "bar", [b, h("div", "t", title), end || null]);
}

// --- the desk of one video ---------------------------------------------------------------------------------------------
// Opened from a home card: the video as a storyboard (its scenes in order, a still each), the team's suggestions one at a
// time, and every sentence on the sentence map. The owner's keep / tighten / remove marks are a local draft (`marks.set`
// stores them on the Studio computer); nothing renders, is sent or is published from here.
var SUGGESTS = ["tighten", "remove"], FLAGS = ["visual", "take", "fix"], MARKS = ["keep", "tighten", "remove"];
var MEDIA = /^[A-Za-z0-9][A-Za-z0-9._\/-]{0,199}$/;
function toInt(v, lo, hi) { var n = Math.round(Number(v)); return isFinite(n) ? Math.max(lo, Math.min(hi, n)) : lo; }
function secs(v) { var n = Number(v); return isFinite(n) && n > 0 ? Math.min(n, 36000) : 0; }
function mediaPath(v) { return typeof v === "string" && MEDIA.test(v) && v.indexOf("..") < 0 ? v : null; }
function mediaUrl(path) {
  return "media/" + path.split("/").map(encodeURIComponent).join("/") + "?auth=" + encodeURIComponent(st.auth);
}
function cleanProject(p, pid) {
  if (!p || typeof p !== "object" || String(p.id) !== pid) return null;
  var scenes = list(p.scenes, 60).map(function (sc, k) {
    var a = secs(sc.start), b = Math.max(a, secs(sc.end));
    return {k: k, title: sc.title, start: a, end: b, scene: sc.scene, frame: mediaPath(sc.frame)};
  });
  var lines = list(p.sentences, 200).map(function (s, k) {
    var a = secs(s.start), b = Math.max(a, secs(s.end));
    return {i: k + 1, scene_i: toInt(s.scene_i, 0, Math.max(0, scenes.length - 1)), start: a, end: b, said: s.said,
      scene: s.scene, frame: mediaPath(s.frame), audio: mediaPath(s.audio),
      suggest: SUGGESTS.indexOf(s.suggest) >= 0 ? s.suggest : null, short: s.short, short_s: secs(s.short_s),
      why: s.why, flag: FLAGS.indexOf(s.flag) >= 0 ? s.flag : null, note: s.note};
  });
  if (!lines.length) return null;
  if (!scenes.length) scenes = [{k: 0, title: null, start: 0, end: lines[lines.length - 1].end, scene: lines[0].scene, frame: null}];
  var total = lines[lines.length - 1].end;
  return {id: pid, sample: p.sample === true, series: p.series, title: p.title, draft: toInt(p.draft, 0, 99),
    length_s: secs(p.length_s) || total, scenes: scenes, sentences: lines};
}
function cleanMarks(m, count) {
  var out = {};
  if (!m || typeof m !== "object") return out;
  for (var k in m) {
    var v = m[k], i = Number(k);
    if (!(i >= 1 && i <= count && Math.round(i) === i) || !v || MARKS.indexOf(v.mark) < 0) continue;
    out[i] = {mark: v.mark};
    if (v.mark === "tighten" && typeof v.text === "string" && v.text.trim()) out[i].text = v.text.trim().slice(0, 240);
  }
  return out;
}
function cur() { return st.proj[st.pid] || null; }
function sentence(i) { var c = cur(); return c ? c.project.sentences[i - 1] || null : null; }
function markOf(i) { var c = cur(), m = c && c.marks[i]; return m ? m.mark : null; }
function stateOf(s) {  // one status vocabulary for the map, the legend and the rows
  var m = markOf(s.i);
  if (m === "remove") return "x";
  if (m === "tighten") return "t";
  if (s.suggest && !m) return "s";
  if (s.flag === "fix") return "f";
  return s.flag ? "n" : "";
}
function shortPart(s) {  // the shorter wording: the owner's own, else the team's
  var c = cur(), m = c && c.marks[s.i];
  return m && m.text ? part(m.text) : part(s.short);
}
function newLength(s) {
  var len = s.end - s.start, m = markOf(s.i);
  if (m === "remove") return 0;
  if (m !== "tighten") return len;
  var said = part(s.said).text, mine = cur().marks[s.i].text;
  if (mine && said) return Math.max(1, Math.min(len, len * mine.length / said.length));
  return s.short_s > 0 ? Math.min(len, s.short_s) : len * 0.7;
}
function runtime(p) {
  var cut = 0;
  p.sentences.forEach(function (s) { cut += (s.end - s.start) - newLength(s); });
  return {before: p.length_s, after: Math.max(0, Math.round(p.length_s - cut))};
}
function changesOf(p, lines) {
  return (lines || p.sentences).filter(function (s) { var m = markOf(s.i); return m === "tighten" || m === "remove"; }).length;
}
function secondsText(sec) {
  sec = Math.round(sec);
  if (sec > 20) return aboutLength(sec);
  var map = dict().words.seconds;
  return fill(map[plural.select(sec)] || map.other, {n: num(sec, "fem")});
}
function count(key, n, gender) { return tn(key, n, {n: num(n, gender)}); }

// loading one video: what was loaded shows at once and is refreshed underneath; an answer for another video is dropped
// Each loaded video keeps its own state: `saving` (the sentences whose mark is on its way, with the request's number)
// and `offline` (a request for it found the Studio unreachable: it stays read-only until a request succeeds again).
function openProject(item) {
  st.pid = String(item.id); st.item = item; st.mode = "suggest"; st.cue = null; st.edit = null; st.cur = 1;
  var known = st.proj[st.pid];
  st.pst = known ? known.offline ? "offline" : "ok" : "loading";
  enter("desk");
  loadProject();
}
function loadProject() {
  var pid = st.pid;
  if (!st.proj[pid]) { st.pst = "loading"; render(); }
  call({op: "project.get", project: pid}).then(function (r) { if (st.pid === pid) applyProject(r, pid); });
}
function applyProject(r, pid) {
  var d = r.data, p = d && d.ok === true ? cleanProject(d.project, pid) : null;
  if (r.status === 403) st.pst = "expired";
  else if (r.status === 503) st.pst = "off";
  else if (r.status === 0 || r.status === 504) { st.pst = "offline"; if (st.proj[pid]) st.proj[pid].offline = true; }
  else if (r.status === 200 && p) {
    var old = st.proj[pid], marks = cleanMarks(d.marks, p.sentences.length), onWay = old ? old.saving : {};
    for (var k in onWay) { if (old.marks[k]) marks[k] = old.marks[k]; else delete marks[k]; }  // a tap on its way wins
    st.proj[pid] = {project: p, marks: marks, saving: onWay, seq: old ? old.seq : 0, offline: false};
    st.pst = "ok";
    if (!old) {  // first look: the player starts at the first open suggestion
      var first = openSuggestions(p)[0];
      st.cur = first ? first.i : 1;
    }
    st.cur = Math.min(st.cur, p.sentences.length);
  } else if (r.status === 200 && d && d.ok === false && d.error === "not_found") st.pst = "notfound";
  else if (r.status === 200 && d && d.ok === false && d.error === "no_permission") st.pst = "noperm";
  else st.pst = "error";
  if (st.view === "desk" || st.view === "words") render();
}
function readOnly() { return st.pst !== "ok"; }
function saving(i) { var c = cur(); return !!(c && c.saving[i]); }
function toast(text, cls, icon) {
  st.toast = {text: text, cls: cls || "red", icon: icon || "alert"};
  setTimeout(function () { st.toast = null; render(); }, 3600);
}

// a mark saves at once: shown first, stored, and rolled back if the Studio did not keep it. An answer changes only its
// own sentence (an older answer can never undo a newer mark), and only its own video's state.
function setMark(s, mark, text) {
  var c = cur(), pid = st.pid;
  if (!c || readOnly() || c.saving[s.i]) return;
  var before = c.marks[s.i], next = mark === "clear" ? null : {mark: mark}, n = ++c.seq;
  if (next && text) next.text = text;
  if (next) c.marks[s.i] = next; else delete c.marks[s.i];
  c.saving[s.i] = n; st.edit = null; st.cue = null; st.cur = s.i;
  if (st.view === "desk" && st.mode === "suggest") {  // the next open suggestion takes the player
    var open = openSuggestions(c.project);
    if (open.length) st.cur = open[0].i;
  }
  render();
  var body = {op: "marks.set", project: pid, i: s.i, mark: mark};
  if (text) body.text = text;
  call(body).then(function (r) {
    var d = r.data, c2 = st.proj[pid], here = st.pid === pid && (st.view === "desk" || st.view === "words");
    if (!c2 || c2.saving[s.i] !== n) return;  // the video was reloaded meanwhile: its own answer decides
    delete c2.saving[s.i];
    if (r.status === 200 && d && d.ok === true && d.marks && typeof d.marks === "object") {
      var kept = cleanMarks(d.marks, c2.project.sentences.length)[s.i];
      if (kept) c2.marks[s.i] = kept; else delete c2.marks[s.i];
      c2.offline = false;
    } else {
      if (before) c2.marks[s.i] = before; else delete c2.marks[s.i];
      var lost = r.status === 0 || r.status === 504;
      if (lost) c2.offline = true;
      if (st.pid === pid) {
        st.cur = s.i;  // back on the sentence whose mark did not keep
        if (r.status === 403) st.pst = "expired";
        else if (r.status === 503) st.pst = "off";
        else if (lost) st.pst = "offline";
      }
      if (here) toast(t(lost ? "desk.savedFail" : "desk.savedFailGeneric"));
    }
    if (here) render();
  });
}
function tightenWith(s) {  // "shorten" from the editor saves the owner's wording; the team's own wording needs no text
  var text = "", m = cur() && cur().marks[s.i];
  if (st.edit === s.i) {
    text = st.editText.replace(/\s+/g, " ").trim();
    if (text === part(s.short).text) text = "";
  } else if (m && m.text) text = m.text;  // tapped again: the owner's earlier wording stays
  setMark(s, "tighten", text || null);
}
function setCur(i) { st.cur = i; render(); }

// the player: the still and caption of the sentence in it; play steps through the stills in time (no sound until a
// real draft brings its proxy video), skipping removed sentences
function stopPlay() { st.playing = false; if (st.timer) clearTimeout(st.timer); st.timer = null; }
function nextLive(p, from) {
  for (var i = from + 1; i <= p.sentences.length; i++) if (markOf(i) !== "remove") return i;
  return 0;
}
function tick() {
  var s = sentence(st.cur);
  st.timer = setTimeout(function () {
    var c = cur();
    if (!st.playing || !c) return;
    var nx = nextLive(c.project, st.cur);
    if (!nx) { stopPlay(); render(); return; }
    st.cur = nx; render(); tick();
  }, Math.max(1200, (s ? newLength(s) : 2) * 1000));
}
function togglePlay() {
  var c = cur();
  if (!c) return;
  if (st.playing) { stopPlay(); render(); return; }
  if (markOf(st.cur) === "remove" || st.cur >= c.project.sentences.length) st.cur = nextLive(c.project, 0) || 1;
  st.playing = true; render(); tick();
}
function still(sceneId, frame, kids, cls) {
  var img = null;
  if (frame && st.auth) {  // the engine's real still over the placeholder; the placeholder stays if it does not load
    img = h("img", "still", null, {src: mediaUrl(frame), alt: "", loading: "lazy", decoding: "async"});
    img.addEventListener("error", function () { if (img.parentNode) img.parentNode.removeChild(img); });
  }
  return scene(sceneId, kids, cls, img);
}
function capNode(s) {
  if (markOf(s.i) === "remove") return h("div", "veil", h("span", "veil-t", t("desk.removedVeil")));
  var p = markOf(s.i) === "tighten" && shortPart(s).text ? shortPart(s) : part(s.said);
  if (!p.text) return null;
  if (mixed(p)) return txt("div", "cap hl", [p]);
  var c = h("div", "cap hl", h("span", null, p.text));
  if (p.lang !== st.lang) markLang(c, p.lang);
  return c;
}
function player(p) {
  var s = sentence(st.cur) || p.sentences[0];
  var play = h("button", "play", null, {type: "button", "aria-label": st.playing ? t("desk.pause") : t("common.play")});
  var g = svg("svg", {viewBox: "0 0 24 24"});
  g.appendChild(svg("use", {href: st.playing ? "#i-pause" : "#i-play"}));
  play.appendChild(g);
  play.addEventListener("click", togglePlay);
  var f = still(s.scene, s.frame, [h("span", "tag", t("desk.playerTag")), capNode(s), play, h("span", "dur", clock(s.start))], "player");
  f.setAttribute("role", "group");
  f.setAttribute("aria-label", t("desk.playerLabel"));
  return keep(f, "player");
}
function mapBar(p) {
  var bar = keep(h("div", "map", null, {role: "img", "aria-label": t("desk.mapLabel")}), "map");
  p.sentences.forEach(function (s) {
    var c = h("i", (stateOf(s) + (s.i === st.cur ? " cur" : "")).trim(), null, {"data-i": String(s.i)});
    c.style.flex = String(Math.max(1, s.end - s.start));
    bar.appendChild(c);
  });
  bar.addEventListener("click", function (e) {
    var i = e && e.target && e.target.getAttribute ? Number(e.target.getAttribute("data-i")) : 0;
    if (i) setCur(i);
  });
  var total = p.sentences[p.sentences.length - 1].end;
  return [bar, h("div", "map-ends", [h("span", "tc", clock(0)), h("span", "tc", clock(total))])];
}
function runtimeRow(p, cls) {
  var rt = runtime(p), changed = rt.after !== rt.before;
  return keep(h("div", "runtime" + (cls ? " " + cls : ""), [h("span", null, t("desk.length")),
    changed ? h("span", "chip old", clock(rt.before)) : null, changed ? ic("arrow", st.dir === "rtl") : null,
    h("span", "chip" + (changed ? " ok" : ""), clock(rt.after)),
    changed ? h("span", "pill ok end", t("desk.saves", {t: secondsText(rt.before - rt.after)})) : null]), "runtime");
}
function legend() {
  return keep(h("div", "legend", ["kept", "t", "s", "n", "f", "x"].map(function (k) {
    return h("span", null, [h("i", k === "kept" ? "" : k), t("sents.legend." + k)]);
  })), "legend");
}
function openSuggestions(p) { return p.sentences.filter(function (s) { return s.suggest && !markOf(s.i); }); }
function modes(p) {
  var n = openSuggestions(p).length;
  var box = keep(h("div", "seg modes", null, {role: "group", "aria-label": t("desk.modesLabel")}), "desk-modes");
  [["suggest", "cc", "desk.modeSuggest"], ["scenes", "board", "desk.modeScenes"]].forEach(function (m) {
    var on = st.mode === m[0];
    var b = button("opt" + (on ? " on" : ""), m[1], t(m[2]), function () { st.mode = m[0]; st.cue = null; render(); });
    b.setAttribute("aria-pressed", on ? "true" : "false");
    if (m[0] === "suggest" && n) b.appendChild(h("span", "k", nf.format(n)));
    box.appendChild(b);
  });
  box.appendChild(keep(button("opt", "list", t("desk.modeAll"), function () { openWords(null); }), "to-map"));
  return box;
}
function hearBtn(s) {
  var b = keep(h("button", "hear", ic("speaker"), {type: "button", "aria-label": t("desk.hear")}), "hear");
  b.addEventListener("click", function () {
    st.cur = s.i;
    if (s.audio && st.auth && typeof Audio === "function") {  // the sentence's own clip, only its range
      try { var a = new Audio(mediaUrl(s.audio)); a.play(); } catch (e) { /* the browser refused: nothing to do */ }
      render();
    } else { toast(t("desk.hearNone"), "blue", "speaker"); render(); }
  });
  return b;
}
function saidNode(s, cls) {  // the sentence as said; with a shorter wording that ends it, the part that goes is struck
  var p = part(s.said), sp = markOf(s.i) === "tighten" || s.suggest === "tighten" && !markOf(s.i) ? shortPart(s) : {text: ""};
  if (mixed(p)) return txt("div", cls, [p]);  // both languages in one sentence: one line each, no strike
  var e = h("div", cls);
  if (p.lang !== st.lang) markLang(e, p.lang);
  var keepEnd = sp.text && sp.lang === p.lang && p.text.length > sp.text.length ? p.text.lastIndexOf(sp.text) : -1;
  var head = sp.text ? sp.text.replace(/[.!?\u05c3]+$/, "") : "";
  if (keepEnd > 0 && keepEnd + sp.text.length === p.text.length) add(e, [h("s", null, p.text.slice(0, keepEnd)), p.text.slice(keepEnd)]);
  else if (head && sp.lang === p.lang && p.text.length > head.length && p.text.indexOf(head) === 0) add(e, [head, h("s", null, p.text.slice(head.length))]);
  else add(e, p.text);
  return e;
}
function textField(id, value, placeholder) {  // an empty field takes the page's direction; it turns auto only with text
  var f = h("textarea", "field", null, {id: id, rows: "2", maxlength: "240", placeholder: placeholder});
  f.value = value || "";
  var direction = function () { f.setAttribute("dir", f.value.trim() ? "auto" : st.dir); };
  direction();
  f.addEventListener("input", function () { st.editText = f.value; direction(); });
  return f;
}
function fixBox(s, edit) {
  var mine = cur().marks[s.i] && cur().marks[s.i].text, sp = shortPart(s);
  if (edit) {
    return keep(h("div", "fixedit", [h("label", "lbl", t("desk.editLabel"), {"for": "ed-" + s.i}),
      textField("ed-" + s.i, st.editText, t("desk.editPlaceholder")), h("div", "fieldhint", h("span", null, t("desk.editHint")))]), "short-edit");
  }
  var b = h("button", "fix", [h("div", "body", [h("small", null, t(mine ? "desk.yourShort" : "desk.afterShort")), txt("b", null, [sp])]),
    ic("pencil")], {type: "button"});
  b.addEventListener("click", function () {
    if (readOnly()) return;
    st.edit = s.i; st.editText = sp.text; stopPlay(); render();
    var f = document.getElementById ? document.getElementById("ed-" + s.i) : null;
    if (f && f.focus) f.focus();
  });
  return b;
}
function answers(s, three) {  // keep / tighten / remove: the team's suggestion is the primary button
  var m = markOf(s.i), box = h("div", three ? "btns three" : "seg", null, {role: "group", "aria-label": t("desk.choiceLabel")});
  [["keep", "check"], ["tighten", "tight"], ["remove", "trash"]].forEach(function (a) {
    var on = m === a[0] || !three && !m && !s.suggest && a[0] === "keep";
    var cls = three ? "btn" + (s.suggest === a[0] ? " yes" : a[0] === "remove" ? " rm" : "")
      : "opt" + (on ? " on" : "") + (on && a[0] === "tighten" ? " t" : on && a[0] === "remove" ? " x" : "");
    var b = button(cls, three ? a[1] : null, t("desk." + a[0]), function () {
      if (a[0] === "tighten") tightenWith(s); else setMark(s, a[0]);
    });
    b.setAttribute("aria-pressed", on ? "true" : "false");
    if (readOnly() || saving(s.i)) { b.setAttribute("disabled", ""); b.className += " off"; }
    box.appendChild(b);
  });
  return box;
}
function cueStack(p) {
  var sugg = p.sentences.filter(function (s) { return s.suggest; }), open = openSuggestions(p);
  if (!sugg.length) return noSuggestions(false);
  var shown = st.cue && sugg.some(function (s) { return s.i === st.cue; }) ? st.cue : open.length ? open[0].i : 0;
  if (!shown) return noSuggestions(true);
  var s = sentence(shown);
  var head = h("div", "cue-h", [ic(s.suggest === "remove" ? "trash" : "tight"), h("span", null, t("desk.suggest." + s.suggest)),
    h("span", "chip", clock(s.start) + "–" + clock(s.end))]);
  var body = h("div", "cue-b", [h("div", "vtop", [saidNode(s, "said"), hearBtn(s)]),
    s.suggest === "tighten" ? fixBox(s, st.edit === s.i) : null,
    part(s.why).text ? h("div", "why", [ic("bolt"), txt("span", null, [part(s.why)])]) : null,
    keep(answers(s, true), "cue-answers")]);
  var box = keep(h("div", "stack cues" + (open.length > 1 ? " multi" : "")), "cue-stack");
  if (open.length > 1) box.appendChild(h("div", "ghost"));
  box.appendChild(h("article", "card cue", [head, body]));
  var wrap = h("div", null, box);
  if (sugg.length > 1) {
    var dots = h("div", "dots", null, {role: "group"});
    sugg.forEach(function (x, k) {
      var on = x.i === shown, b = h("button", null, h("i", on ? "on" : markOf(x.i) ? "done" : ""), {type: "button",
        "aria-pressed": on ? "true" : "false", "aria-label": t("desk.cueLabel", {i: num(k + 1, "fem"), n: num(sugg.length, "fem")})});
      b.addEventListener("click", function () { st.cue = x.i; st.edit = null; st.cur = x.i; stopPlay(); render(); });
      dots.appendChild(b);
    });
    wrap.appendChild(dots);
  }
  return wrap;
}
function noSuggestions(answered) {
  var c = keep(h("div", "card empty"), "no-suggestions");
  c.appendChild(h("span", "okmark big", ic("check")));
  c.appendChild(h("h3", null, t(answered ? "desk.allAnsweredTitle" : "desk.noSuggestTitle")));
  c.appendChild(h("p", null, t(answered ? "desk.allAnsweredText" : "desk.noSuggestText")));
  c.appendChild(button("link-btn", "list", t("desk.toAll"), function () { openWords(null); }));
  return c;
}
function storyboard(p) {
  var box = keep(h("div", "board"), "storyboard");
  p.scenes.forEach(function (sc, k) {
    var lines = p.sentences.filter(function (s) { return s.scene_i === k; });
    var open = lines.filter(function (s) { return s.suggest && !markOf(s.i); }).length, ch = changesOf(p, lines);
    var pill = open ? h("span", "pill warm", count("desk.sceneSuggest", open, "fem"))
      : ch ? h("span", "pill blue", count("desk.sceneChanges", ch, "masc")) : h("span", "pill", t("desk.sceneClean"));
    var title = part(sc.title).text ? txt("b", null, [part(sc.title)]) : h("b", null, sceneName(k));
    var b = keep(h("button", "scard", [still(sc.scene, sc.frame, [h("span", "no", nf.format(k + 1)), h("span", "dur", clock(sc.end - sc.start))]),
      title, h("small", null, [count("desk.sentencesCount", lines.length, "masc") + t("common.sep"),
        h("span", "tc", clock(sc.start) + "–" + clock(sc.end))]), pill], {type: "button"}), "scene-card");
    b.addEventListener("click", function () { openWords(k); });
    box.appendChild(b);
  });
  return box;
}
function sceneName(k) {
  var ord = dict().words.ord[k + 1];
  return ord ? t("desk.sceneOrd", {ord: ord}) : st.lang === "en" ? t("desk.sceneOrd", {ord: nf.format(k + 1)}) : t("desk.sceneN");
}
function deskDock(p) {
  var n = changesOf(p), rt = runtime(p);
  var sum = keep(h("div", "sumrow", [h("span", "lead" + (n ? " ok" : ""), ic("check")),
    h("div", "body", h("b", null, n ? count("desk.changes", n, "masc") : t("desk.noChanges"))),
    h("span", "chip" + (n ? " ok" : ""), clock(rt.after))]), "new-length");
  return keep(h("div", "dock", h("div", "panel", [sum, h("div", "two", [
    button("btn sm", "shield", t("desk.review"), function () { go("review", st.item); }),
    button("btn pri", "bolt", t("desk.render"), function () { go("render", st.item); })])])), "primary-dock");
}
function projectBar(p, end) {
  var it = p || st.item || {}, ord = dict().words.ord[it.draft];
  var draft = ord ? t("desk.draft", {ord: ord}) : st.lang === "en" && it.draft > 0 ? t("desk.draft", {ord: nf.format(it.draft)}) : t("desk.draftN");
  return backBar([txt("b", null, [part(it.title)]), txt("small", null, [seriesPart(it.series), part(draft)], t("common.sep"))], end);
}
function projectNotice(p) {
  return p.sample ? h("div", "banner blue", [ic("film"), h("span", null, t("common.sampleNotice"))]) : null;
}
function deskOfflineBanner() {
  return keep(h("div", "banner red", [ic("screen"), h("span", null, t("desk.offlineBanner")),
    button("link-btn", "", t("common.retry"), retry)], {role: "status"}), "offline-banner");
}
function renderDeskView(p) {
  var open = openSuggestions(p).length, had = p.sentences.some(function (s) { return s.suggest; });
  var pill = open ? h("span", "pill warm", t("desk.pillWaiting")) : had ? h("span", "pill ok", t("desk.pillDone")) : null;
  root.appendChild(projectBar(p, pill));
  if (st.pst === "offline") root.appendChild(deskOfflineBanner());
  var left = h("section", "col-sticky", [player(p), mapBar(p), runtimeRow(p, "desk-only"), modes(p), deskDock(p)]);
  var right = h("section");
  if (st.mode === "scenes") {
    right.appendChild(section("dim", t("desk.storyboard"), p.scenes.length));
    right.appendChild(storyboard(p));
    right.appendChild(h("div", "note", t("desk.storyboardHint")));
  } else {
    right.appendChild(cueStack(p));
    right.appendChild(h("div", "desk-only", [section("dim", t("desk.storyboard"), p.scenes.length), storyboard(p)]));
  }
  add(right, projectNotice(p));
  root.appendChild(h("div", "cols", [left, right]));
}
function openWords(sceneK) { stopPlay(); st.scrollScene = sceneK; st.edit = null; enter("words"); }
function vrow(s) {
  var m = markOf(s.i), cell = h("div", "vcell", [h("span", "no", nf.format(s.i)), still(s.scene, s.frame), h("span", "tc", clock(s.start))]);
  if (m === "remove") {
    var back = button("btn sm", "undo", t("desk.restore"), function () { setMark(s, "keep"); });
    if (readOnly() || saving(s.i)) { back.setAttribute("disabled", ""); back.className += " off"; }
    return keep(h("div", "vrow cut", [cell, h("div", "card vbody", [saidNode(s, "tx"), back])]), "sentence");
  }
  var meta = h("div", "vmeta");
  if (s.suggest && !m) meta.appendChild(h("span", "pill warm", [ic(s.suggest === "remove" ? "trash" : "tight"), t("desk.suggestPill." + s.suggest)]));
  if (s.flag) {
    var note = s.flag === "fix" && part(s.note).text ? part(s.note) : part(t("desk.flag." + s.flag));
    meta.appendChild(h("span", "pill " + (s.flag === "fix" ? "red" : "blue"), [ic({visual: "image", take: "mic", fix: "alert"}[s.flag]),
      txt("span", null, [note])]));
  }
  if (m === "tighten" && !shortPart(s).text && st.edit !== s.i) meta.appendChild(h("span", "pill warm", [ic("tight"), t("desk.teamShortens")]));
  var fix = m === "tighten" && (shortPart(s).text || st.edit === s.i) ? fixBox(s, st.edit === s.i) : null;
  var cls = "vrow" + (s.i === st.cur ? " cur" : "") + (s.flag === "fix" && !m ? " flag" : "");
  return keep(h("div", cls, [cell, h("div", "card vbody", [h("div", "vtop", [saidNode(s, "tx"), hearBtn(s)]),
    meta.childNodes.length ? meta : null, fix, answers(s, false)])]), "sentence");
}
function renderWordsView(p) {
  root.appendChild(backBar([h("b", null, t("sents.title")), h("small", null,
    count("desk.sentencesCount", p.sentences.length, "masc") + t("common.sep") + count("desk.scenesCount", p.scenes.length, "fem"))]));
  if (st.pst === "offline") root.appendChild(deskOfflineBanner());
  var left = h("section", "col-sticky", [h("div", "desk-only", player(p)), mapBar(p), legend(), runtimeRow(p), deskDock(p)]);
  var listBox = keep(h("div", "sgroups"), "sentence-list");
  p.scenes.forEach(function (sc, k) {
    var lines = p.sentences.filter(function (s) { return s.scene_i === k; });
    if (!lines.length) return;
    var name = part(sc.title).text ? [h("small", null, sceneName(k)), txt("b", null, [part(sc.title)])] : [h("b", null, sceneName(k))];
    var head = h("div", "shead", [still(sc.scene, sc.frame), h("div", "body", name), h("span", "chip", clock(sc.start) + "–" + clock(sc.end))]);
    var group = keep(h("section", "sgroup", [head, h("div", "vfilm", lines.map(vrow))], {"aria-label": sceneName(k)}), "scene-group");
    if (st.scrollScene === k) st.scrollEl = group;
    listBox.appendChild(group);
  });
  st.scrollScene = null;
  var right = h("section", null, [listBox]);
  add(right, projectNotice(p));
  root.appendChild(h("div", "cols", [left, right]));
}
function renderDeskLoading() {
  var it = st.item || {};
  root.appendChild(projectBar(null));
  var poster = keep(scene(it.scene, [h("div", "veil", h("span", "spin big", null, {"aria-hidden": "true"})), h("span", "tag", t("desk.loadingTag"))]),
    "poster-first");
  var sk = function (c, s) { return h("div", "sk " + c, null, s ? {style: s} : null); };
  var right = h("section", null, [h("div", "card", [sk("l s"), sk("l"), sk("l m"),
    h("div", "btns", [sk("b", "flex:1"), sk("b", "flex:1.3"), sk("b", "flex:1")])]), h("div", "note", t("desk.loadingNote"))]);
  right.setAttribute("aria-hidden", "true");
  root.appendChild(h("div", "cols", [h("section", "col-sticky", [poster, sk("", "block-size:12px;margin-block:16px 6px;border-radius:99px")]), right]));
}
function renderProjectError(notFound) {
  var card = keep(h("article", "card err"), "error-card");
  card.setAttribute("data-state", notFound ? "notfound" : "error");
  card.appendChild(h("div", "err-h", [h("span", "lead red", ic("alert")), h("div", null, [
    h("b", null, t(notFound ? "desk.notFoundTitle" : "desk.errorTitle")), h("small", null, t("desk.errorWhen"))])]));
  card.appendChild(keep(h("div", "why-box", [h("b", null, t("states.whatHappened") + " "), t(notFound ? "desk.notFoundWhy" : "desk.errorWhy")]),
    "plain-reason"));
  card.appendChild(keep(h("div", "btns", [notFound ? button("btn pri", "back", t("next.home"), onBack, true) : retryBtn("pri", "common.retry"),
    button("btn", "chat", t("common.askTeam"), openChat)]), "error-actions"));
  root.appendChild(h("div", "cols", [h("section", null, card), h("section")]));
}
function renderProject() {
  var c = cur(), p = c && c.project, s = st.pst;
  var nodock = !p || s === "expired" || s === "off" || s === "noperm" || s === "notfound" || s === "error" && !p;
  if (nodock) root.className += " nodock";
  if (s === "loading" && !p) { renderDeskLoading(); return; }
  if (s === "expired" || s === "off" || s === "noperm") {
    root.appendChild(projectBar(p));
    if (s === "expired") renderExpired(); else if (s === "off") renderOff(); else renderNoPerm();
    return;
  }
  if (s === "notfound") { root.appendChild(projectBar(p)); renderProjectError(true); return; }
  if (!p) {
    root.appendChild(projectBar(null));
    if (s === "offline") {
      root.appendChild(deskOfflineBanner());
      root.appendChild(stateCard("offline-card", "offline", ["screen", "red"], t("states.offlineTitle"), t("states.offlineText"),
        retryBtn("pri big", "common.retry")));
    } else renderProjectError(false);
    return;
  }
  if (s === "error") root.appendChild(errorBanner());
  if (st.view === "words") renderWordsView(p); else renderDeskView(p);
}
// --- start --------------------------------------------------------------------------------------------------------
function hello() {
  if (st.ready) return;
  if (++helloTries > HELLO_TRIES) { st.ready = true; st.status = "error"; render(); return; }
  post({cockpit: "hello"});
  setTimeout(hello, HELLO_RETRY);
}
hello();
})();
