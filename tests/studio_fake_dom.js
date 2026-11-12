// A tiny fake DOM and panel host for running the Studio page's own script in node (no browser). The test appends a
// scenario after this prelude; `emit(obj)` prints one JSON result. Everything the page draws is walkable: byKeep(key),
// textOf(el), allEls(). The page's messages to the panel land in `out`; `send(data)` delivers one from the panel.
var vm = require("vm");
function Text(s) { this.nodeType = 3; this.data = s; this.parentNode = null; }
function El(tag) {
  this.tagName = tag; this.nodeType = 1; this.childNodes = []; this.attrs = {}; this.className = ""; this.style = {};
  this.listeners = {}; this.parentNode = null; this.hidden = false;
}
El.prototype.appendChild = function (c) { if (c.parentNode) c.parentNode.removeChild(c); c.parentNode = this; this.childNodes.push(c); return c; };
El.prototype.removeChild = function (c) { var i = this.childNodes.indexOf(c); if (i >= 0) this.childNodes.splice(i, 1); c.parentNode = null; return c; };
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); if (k === "class") this.className = String(v); };
El.prototype.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
El.prototype.addEventListener = function (t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); };
El.prototype.click = function () { (this.listeners.click || []).forEach(function (f) { f({}); }); };
El.prototype.fire = function (t, ev) { (this.listeners[t] || []).forEach(function (f) { f(ev || {}); }); };
El.prototype.focus = function () { focused = this; };
El.prototype.scrollIntoView = function () { scrolledTo = this; };
Object.defineProperty(El.prototype, "firstChild", {get: function () { return this.childNodes[0] || null; }});
Object.defineProperty(El.prototype, "lastChild", {get: function () { return this.childNodes[this.childNodes.length - 1] || null; }});
Object.defineProperty(El.prototype, "firstElementChild", {get: function () { return this.childNodes.filter(function (c) { return c.nodeType === 1; })[0] || null; }});
Object.defineProperty(El.prototype, "textContent", {
  get: function () { return this.childNodes.map(function (c) { return c.nodeType === 3 ? c.data : c.textContent; }).join(""); },
  set: function (v) { this.childNodes.forEach(function (c) { c.parentNode = null; }); this.childNodes = []; if (v !== "") this.appendChild(new Text(String(v))); }});
function walk(e, f) { f(e); e.childNodes.forEach(function (c) { if (c.nodeType === 1) walk(c, f); }); }
function allEls() { var r = []; walk(root, function (e) { r.push(e); }); return r; }
function byKeep(k) { return allEls().filter(function (e) { return e.attrs["data-keep"] === k; }); }
function byClass(c) { return allEls().filter(function (e) { return (" " + e.className + " ").indexOf(" " + c + " ") >= 0; }); }
function textOf(e) { return e.textContent; }
function ownText(e) { return e.childNodes.filter(function (c) { return c.nodeType === 3; }).map(function (c) { return c.data; }).join(""); }

var shell = new El("div"), root = new El("main");
shell.appendChild(root);
var tpl = new El("template"); tpl.content = {firstElementChild: new El("svg")};
var out = [], timers = [], handler = null, docHandlers = {}, focused = null, scrolledTo = null, audios = [];
var HOST = {postMessage: function (m) { out.push(JSON.parse(JSON.stringify(m))); }};
var docEl = new El("html"); docEl.style = {setProperty: function () {}};
var doc = {
  getElementById: function (id) {
    if (id === "app") return root;
    if (id === "art-clapper") return tpl;
    return allEls().filter(function (e) { return e.attrs.id === id; })[0] || null;
  },
  createElement: function (t) { return new El(t); }, createElementNS: function (n, t) { return new El(t); },
  createTextNode: function (s) { return new Text(s); }, importNode: function () { return new El("svg"); },
  documentElement: docEl, title: "", hidden: false,
  addEventListener: function (t, f) { docHandlers[t] = f; }};
var win = {innerWidth: 390, innerHeight: 844, scrollY: 0, scrollTo: function () {}, parent: HOST,
  addEventListener: function (t, f) { if (t === "message") handler = f; }};
function Audio(src) { audios.push(src); this.play = function () {}; }
var sandbox = {document: doc, window: win, setTimeout: function (f, ms) { timers.push({f: f, ms: ms}); return timers.length; },
  clearTimeout: function (id) { if (timers[id - 1]) timers[id - 1].cleared = true; }, setInterval: function () { return 0; },
  Intl: Intl, console: console, Audio: Audio, encodeURIComponent: encodeURIComponent};
function start(script) { vm.runInNewContext(script, sandbox); }
function send(data) { handler({source: HOST, data: data}); }
function hello(lang) {
  send({cockpit: "hello", initData: "init", lang: lang || "he", dir: (lang || "he") === "he" ? "rtl" : "ltr", theme: "", focus: false, inset: {bottom: 0}});
}
function flush() { return new Promise(function (r) { setImmediate(r); }); }
var answered = {};
async function answer(status, data) {  // answer the oldest api call that has no answer yet
  await flush();
  var call = out.filter(function (m) { return m.cockpit === "api" && !answered[m.id]; })[0];
  if (!call) throw new Error("no api call waiting");
  answered[call.id] = true;
  send({cockpit: "api", id: call.id, status: status, data: data});
  await flush();
}
async function answerWhere(pick, status, data) {  // answer the waiting api call `pick(body, index)` chooses
  await flush();
  var waiting = out.filter(function (m) { return m.cockpit === "api" && !answered[m.id]; });
  var call = waiting.filter(function (m, k) { return pick(m.body, k); })[0];
  if (!call) throw new Error("no such api call waiting");
  answered[call.id] = true;
  send({cockpit: "api", id: call.id, status: status, data: data});
  await flush();
}
function sent(kind) { return out.filter(function (m) { return m.cockpit === kind; }); }
function emit(o) { process.stdout.write(JSON.stringify(o)); }
