"""The flight board's motion units: a station ident and a "landed" sting, rendered ONCE from a deterministic HTML
composition by a headless Chrome (through puppeteer-core) into JPEG frames, and played by radio_show at the
stream's frame rate. Rendering is an explicit, prepared step (), never part of
streaming; without the prepared frames the show uses its plain split-flap transitions instead.

The units carry no project text at all (the station name, the word for "landed", a tick and lights), so the same
frames serve every day and both editions.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

FPS = 10
UNITS = ("ident", "landed")
MANIFEST = "manifest.json"

HTML = r'''<!doctype html>
<html lang="he" dir="rtl">
<head>
<meta charset="utf-8">
<title>לוח המראות</title>
<style>
  :root { --bg: #0A0B0D; --panel: #121418; --tile: #1D2026; --amber: #FFC23D; --white: #F3F1EA; --dim: #6F7684; --green: #3DDC84; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { width: 1280px; height: 720px; overflow: hidden; background: var(--bg); font-family: -apple-system, "SF Hebrew", "Arial Hebrew", sans-serif; }
  #stage { position: absolute; inset: 0; }
  .panel { position: absolute; left: 24px; top: 20px; width: 1232px; height: 680px; background: var(--panel);
           border: 2px solid #262A31; border-radius: 14px; }
  .abs { position: absolute; }
  /* a flap: tile (one letter) or strip (a phrase) */
  .f { position: absolute; perspective: 500px; }
  .f .h { position: absolute; left: 0; right: 0; overflow: hidden; background: var(--tile); }
  .f .top, .f .lt { top: 0; height: 50%; border-radius: 5px 5px 0 0;
                    background: linear-gradient(#2A2E35, var(--tile)); }
  .f .bot, .f .lb { top: 50%; height: 50%; border-radius: 0 0 5px 5px; }
  .f .lt { transform-origin: 50% 100%; backface-visibility: hidden; visibility: hidden; }
  .f .lb { transform-origin: 50% 0%; visibility: hidden; }
  .f b { position: absolute; left: 0; right: 0; text-align: center; white-space: nowrap; font-weight: 800; }
  .f.strip b { text-align: right; padding: 0 16px; direction: rtl; }
  .f .seam { position: absolute; left: 0; right: 0; height: 2px; background: #000; opacity: .85; }
  .f .nub { position: absolute; width: 3px; height: 8px; background: #000; }
  .lamp { position: absolute; width: 14px; height: 14px; border-radius: 50%; }
  .seg { position: absolute; height: 14px; border-radius: 3px; background: #2C313A; }
  .txt { position: absolute; white-space: nowrap; direction: rtl; text-align: right; }
  .row-bg { position: absolute; left: 40px; width: 1208px; height: 88px; border-radius: 8px; background: #16191E; }
  .light { position: absolute; width: 12px; height: 12px; border-radius: 50%; background: #1F3A2A; }
</style>
</head>
<body>
<div id="stage"><div class="panel"></div></div>
<script>
// Deterministic: everything is a function of t (window.__seek). No data: these units carry no project text.
const S = document.getElementById('stage');
const LET = 'אבגדהוזחטיכלמנסעפצקרשת';
const FD = 0.07;                     // seconds per flip, like a real board
const clamp = (x, a = 0, b = 1) => Math.max(a, Math.min(b, x));
const ease = (x) => 1 - Math.pow(1 - clamp(x), 3);
let seed = 7;
const rnd = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);
const events = [];                   // flip times, for the sound track
const parts = [];                    // every animated part: {frame(t)}

function el(tag, cls, style, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  Object.assign(e.style, style || {});
  if (text !== undefined) e.textContent = text;
  S.appendChild(e);
  return e;
}

// A flap with phases [{at, seq:[...]}]: each phase flips through its sequence, FD seconds a step.
function flap(x, y, w, h, size, color, phases, opts = {}) {
  const box = el('div', 'f' + (opts.strip ? ' strip' : ''), { left: x + 'px', top: y + 'px', width: w + 'px', height: h + 'px' });
  const mk = (cls) => { const d = document.createElement('div'); d.className = 'h ' + cls; const b = document.createElement('b');
    Object.assign(b.style, { height: h + 'px', lineHeight: h + 'px', fontSize: size + 'px', color, top: cls === 'bot' || cls === 'lb' ? (-h / 2) + 'px' : '0px' });
    if (opts.font) b.style.fontWeight = opts.font; d.appendChild(b); box.appendChild(d); return [d, b]; };
  const [top, tb] = mk('top'), [bot, bb] = mk('bot'), [lt, ltb] = mk('lt'), [lb, lbb] = mk('lb');
  const seam = document.createElement('div'); seam.className = 'seam'; seam.style.top = (h / 2 - 1) + 'px'; box.appendChild(seam);
  for (const side of ['left', 'right']) { const n = document.createElement('div'); n.className = 'nub';
    Object.assign(n.style, { [side]: '-1px', top: (h / 2 - 4) + 'px' }); box.appendChild(n); }
  if (opts.bg) [top, bot, lt, lb].forEach((d) => (d.style.background = opts.bg));
  if (opts.lamp) { const l = document.createElement('div'); l.className = 'lamp';
    Object.assign(l.style, { right: '12px', top: (h / 2 - 7) + 'px', background: opts.lamp, boxShadow: `0 0 12px ${opts.lamp}` });
    box.appendChild(l); opts.lampEl = l; tb.style.paddingRight = bb.style.paddingRight = ltb.style.paddingRight = lbb.style.paddingRight = '36px'; }
  for (const p of phases) for (let k = 1; k < p.seq.length; k++) events.push({ t: p.at + (k - 1) * FD, kind: opts.strip ? 'strip' : 'tile' });
  const set = (b, s) => { if (b.__t !== s) { b.textContent = s; b.__t = s; } };
  // a flap exists on the board only from just before its first flip until its last (blanking) flip is done
  const startAt = phases[0].at, last = phases[phases.length - 1];
  const endAt = phases.length > 1 ? last.at + (last.seq.length - 1) * FD + 0.04 : Infinity;
  const part = { frame(t) {
    let ph = null;
    for (const p of phases) if (t >= p.at) ph = p;
    const vis = (opts.always ? 1 : clamp((t - startAt + 0.15) / 0.15)) * (1 - clamp((t - endAt) / 0.1));
    box.style.opacity = vis.toFixed(3); box.style.visibility = vis > 0.001 ? 'visible' : 'hidden';
    if (!ph) { const c = phases[0].seq[0]; set(tb, c); set(bb, c); lt.style.visibility = lb.style.visibility = 'hidden'; return; }
    const lt0 = t - ph.at, k = Math.floor(lt0 / FD);
    if (k >= ph.seq.length - 1) { const c = ph.seq[ph.seq.length - 1]; set(tb, c); set(bb, c);
      lt.style.visibility = lb.style.visibility = 'hidden'; }
    else {
      const cur = ph.seq[k], next = ph.seq[k + 1], q = lt0 / FD - k;
      set(tb, next); set(bb, cur); set(ltb, cur); set(lbb, next);
      if (q < 0.5) { lt.style.visibility = 'visible'; lb.style.visibility = 'hidden'; lt.style.transform = `rotateX(${(-q * 180).toFixed(1)}deg)`; }
      else { lt.style.visibility = 'hidden'; lb.style.visibility = 'visible'; lb.style.transform = `rotateX(${((1 - q) * 180).toFixed(1)}deg)`; }
    }
    if (opts.lampEl) { const on = t > opts.lampAt ? 0.55 + 0.45 * Math.cos((t - opts.lampAt) * Math.PI * 2 * 0.8) : 0;
      opts.lampEl.style.opacity = on.toFixed(2); }
  } };
  parts.push(part);
  return part;
}
const shuffle = (target, n, pool = LET) => [' ', ...Array.from({ length: n }, () => pool[Math.floor(rnd() * pool.length)]), target];
// a word in letter tiles, first letter in the rightmost tile
function word(s, xr, y, tw, th, size, color, at, opts = {}) {
  const chars = [...s];
  chars.forEach((ch, i) => {
    const x = xr - (i + 1) * tw - i * 3;
    const a = at + i * (opts.step ?? 0.035);
    let seq = ch === ' ' ? [' ', ' '] : shuffle(ch, opts.n ?? (2 + Math.floor(rnd() * 3)), opts.pool);
    if (opts.noBlank && ch !== ' ') seq = seq.slice(1);   // the first frame already shows letters (the thumbnail)
    const ph = [{ at: a, seq }];
    if (opts.out !== undefined) ph.push({ at: opts.out + (xr - x) * 0.0006, seq: [ch, ' '] });
    flap(x, y, tw, th, size, color, ph, { bg: opts.bg, always: opts.noBlank });
  });
}
// digits read left to right, right-aligned at xr
function digits(s, xr, y, tw, th, size, color, at, opts = {}) {
  const chars = [...s], x0 = xr - chars.length * (tw + 3) + 3;
  chars.forEach((ch, i) => {
    const pool = /\d/.test(ch) ? '0123456789' : ch;
    const ph = [{ at: at + i * 0.04, seq: opts.count && /\d/.test(ch) ? [' ', ...Array.from({ length: +ch + 1 }, (_, d) => String(d))] : shuffle(ch, 3, pool) }];
    if (opts.out !== undefined) ph.push({ at: opts.out, seq: [ch, ' '] });
    flap(x0 + i * (tw + 3), y, tw, th, size, color, ph, { bg: opts.bg });
  });
}
function text(s, x, y, w, size, color, weight, at, opts = {}) {
  const e = el('div', 'txt', { left: x + 'px', top: y + 'px', width: w + 'px', fontSize: size + 'px', color, fontWeight: weight,
    textAlign: opts.align || 'right', direction: opts.dir || 'rtl' }, s);
  parts.push({ frame(t) { const k = ease((t - at) / 0.35) * (opts.out !== undefined ? 1 - clamp((t - opts.out) / 0.25) : 1);
    e.style.opacity = k.toFixed(3); e.style.transform = `translateX(${((1 - ease((t - at) / 0.35)) * 30).toFixed(1)}px)`; } });
  return e;
}
function box(style, frame) { const e = el('div', 'abs', style); parts.push({ frame: (t) => frame(e, t) }); return e; }

const R = 1240, AMBER = '#FFC23D', WHITE = '#F3F1EA', DIM = '#6F7684', GREEN = '#3DDC84';

const SCENE = (location.hash || '#ident').slice(1);
const AMB = AMBER;
if (SCENE === 'ident') {
  // the station ident: letters shuffle and land on the station name, a line draws under the board's name
  word('רדיו הצוות', 640 + (10 * 73) / 2, 230, 70, 104, 66, AMB, 0.15, { step: 0.06, n: 6, noBlank: true });
  text('לוח המראות', 240, 372, 800, 34, WHITE, 800, 1.1, { align: 'center' });
  box({ left: '640px', top: '426px', height: '3px', width: '0px', background: AMB, boxShadow: `0 0 14px ${AMB}` }, (e, t) => {
    const w = 500 * ease((t - 1.2) / 0.7); e.style.width = w + 'px'; e.style.left = (640 - w / 2) + 'px'; });
  window.__duration = 2.6;
} else {
  // "landed": a green flash, the word in big flaps, a tick drawn in a ring, runway lights running right to left
  box({ left: '24px', top: '20px', width: '1232px', height: '680px', borderRadius: '14px', background: GREEN }, (e, t) =>
    (e.style.opacity = (clamp(t / 0.08) * (1 - clamp((t - 0.08) / 0.5)) * 0.22).toFixed(3)));
  word('נחת', 640 + (3 * 107) / 2, 210, 104, 150, 104, GREEN, 0.05, { step: 0.08, n: 4, noBlank: true });
  const chk = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  chk.setAttribute('viewBox', '0 0 120 120');
  Object.assign(chk.style, { position: 'absolute', left: '1000px', top: '216px', width: '140px', height: '140px' });
  chk.innerHTML = `<circle cx="60" cy="60" r="54" fill="none" stroke="${GREEN}" stroke-width="7" opacity=".35"/><path d="M30 62 L52 84 L92 38" fill="none" stroke="${GREEN}" stroke-width="12" stroke-linecap="round" stroke-linejoin="round" pathLength="1" stroke-dasharray="1" stroke-dashoffset="1"/>`;
  S.appendChild(chk);
  parts.push({ frame(t) { chk.style.opacity = clamp((t - 0.4) / 0.1).toFixed(2);
    chk.querySelector('path').setAttribute('stroke-dashoffset', (1 - ease((t - 0.45) / 0.45)).toFixed(3)); } });
  for (let k = 0; k < 24; k++) {
    box({ left: (1180 - k * 48) + 'px', top: '470px', width: '14px', height: '14px', borderRadius: '50%' }, (e, t) => {
      const ph = (t - 0.2) * 2.2 - k / 24; const lit = t > 0.2 ? Math.max(0, 1 - Math.abs(ph - Math.floor(ph + 0.5)) * 6) : 0;
      e.style.background = lit > 0.05 ? GREEN : '#1F3A2A'; e.style.boxShadow = lit > 0.05 ? `0 0 ${(18 * lit).toFixed(0)}px ${GREEN}` : 'none'; });
  }
  window.__duration = 2.2;
}
window.__seek = (t) => { for (const p of parts) p.frame(t); };
window.__seek(0);
</script>
</body>
</html>
'''

# puppeteer-core is resolved from the folder given (a folder that holds node_modules/puppeteer-core)
CAPTURE = r'''
const path = require('path');
const fs = require('fs');
const { createRequire } = require('module');
const [page_, outDir, modules, chrome, fps, units] = process.argv.slice(2);
const puppeteer = createRequire(path.join(modules, 'index.js'))('puppeteer-core');
(async () => {
  const browser = await puppeteer.launch({ executablePath: chrome, headless: true,
    args: ['--force-device-scale-factor=1', '--hide-scrollbars', '--no-first-run', '--allow-file-access-from-files'],
    defaultViewport: { width: 1280, height: 720, deviceScaleFactor: 1 } });
  try {
    const page = await browser.newPage();
    for (const unit of units.split(',')) {
      await page.goto('file://' + page_ + '#' + unit, { waitUntil: 'load' });
      await page.reload({ waitUntil: 'load' });
      await page.evaluate(() => document.fonts.ready);
      const dur = await page.evaluate(() => window.__duration);
      fs.mkdirSync(path.join(outDir, unit), { recursive: true });
      const n = Math.round(dur * Number(fps));
      for (let i = 0; i < n; i++) {
        await page.evaluate((t) => window.__seek(t), i / Number(fps));
        await page.screenshot({ path: path.join(outDir, unit, String(i).padStart(4, '0') + '.jpg'), type: 'jpeg', quality: 88 });
      }
    }
  } finally { await browser.close(); }
})().catch((e) => { console.error(String(e && e.message || e)); process.exit(1); });
'''


def source_digest() -> str:
    return hashlib.sha256((HTML + CAPTURE + str(FPS)).encode()).hexdigest()[:16]


def prepare(out_dir: Path, modules: str, chrome: str, node: str = "node", timeout: float = 600) -> str:
    """Render every unit's frames into out_dir/<unit>/ and write the manifest. Returns "" or why it failed."""
    if not modules or not Path(modules, "node_modules", "puppeteer-core").is_dir():
        return "puppeteer-core not found under the given folder"
    if not chrome or not Path(chrome).exists():
        return "Chrome not found"
    if not shutil.which(node) and not Path(node).exists():
        return "node not found"
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        page, script, staged = Path(tmp, "units.html"), Path(tmp, "capture.js"), Path(tmp, "frames")
        page.write_text(HTML, encoding="utf-8")
        script.write_text(CAPTURE, encoding="utf-8")
        try:
            r = subprocess.run([node, str(script), str(page), str(staged), str(Path(modules)), chrome, str(FPS),
                                ",".join(UNITS)], capture_output=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            return "render failed: " + e.__class__.__name__
        if r.returncode:
            return "render failed: " + r.stderr.decode("utf-8", "replace").strip()[-200:]
        counts = {}
        for unit in UNITS:
            frames = sorted(Path(staged, unit).glob("*.jpg"))
            if not frames:
                return f"no frames for {unit}"
            dest = out_dir / unit
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(Path(staged, unit), dest)
            counts[unit] = len(frames)
    (out_dir / MANIFEST).write_text(json.dumps({"digest": source_digest(), "fps": FPS, "frames": counts}),
                                    encoding="utf-8")
    return ""


def load(out_dir: Optional[Path], fps: int = FPS) -> Dict[str, List[bytes]]:
    """{unit: [JPEG frame bytes]} for the prepared units that match this source and frame rate; {} when there
    are none (the show then plays its plain transitions)."""
    if not out_dir:
        return {}
    try:
        man = json.loads((Path(out_dir) / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(man, dict) or man.get("digest") != source_digest() or man.get("fps") != fps:
        return {}
    counts = man.get("frames")
    if not isinstance(counts, dict):
        return {}
    out = {}
    for unit in UNITS:
        want = counts.get(unit)
        if type(want) is not int or want <= 0:
            continue
        files = sorted((Path(out_dir) / unit).glob("*.jpg"))
        if len(files) == want:
            try:
                out[unit] = [f.read_bytes() for f in files]
            except OSError:
                continue
    return out


def default_chrome() -> str:
    for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "/Applications/Chromium.app/Contents/MacOS/Chromium"):
        if os.path.exists(p):
            return p
    return shutil.which("google-chrome") or shutil.which("chromium") or ""
