"""The flight board's painter: a small display-list renderer (AppKit + CoreText, so Hebrew is laid out right to
left), compiled once with swiftc into the radio's folder like radio_card's renderer, and the slide layouts that
turn radio_board's slide dicts into display lists.

One painter process draws many frames (a slide transition is one launch). Frames end in .jpg (stream frames:
cheap to write and to decode) or .png (the staff card). Without a Swift compiler `available()` is False and the
radio keeps its plain card.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

from .config import private_dir

W, H = 1280, 720

SWIFT = r'''
// A display-list painter for the radio picture (the flight board).
// stdin: {"frames":[{"out": path, "w": 1280, "h": 720, "ops": [...]}]}  (top-left coordinates)
// Many frames per process, so a slide transition (a few frames) costs one launch.
// Ops: fill/stroke/grad (rect, r), text, line, circle, arc, poly, image, push/pop (translate, rotate, alpha), glow.
import AppKit
import Foundation

func color(_ v: Any?) -> NSColor {
    guard var s = v as? String, s.hasPrefix("#") else { return .clear }
    s.removeFirst()
    var n: UInt64 = 0
    Scanner(string: s).scanHexInt64(&n)
    if s.count == 6 { n = (n << 8) | 0xFF }
    return NSColor(calibratedRed: CGFloat((n >> 24) & 0xFF) / 255, green: CGFloat((n >> 16) & 0xFF) / 255,
                   blue: CGFloat((n >> 8) & 0xFF) / 255, alpha: CGFloat(n & 0xFF) / 255)
}
func num(_ o: [String: Any], _ k: String, _ d: CGFloat = 0) -> CGFloat {
    if let v = o[k] as? Double { return CGFloat(v) }
    if let v = o[k] as? Int { return CGFloat(v) }
    return d
}
let weights: [String: NSFont.Weight] = ["thin": .thin, "light": .light, "regular": .regular, "medium": .medium,
                                         "semibold": .semibold, "bold": .bold, "heavy": .heavy, "black": .black]
var fontCache: [String: NSFont] = [:]
func font(_ o: [String: Any]) -> NSFont {
    let size = num(o, "size", 24), w = weights[o["weight"] as? String ?? "regular"] ?? .regular
    let family = o["font"] as? String ?? "system"
    let key = "\(family)|\(size)|\(w.rawValue)"
    if let f = fontCache[key] { return f }
    var f = NSFont.systemFont(ofSize: size, weight: w)
    switch family {
    case "system": break
    case "rounded":
        if let d = f.fontDescriptor.withDesign(.rounded), let r = NSFont(descriptor: d, size: size) { f = r }
    case "mono": f = NSFont.monospacedDigitSystemFont(ofSize: size, weight: w)
    case "monofull": f = NSFont.monospacedSystemFont(ofSize: size, weight: w)
    default:
        if let named = NSFont(name: family, size: size) { f = named }
    }
    fontCache[key] = f
    return f
}
func path(_ o: [String: Any]) -> NSBezierPath {
    let r = NSRect(x: num(o, "x"), y: num(o, "y"), width: num(o, "w"), height: num(o, "h"))
    let rad = num(o, "r")
    return rad > 0 ? NSBezierPath(roundedRect: r, xRadius: rad, yRadius: rad) : NSBezierPath(rect: r)
}
func withGlow(_ o: [String: Any], _ body: () -> Void) {
    if let g = o["glow"] {
        NSGraphicsContext.saveGraphicsState()
        let sh = NSShadow()
        sh.shadowColor = color(g)
        sh.shadowBlurRadius = num(o, "blur", 14)
        sh.shadowOffset = NSSize(width: num(o, "dx"), height: -num(o, "dy"))
        sh.set()
        body()
        NSGraphicsContext.restoreGraphicsState()
    } else { body() }
}
var images: [String: CGImage] = [:]

func paint(_ frame: [String: Any]) -> Bool {
    let W = Int(num(frame, "w", 1280)), H = Int(num(frame, "h", 720))
    guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: W, pixelsHigh: H, bitsPerSample: 8,
                                     samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                     bytesPerRow: 0, bitsPerPixel: 0),
          let ctx = NSGraphicsContext(bitmapImageRep: rep) else { return false }
    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = ctx
    // flip to top-left coordinates
    let flip = NSAffineTransform()
    flip.translateX(by: 0, yBy: CGFloat(H))
    flip.scaleX(by: 1, yBy: -1)
    flip.concat()
    let cg = ctx.cgContext
    for case let o as [String: Any] in frame["ops"] as? [Any] ?? [] {
        switch o["op"] as? String ?? "" {
        case "fill":
            withGlow(o) { color(o["c"]).setFill(); path(o).fill() }
        case "stroke":
            let p = path(o); p.lineWidth = num(o, "lw", 1)
            if let d = o["dash"] as? [Double] { p.setLineDash(d.map { CGFloat($0) }, count: d.count, phase: 0) }
            withGlow(o) { color(o["c"]).setStroke(); p.stroke() }
        case "grad":
            let cs = (o["cs"] as? [Any] ?? []).map { color($0) }
            if cs.count >= 2, let g = NSGradient(colors: cs) {
                // angle in degrees, 0 = left to right, 90 = top to bottom (top-left space)
                g.draw(in: path(o), angle: -num(o, "angle", 90))
            }
        case "radial":
            let cs = (o["cs"] as? [Any] ?? []).map { color($0) }
            if cs.count >= 2, let g = NSGradient(colors: cs) {
                let c = NSPoint(x: num(o, "cx"), y: num(o, "cy"))
                g.draw(fromCenter: c, radius: 0, toCenter: c, radius: num(o, "rad", 100), options: [])
            }
        case "line":
            let p = NSBezierPath()
            p.move(to: NSPoint(x: num(o, "x1"), y: num(o, "y1"))); p.line(to: NSPoint(x: num(o, "x2"), y: num(o, "y2")))
            p.lineWidth = num(o, "lw", 1)
            p.lineCapStyle = (o["cap"] as? String) == "round" ? .round : .butt
            if let d = o["dash"] as? [Double] { p.setLineDash(d.map { CGFloat($0) }, count: d.count, phase: 0) }
            withGlow(o) { color(o["c"]).setStroke(); p.stroke() }
        case "circle":
            let r = num(o, "r")
            let p = NSBezierPath(ovalIn: NSRect(x: num(o, "cx") - r, y: num(o, "cy") - r, width: 2 * r, height: 2 * r))
            withGlow(o) {
                if o["c"] != nil { color(o["c"]).setFill(); p.fill() }
                if o["sc"] != nil { p.lineWidth = num(o, "lw", 1); color(o["sc"]).setStroke(); p.stroke() }
            }
        case "arc":
            // degrees clockwise from 12 o'clock (top-left space)
            let p = NSBezierPath()
            let a0 = num(o, "a0") - 90, a1 = num(o, "a1") - 90
            p.appendArc(withCenter: NSPoint(x: num(o, "cx"), y: num(o, "cy")), radius: num(o, "r"),
                        startAngle: a0, endAngle: a1, clockwise: false)
            if (o["wedge"] as? Bool) == true {
                p.line(to: NSPoint(x: num(o, "cx"), y: num(o, "cy"))); p.close()
                withGlow(o) { color(o["c"]).setFill(); p.fill() }
            } else {
                p.lineWidth = num(o, "lw", 4); p.lineCapStyle = (o["cap"] as? String) == "butt" ? .butt : .round
                withGlow(o) { color(o["c"]).setStroke(); p.stroke() }
            }
        case "poly":
            let pts = o["pts"] as? [[Double]] ?? []
            guard pts.count >= 2 else { break }
            let p = NSBezierPath()
            p.move(to: NSPoint(x: pts[0][0], y: pts[0][1]))
            for q in pts.dropFirst() { p.line(to: NSPoint(x: q[0], y: q[1])) }
            if (o["open"] as? Bool) != true { p.close() }
            p.lineJoinStyle = .round; p.lineCapStyle = .round
            withGlow(o) {
                if o["c"] != nil { color(o["c"]).setFill(); p.fill() }
                if o["sc"] != nil { p.lineWidth = num(o, "lw", 1); color(o["sc"]).setStroke(); p.stroke() }
            }
        case "text":
            let s = o["s"] as? String ?? ""
            let rtl = (o["dir"] as? String ?? "rtl") == "rtl"
            let p = NSMutableParagraphStyle()
            p.alignment = ["r": .right, "l": .left, "c": .center][o["align"] as? String ?? (rtl ? "r" : "l")] ?? .natural
            p.baseWritingDirection = rtl ? .rightToLeft : .leftToRight
            p.lineBreakMode = (o["wrap"] as? Bool) == true ? .byWordWrapping : .byTruncatingTail
            p.lineHeightMultiple = num(o, "lh", 1.0)
            var attrs: [NSAttributedString.Key: Any] = [.font: font(o), .foregroundColor: color(o["c"] ?? "#FFFFFF"),
                                                        .paragraphStyle: p]
            if o["kern"] != nil { attrs[.kern] = num(o, "kern") }
            let a = NSAttributedString(string: s, attributes: attrs)
            let box = NSRect(x: num(o, "x"), y: num(o, "y"), width: num(o, "w"), height: num(o, "h"))
            let fit = a.boundingRect(with: box.size, options: [.usesLineFragmentOrigin, .truncatesLastVisibleLine])
            let v = o["v"] as? String ?? "c"
            let dy: CGFloat = v == "t" ? 0 : v == "b" ? box.height - ceil(fit.height) : (box.height - ceil(fit.height)) / 2
            let r = NSRect(x: box.minX, y: box.minY + max(0, dy), width: box.width, height: min(box.height, ceil(fit.height) + 2))
            withGlow(o) {
                NSGraphicsContext.saveGraphicsState()
                // text must be drawn unflipped: use a flipped-aware context
                let t = NSAffineTransform(); t.translateX(by: 0, yBy: r.minY + r.height); t.scaleX(by: 1, yBy: -1); t.concat()
                a.draw(with: NSRect(x: r.minX, y: 0, width: r.width, height: r.height),
                       options: [.usesLineFragmentOrigin, .truncatesLastVisibleLine])
                NSGraphicsContext.restoreGraphicsState()
            }
        case "image":
            // decoded once per source file; a source rect is a cheap crop of the decoded pixels (no copy)
            let src = o["src"] as? String ?? ""
            if images[src] == nil, let ns = NSImage(contentsOfFile: src) {
                var r = NSRect(origin: .zero, size: ns.size)
                images[src] = ns.cgImage(forProposedRect: &r, context: nil, hints: nil)
            }
            if let img = images[src] {
                var part: CGImage? = img
                if let sr = o["sr"] as? [Double], sr.count == 4 {  // source rect, top-left image pixels
                    part = img.cropping(to: CGRect(x: sr[0], y: sr[1], width: sr[2], height: sr[3]))
                }
                if let part = part {
                    let r = CGRect(x: num(o, "x"), y: num(o, "y"), width: num(o, "w"), height: num(o, "h"))
                    cg.saveGState()
                    cg.translateBy(x: 0, y: r.minY + r.height); cg.scaleBy(x: 1, y: -1)
                    cg.interpolationQuality = .low
                    if o["alpha"] != nil { cg.setAlpha(num(o, "alpha", 1)) }
                    cg.draw(part, in: CGRect(x: r.minX, y: 0, width: r.width, height: r.height))
                    cg.restoreGState()
                }
            }
        case "push":
            cg.saveGState()
            cg.translateBy(x: num(o, "tx"), y: num(o, "ty"))
            if o["rot"] != nil { cg.rotate(by: num(o, "rot") * .pi / 180) }
            if o["sy"] != nil { cg.scaleBy(x: 1, y: num(o, "sy", 1)) }
            if o["alpha"] != nil { cg.setAlpha(num(o, "alpha", 1)) }
            if o["clip"] != nil, let c = o["clip"] as? [String: Any] { path(c).addClip() }
        case "pop":
            cg.restoreGState()
        default: break
        }
    }
    NSGraphicsContext.restoreGraphicsState()
    let out = URL(fileURLWithPath: frame["out"] as? String ?? "")
    let jpeg = out.pathExtension.lowercased() == "jpg"  // JPEG for stream frames: much cheaper to write than PNG
    guard let png = jpeg ? rep.representation(using: .jpeg, properties: [.compressionFactor: 0.88])
                         : rep.representation(using: .png, properties: [:]) else { return false }
    let tmp = out.deletingLastPathComponent().appendingPathComponent(".paint-\(getpid()).png")
    do { try png.write(to: tmp) } catch { return false }
    return rename(tmp.path, out.path) == 0
}

let data = FileHandle.standardInput.readDataToEndOfFile()
guard let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
      let frames = root["frames"] as? [[String: Any]] else { exit(2) }
var bad = 0
for f in frames { if !paint(f) { bad += 1 } }
exit(bad == 0 ? 0 : 4)
'''


def _binary(folder: Path) -> Optional[Path]:
    """The compiled painter, built once per source version; None without a Swift compiler (or off macOS)."""
    if os.uname().sysname != "Darwin":
        return None
    digest = hashlib.sha256(SWIFT.encode()).hexdigest()[:12]
    exe = folder / f"paint-{digest}"
    if exe.exists():
        return exe
    swiftc = shutil.which("swiftc")
    if not swiftc:
        return None
    private_dir(folder)
    src = folder / f"paint-{digest}.swift"
    src.write_text(SWIFT, encoding="utf-8")
    try:
        r = subprocess.run([swiftc, "-O", "-o", str(exe) + ".tmp", str(src)], capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode or not Path(str(exe) + ".tmp").exists():
        return None
    os.replace(str(exe) + ".tmp", exe)
    for old in folder.glob("paint-*"):
        if old.name not in (exe.name, src.name):
            try:
                old.unlink()
            except OSError:
                pass
    return exe


class Frame:
    """A display list for the painter. Top-left coordinates in pixels; colours "#RRGGBB" or "#RRGGBBAA"."""

    def __init__(self, out: str, w: int = W, h: int = H):
        self.out, self.w, self.h, self.ops = str(out), w, h, []

    def _add(self, op, **kw):
        self.ops.append({"op": op, **{k: v for k, v in kw.items() if v is not None}})

    def fill(self, x, y, w, h, c, r=0, **kw):
        self._add("fill", x=x, y=y, w=w, h=h, c=c, r=r, **kw)

    def stroke(self, x, y, w, h, c, lw=1, r=0, **kw):
        self._add("stroke", x=x, y=y, w=w, h=h, c=c, lw=lw, r=r, **kw)

    def grad(self, x, y, w, h, cs, angle=90, r=0):
        self._add("grad", x=x, y=y, w=w, h=h, cs=cs, angle=angle, r=r)

    def line(self, x1, y1, x2, y2, c, lw=1, **kw):
        self._add("line", x1=x1, y1=y1, x2=x2, y2=y2, c=c, lw=lw, **kw)

    def circle(self, cx, cy, r, c=None, sc=None, lw=None, **kw):
        self._add("circle", cx=cx, cy=cy, r=r, c=c, sc=sc, lw=lw, **kw)

    def arc(self, cx, cy, r, a0, a1, c, lw=4, **kw):
        self._add("arc", cx=cx, cy=cy, r=r, a0=a0, a1=a1, c=c, lw=lw, **kw)

    def poly(self, pts, c=None, sc=None, lw=None, **kw):
        self._add("poly", pts=pts, c=c, sc=sc, lw=lw, **kw)

    def image(self, src, x, y, w, h, sr=None):
        self._add("image", src=str(src), x=x, y=y, w=w, h=h, sr=sr)

    def radial(self, cx, cy, rad, cs):
        self._add("radial", cx=cx, cy=cy, rad=rad, cs=cs)

    def push(self, tx=0, ty=0, rot=None, alpha=None, clip=None, sy=None):
        """Until the matching pop: move the origin, turn (degrees), fade, clip or squash what follows."""
        self._add("push", tx=tx, ty=ty, rot=rot, alpha=alpha, clip=clip, sy=sy)

    def pop(self):
        self._add("pop")

    def text(self, s, x, y, w, h, size, c="#FFFFFF", weight="regular", align=None, font="system", dir="rtl",
             v="c", wrap=None, lh=None, **kw):
        self._add("text", s=s, x=x, y=y, w=w, h=h, size=size, c=c, weight=weight, align=align, font=font, dir=dir,
                  v=v, wrap=wrap, lh=lh, **kw)

    def data(self) -> dict:
        return {"out": self.out, "w": self.w, "h": self.h, "ops": self.ops}


def paint(folder: Path, frames: List[Frame], timeout: float = 60) -> bool:
    """Draw every frame in one painter launch. False when the painter is missing or failed (the caller falls back)."""
    exe = _binary(folder)
    if exe is None or not frames:
        return False
    try:
        r = subprocess.run([str(exe)], input=json.dumps({"frames": [f.data() for f in frames]},
                                                       ensure_ascii=False).encode("utf-8"),
                           capture_output=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def available(folder: Path) -> bool:
    return _binary(folder) is not None


# --- the look ------------------------------------------------------------------------------------------------
# Two families on one stage. The overview (one glowing column per project, a quiet radar) is the control tower;
# the walls (paper notes in three columns: done, in work, next up) and the owner's desk are the newsroom. Counts are
# split-flap numerals; a news ticker runs along the bottom of every slide (radio_show scrolls it).

R = 1240                 # the content's right edge
TICK_Y, TICK_H = 662, 58  # the ticker band; its moving part is x 0..TICK_W, the label sits right of it
TICK_W = 1100
FOOT_Y = 620             # the footer line ("next on the board")

# the tower (overview, staff)
NAVY0, NAVY1, CARD, LINE = "#050B16", "#0B1B33", "#0E2240", "#1C3A63"
CYAN, MINT, AMBER, WHITE, DIM, SOFT = "#39D5FF", "#4BE3A0", "#FFB547", "#EEF5FF", "#7F95B5", "#AFC0D8"
STATUS_C = {"amber": AMBER, "green": MINT, "violet": "#B892FF", "blue": "#7CC4FF", "gray": "#8A93A3"}
# the newsroom (walls, desk, ticker)
ROOM, INK, CREAM, RED, MUTED = "#17171A", "#1B1B1F", "#EDE6D6", "#E8452C", "#8E8A82"
PAPER = {"done": "#DCEFD9", "doing": "#FFF1B8", "todo": "#F3EEE4"}
TAPE = {"done": "#3DBB6B", "doing": "#F5A623", "todo": "#B9B2A3"}
FLAP_BG, FLAP_FG = "#1D2026", "#FFC23D"

from .i18n import RADIO_PAINT_TEXT as TEXT


def _dir(text: str) -> str:
    from .radio_board import direction
    return direction(text)


def _lang(sl: dict):
    lang = sl.get("lang", "he") if sl.get("lang") in ("he", "en") else "he"
    return TEXT[lang], "rtl" if lang == "he" else "ltr"


def _status(key: str):
    from .radio_board import STATUS
    return STATUS.get(key, STATUS["hold"])


# --- split-flap numerals (and letters) --------------------------------------------------------------------

def tile(f: Frame, x, y, w, h, ch, fg=FLAP_FG, size=None, bg=FLAP_BG, font="system"):
    f.fill(x, y, w, h, bg, r=max(3, w * 0.1))
    f.grad(x, y, w, h / 2, ["#FFFFFF14", "#FFFFFF02"], angle=90, r=max(3, w * 0.1))
    if ch.strip():
        f.text(ch, x, y + 1, w, h, size or round(h * 0.6), fg, "heavy", align="c", dir="ltr", font=font)
    f.line(x, y + h / 2, x + w, y + h / 2, "#000000CC", max(1.5, h / 30))


def fit_tiles(name: str, most: int) -> str:
    name = " ".join(name.split())
    return name if len(name) <= most else name[:most - 1].rstrip() + "…"


_LTR_RUN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9 .:/%_-]*[A-Za-z0-9%])?")


def visual(s: str) -> List[str]:
    """The characters of `s` in left-to-right display order, for one-character tiles. Latin and number runs keep
    their own order; in a Hebrew (or neutral) text the runs are laid out right to left, so "בית ABC" shows
    A, B, C, space, ת, י, ב."""
    runs, at = [], 0
    for m in _LTR_RUN.finditer(s):
        if m.start() > at:
            runs.append(("rtl", s[at:m.start()]))
        runs.append(("ltr", m.group(0)))
        at = m.end()
    if at < len(s):
        runs.append(("rtl", s[at:]))
    if _dir(s) == "ltr":   # a Latin text: runs left to right, a Hebrew run inside still reads right to left
        out: List[str] = []
        for kind, r in runs:
            if kind == "ltr":
                out += list(r)
            else:  # spaces at the run's edges stay where they are; only its letters turn around
                core = r.strip(" ")
                lead = r[:len(r) - len(r.lstrip(" "))]
                out += list(lead) + list(reversed(core)) + list(r[len(lead) + len(core):])
        return out
    return [c for kind, r in reversed(runs) for c in (r if kind == "ltr" else reversed(r))]


def flaps(f: Frame, s: str, xr, y, tw, th, most: int, gap=3, **kw):
    """A word in letter tiles, right-aligned at xr, in display order (visual); shortened to `most` tiles."""
    chars = visual(fit_tiles(s, most))
    chars = [" "] * (most - len(chars)) + chars
    x0 = xr - len(chars) * (tw + gap) + gap
    for i, ch in enumerate(chars):
        tile(f, x0 + i * (tw + gap), y, tw, th, ch, **kw)
    return x0


def digits(f: Frame, s: str, xr, y, tw, th, gap=3, **kw):
    """Digits (and ":" "/" "%") in tiles, read left to right, right-aligned at xr. Returns the left edge."""
    x0 = xr - len(s) * (tw + gap) + gap
    for i, ch in enumerate(s):
        tile(f, x0 + i * (tw + gap), y, tw, th, ch, font="mono", **kw)
    return x0


def fraction(f: Frame, done: int, total: int, xr, y, tw, th, size, done_c=FLAP_FG, total_c="#C9CED8"):
    """done/total as tiles, ALWAYS read left to right (done on the left, total on the right), right-aligned at xr.
    Drawn as three groups placed from the right, so the order cannot follow a right-to-left layout."""
    x = digits(f, str(min(total, 99)), xr, y, tw, th, fg=total_c, size=size)
    x = digits(f, "/", x - 3, y, tw * 0.6, th, fg="#6F7684", size=size)
    return digits(f, str(min(done, 99)), x - 3, y, tw, th, fg=done_c, size=size)


def fraction_width(done: int, total: int, tw: float) -> float:
    return (len(str(min(done, 99))) + len(str(min(total, 99)))) * (tw + 3) + tw * 0.6 + 9


# --- shared chrome -------------------------------------------------------------------------------------------

def next_line(sl: dict, t: dict) -> str:
    """The footer's "next: ..." (a project name inside stays isolated: no mixed line)."""
    from .radio_script import isolate
    nxt = sl.get("next") or {}
    kind, name = nxt.get("kind"), isolate(nxt.get("name") or "")
    what = {"overview": t["next_overview"], "waiting": t["next_waiting"],
            "wall": t["next_wall"].format(name=name) if name else t["next_combined"]}.get(kind, "")
    return f"{t['next']} {what}" if what else ""


def page_dots(f: Frame, cur: int, n: int, on=CYAN, off=LINE):
    if n > 1:
        for i in range(n):
            f.fill(60 + i * 26, FOOT_Y + 18, 18, 8, on if i == cur else off, r=4)


def footer(f: Frame, sl: dict, t: dict, d: str, colour=DIM, rule=LINE):
    line = next_line(sl, t)
    f.line(48, FOOT_Y, R, FOOT_Y, rule, 1.5)
    if line:
        f.text(line, 400, FOOT_Y + 6, R - 400, 32, 20, colour, "semibold", dir=d)


def ticker_band(f: Frame, t: dict, d: str, strip: Optional[str] = None):
    """The band the ticker runs in: its label on the reading side; the moving part (x 0..TICK_W) is laid over the
    stream by the encoder. With `strip` (a still frame), the ticker's opening window is drawn in place."""
    f.fill(0, TICK_Y, W, TICK_H, "#0E0E10")
    f.fill(0, TICK_Y, W, 3, RED)
    f.fill(TICK_W, TICK_Y + 3, W - TICK_W, TICK_H - 3, RED)
    f.text(t["ticker"], TICK_W, TICK_Y + 3, W - TICK_W, TICK_H - 3, 26, "#FFFFFF", "heavy", align="c", dir=d)
    if strip:
        f.image(strip, 0, TICK_Y, TICK_W, TICK_H, sr=[STRIP_W - TICK_W, 0, TICK_W, TICK_H])


def chip(f: Frame, x, y, w, h, s, colour, d):
    f.fill(x, y, w, h, colour + "26", r=h / 2)
    f.text(s, x, y, w, h, round(h * 0.56), colour, "bold", align="c", dir=d)


def ring(f: Frame, cx, cy, r, frac, colour, lw=12):
    f.circle(cx, cy, r, sc=LINE, lw=lw)
    if frac > 0:
        f.arc(cx, cy, r, 0, 360 * min(frac, 1.0), colour, lw=lw, glow=colour + "99", blur=12)


def tower_stage(f: Frame, sl: dict, title: str, sub: str, t: dict, d: str, edition: str = ""):
    f.grad(0, 0, W, H, [NAVY1, NAVY0], angle=90)
    for rr in (150, 270, 390, 510):  # the radar: rings only, never cross lines
        f.circle(110, 760, rr, sc="#1B3A6650", lw=2)
    f.circle(R - 12, 50, 8, c=CYAN, glow=CYAN, blur=16)
    f.text(title, 640, 24, R - 640 - 32, 52, 42, WHITE, "heavy", dir=_dir(title))
    f.text(sub, 640, 76, R - 640 - 32, 26, 19, CYAN, "semibold", dir=_dir(sub))
    digits(f, sl.get("clock", "--:--"), 40 + 5 * 37, 30, 34, 52, fg=WHITE, size=32)
    if edition:
        f.fill(250, 38, 200, 38, "#12305A", r=19)
        f.stroke(250, 38, 200, 38, CYAN, 1.5, r=19)
        f.text(edition, 250, 38, 200, 38, 19, CYAN, "bold", align="c", dir=d)


def empty(f: Frame, big: str, small: str, colour, sub_colour, y=290):
    f.text(big, 140, y, 1000, 66, 46, colour, "heavy", align="c", dir=_dir(big))
    f.text(small, 140, y + 70, 1000, 40, 25, sub_colour, "semibold", align="c", dir=_dir(small))


# --- the overview: a glowing column per project --------------------------------------------------------------

def _grid(n: int):
    """[(x, y, w, h)] for n project cards: one row up to five (centred, at most 300 wide), else two rows of four."""
    top, bottom, gap = 122, FOOT_Y - 14, 14
    if n <= 0:
        return []
    if n <= 5:
        w = min(300.0, (1200 - (n - 1) * gap) / n)
        span = n * w + (n - 1) * gap
        right = 640 + span / 2
        return [(right - (i + 1) * w - i * gap, top, w, bottom - top) for i in range(n)]
    w, h = (1200 - 3 * gap) / 4, (bottom - top - gap) / 2
    return [(R - (i % 4 + 1) * w - (i % 4) * gap, top + (i // 4) * (h + gap), w, h) for i in range(n)]


def _crew(f: Frame, cx, y, sessions, working, agents):
    marks = [("s", k < working) for k in range(min(sessions, 6))] + [("a", True) for _ in range(min(agents, 4))]
    x = cx + (len(marks) - 1) * 10
    for kind, on in marks:
        if kind == "s":
            f.circle(x, y, 7, c=MINT if on else None, sc=None if on else DIM, lw=2, glow=MINT if on else None, blur=8)
        else:
            f.fill(x - 6, y - 6, 12, 12, CYAN, r=3)
        x -= 20


def overview(sl: dict, out: str) -> Frame:
    t, d = _lang(sl)
    f = Frame(out)
    tower_stage(f, sl, t["overview"], t["overview_sub"], t, d)
    cols = sl["cols"]
    if not cols:
        empty(f, t["no_projects"], t["no_projects_sub"], WHITE, SOFT)
    big = len(cols) <= 5
    for (x, y, w, h), c in zip(_grid(len(cols)), cols):
        word_he, word_en, role = _status(c["status"])
        col = STATUS_C[role]
        f.fill(x, y, w, h, CARD + "E8", r=16)
        f.stroke(x, y, w, h, LINE, 1.5, r=16)
        f.fill(x + 12, y, w - 24, 5, col, r=2)
        if big:
            f.text(c["name"], x + 12, y + 16, w - 24, 70, 28, WHITE, "heavy", align="c", wrap=True, lh=0.95,
                   dir=_dir(c["name"]))
            chip(f, x + w / 2 - 70, y + 92, 140, 30, word_he if d == "rtl" else word_en, col, d)
            r, cy = min(78.0, w / 2 - 34), y + 220
        else:
            f.text(c["name"], x + 12, y + 12, w - 24, 34, 24, WHITE, "heavy", align="c", dir=_dir(c["name"]))
            r, cy = 50.0, y + 112
        frac = c["done"] / c["total"] if c["total"] else 0.0
        ring(f, x + w / 2, cy, r, frac, col if c["status"] != "hold" else DIM, lw=12 if big else 9)
        tw = 22.0 if big else 19.0
        fw = fraction_width(c["done"], c["total"], tw)
        fraction(f, c["done"], c["total"], x + w / 2 + fw / 2, cy - tw * 0.8, tw, tw * 1.6, round(tw * 0.95),
                 done_c=col if c["status"] != "hold" else SOFT, total_c=SOFT)
        if big:
            f.text(t["done_of"], x, cy + r + 8, w, 26, 18, DIM, "bold", align="c", dir=d)
            _crew(f, x + w / 2, cy + r + 52, c["sessions"], c["working"], c["agents"])
            if c["now"]:
                f.text(t["now"], x + 14, y + 352, w - 28, 24, 16, CYAN, "bold", align="c", dir=d)
                f.text(c["now"], x + 14, y + 376, w - 28, 54, 19, WHITE, "semibold", align="c", wrap=True, lh=0.95,
                       dir=_dir(c["now"]))
        else:  # one bottom row: the state, and beside it what waits for the owner
            word = word_he if d == "rtl" else word_en
            if c["waiting"]:
                chip(f, x + 12, y + h - 40, w / 2 - 18, 28, word, col, d)
                bx, bw = x + w / 2 + 6, w / 2 - 18
                f.fill(bx, y + h - 40, bw, 28, AMBER + "22", r=14)
                f.stroke(bx, y + h - 40, bw, 28, AMBER, 1.5, r=14)
                f.text(t["waits"], bx + 34, y + h - 40, bw - 44, 28, 15, AMBER, "bold", dir=d)
                digits(f, str(min(c["waiting"], 9)), bx + 28, y + h - 37, 17, 22, fg="#1A1200", bg=AMBER, size=14)
            else:
                chip(f, x + w / 2 - 60, y + h - 40, 120, 28, word, col, d)
        if c["waiting"] and big:
            by = y + h - (52 if big else 42)
            bh = 38 if big else 30
            f.fill(x + 12, by, w - 24, bh, AMBER + "22", r=10)
            f.stroke(x + 12, by, w - 24, bh, AMBER, 1.5, r=10)
            f.text(t["waits"], x + 60, by, w - 84, bh, 18 if big else 16, AMBER, "bold", dir=d)
            digits(f, str(min(c["waiting"], 9)), x + 50, by + 4, bh * 0.62, bh - 8, fg="#1A1200", bg=AMBER,
                   size=round(bh * 0.55))
    footer(f, sl, t, d)
    page_dots(f, sl.get("page", 0), sl.get("pages", 1))
    ticker_band(f, t, d, sl.get("_strip"))
    return f


def staff(sl: dict, out: str) -> Frame:
    """The staff overview: each listed project's name and two counts (radio_board.staff_slide), nothing else."""
    t, d = _lang(sl)
    f = Frame(out)
    tower_stage(f, sl, sl.get("title") or t["overview"], t["staff_sub"], t, d, edition=t["edition"])
    rows = sl["rows"]
    if not rows:
        empty(f, t["nothing_listed"], t["staff_foot"], WHITE, SOFT)
    big = len(rows) <= 5
    for (x, y, w, h), r in zip(_grid(len(rows)), rows):
        f.fill(x, y, w, h, CARD + "E8", r=16)
        f.stroke(x, y, w, h, LINE, 1.5, r=16)
        f.text(r["name"], x + 12, y + 14, w - 24, 34 if not big else 66, 24 if not big else 28, WHITE, "heavy",
               align="c", wrap=big, lh=0.95, dir=_dir(r["name"]))
        n = r["done"] + r["open"]
        cy, rad = (y + 190, min(70.0, w / 2 - 40)) if big else (y + 96, 40.0)
        ring(f, x + w / 2, cy, rad, r["done"] / n if n else 0.0, MINT, lw=12 if big else 8)
        tw = 30.0 if big else 20.0
        rows_y = (y + 300, y + 400) if big else (y + 150, y + 196)
        for (label, value, colour), ry in zip(((t["done_today"], r["done"], MINT), (t["in_work"], r["open"], AMBER)), rows_y):
            v = str(min(value, 999))
            if big:
                digits(f, v, x + w / 2 + len(v) * (tw + 3) / 2, ry, tw, tw * 1.5, fg=colour, size=round(tw * 0.95))
                f.text(label, x, ry + tw * 1.5 + 6, w, 26, 18, DIM, "bold", align="c", dir=d)
            else:
                f.text(label, x + w / 2 + 6, ry, w / 2 - 18, tw * 1.5, 16, DIM, "bold", dir=d)
                digits(f, v, x + w / 2 - 6, ry, tw, tw * 1.5, fg=colour, size=round(tw * 0.95))
    f.line(48, FOOT_Y, R, FOOT_Y, LINE, 1.5)
    f.text(t["staff_foot"], 400, FOOT_Y + 6, R - 400, 32, 20, DIM, "semibold", dir=d)
    return f


# --- the walls: paper notes in three columns ---------------------------------------------------------------

def _stamp(f: Frame, cx, cy, k: float = 1.0, alpha: float = 1.0):
    """A green check stamped on a note (a ring and a tick; k > 1 while it is being stamped on)."""
    f.push(tx=cx, ty=cy, rot=-14, alpha=alpha)
    rr = 20 * k
    f.circle(0, 0, rr, sc="#2E9E5BDD", lw=3.5 * k)
    f.poly([[-9 * k, 1 * k], [-2 * k, 8 * k], [11 * k, -7 * k]], sc="#2E9E5B", lw=5 * k, open=True)
    f.pop()


def note(f: Frame, x, y, w, h, text: str, part: str, rot: float, tag: str = "", stamp: Optional[float] = None):
    """One paper note with a little depth (a soft shadow, a strip of tape, a shine). `stamp`: the check's scale
    (None: no check)."""
    f.push(tx=x + w / 2, ty=y + h / 2, rot=rot)
    f.fill(-w / 2, -h / 2, w, h, PAPER[part], r=3, glow="#00000099", blur=12, dy=6)
    f.grad(-w / 2, -h / 2, w, h * 0.45, ["#FFFFFF55", "#FFFFFF00"], angle=90, r=3)
    f.fill(-26, -h / 2 - 7, 52, 15, TAPE[part] + "AA", r=2)
    pad_r = 16
    if tag:
        f.text(tag, -w / 2 + 16, -h / 2 + 10, w - 32, 22, 16, "#6B6457", "bold", dir=_dir(tag), align="r")
    top = -h / 2 + (34 if tag else 12)
    f.text(text, -w / 2 + (56 if stamp is not None else 16), top, w - pad_r - (56 if stamp is not None else 16),
           h / 2 - top - 10, 22, INK, "bold", wrap=True, lh=1.0, v="t", dir=_dir(text), align="r")
    f.pop()
    if stamp is not None:
        _stamp(f, x + 34, y + h / 2, k=stamp, alpha=min(1.0, 1.6 - stamp * 0.6) if stamp > 1 else 1.0)


def wall(sl: dict, out: str, stamp: Optional[float] = None) -> Frame:
    """A project's wall (or the combined wall). `stamp`: the fresh note's check being stamped on (scale)."""
    t, d = _lang(sl)
    f = Frame(out)
    f.fill(0, 0, W, H, ROOM)
    title = sl.get("name") or t["all_projects"]
    f.text(title, 420, 18, R - 420, 60, 46, CREAM, "heavy", dir=_dir(title))
    f.text(t["wall_sub"], 420, 78, R - 420, 26, 19, MUTED, "semibold", dir=d)
    fw = fraction_width(sl["done_n"], sl["total"], 32)
    fraction(f, sl["done_n"], sl["total"], 48 + fw, 26, 32, 52, 32)
    f.text(t["done_of"], 60 + fw, 26, 120, 52, 20, MUTED, "bold", dir=d, align="l" if d == "rtl" else "r")
    f.fill(40, 112, 1200, 6, "#2A2A30", r=3)
    if sl["total"]:
        pw = 1200 * sl["done_n"] / sl["total"]
        f.fill(R - pw, 112, pw, 6, "#3DBB6B", r=3, glow="#3DBB6B88", blur=8)
    cols = sl["cols"]
    if not any(cols.values()):
        for i in range(3):
            cw = (1200 - 44) / 3
            f.stroke(R - (i + 1) * cw - i * 22 + 10, 190, cw - 20, 330, "#3A3A40", 2, r=6, dash=[8, 8])
        empty(f, t["empty_wall"], t["empty_wall_sub"], CREAM, MUTED, y=300)
    else:
        cw = (1200 - 44) / 3
        fresh = (sl.get("fresh") or [""])[0]
        for i, part in enumerate(("done", "doing", "todo")):
            x = R - (i + 1) * cw - i * 22
            f.fill(x, 132, cw, 5, TAPE[part], r=2)
            f.text(t[part], x + 80, 140, cw - 80, 36, 26, CREAM, "heavy", dir=d)
            n = len(cols[part]) + sl["more"][part]
            digits(f, str(min(n, 99)), x + 30 + len(str(min(n, 99))) * 27, 142, 24, 34, size=22,
                   fg={"done": "#7BE0A0", "doing": FLAP_FG, "todo": "#C9CED8"}[part])
            if not cols[part]:
                f.stroke(x + 8, 190, cw - 16, 112, "#3A3A40", 2, r=4, dash=[7, 7])
                f.text(t["nothing_here"], x + 8, 190, cw - 16, 112, 22, MUTED, "bold", align="c", dir=d)
            for j, it in enumerate(cols[part]):
                rot = (-1.4, 1.1, -0.7, 1.5)[(i + j) % 4]
                k = None
                if part == "done":
                    k = stamp if (stamp is not None and it["text"] == fresh) else 1.0
                note(f, x + 8, 190 + j * 124, cw - 16, 108, it["text"], part, rot, it.get("tag", ""), k)
            if sl["more"][part]:
                f.fill(x + cw / 2 - 70, 566, 140, 32, "#2A2A30", r=16)
                f.text(t["more"], x + cw / 2 - 10, 566, 70, 32, 18, CREAM, "bold", dir=d, align="r")
                f.text(str(sl["more"][part]), x + cw / 2 - 64, 566, 50, 32, 18, FLAP_FG, "heavy", dir="ltr", font="mono")
    footer(f, sl, t, d, colour=MUTED, rule="#2A2A30")
    ticker_band(f, t, d, sl.get("_strip"))
    return f


def waiting(sl: dict, out: str) -> Frame:
    """The owner's desk: what waits for him, as papers with a stamp of what kind of answer is needed."""
    t, d = _lang(sl)
    f = Frame(out)
    f.fill(0, 0, W, H, ROOM)
    f.text(t["waiting"], 420, 18, R - 420, 60, 46, CREAM, "heavy", dir=d)
    f.text(t["waiting_sub"], 420, 78, R - 420, 26, 19, MUTED, "semibold", dir=d)
    rows = sl["rows"]
    w, h, gap = 280.0, 440.0, 22
    span = len(rows) * w + (len(rows) - 1) * gap
    right = 640 + span / 2
    for i, r in enumerate(rows):
        x = right - (i + 1) * w - i * gap
        rot = (-2.0, 1.6, -1.0, 2.0)[i % 4]
        f.push(tx=x + w / 2, ty=140 + h / 2, rot=rot)
        f.fill(-w / 2, -h / 2, w, h, "#F3EEE4", r=2, glow="#000000AA", blur=14, dy=7)
        f.fill(-w / 2, -h / 2, w, 9, AMBER)
        f.text(r["project"], -w / 2 + 18, -h / 2 + 24, w - 36, 30, 20, "#8A5A00", "heavy", dir=_dir(r["project"]))
        f.text(r["text"], -w / 2 + 18, -h / 2 + 64, w - 36, 230, 31, INK, "bold", wrap=True, v="t", lh=1.0,
               dir=_dir(r["text"]), align="r")
        f.stroke(-w / 2 + 18, h / 2 - 124, 140, 48, RED, 3, r=4)
        f.text(r["kind"], -w / 2 + 18, h / 2 - 124, 140, 48, 25, RED, "heavy", align="c", dir=d)
        f.text(t["since"], -w / 2 + 100, h / 2 - 60, w - 118, 36, 19, MUTED, "bold", dir=d)
        digits(f, r["age"], -w / 2 + 18 + len(r["age"]) * 25, h / 2 - 62, 22, 36, size=22)
        f.pop()
    if sl.get("more"):
        f.text(t["more"], 150, FOOT_Y + 6, 120, 32, 20, MUTED, "bold", dir=d, align="r")
        f.text(str(sl["more"]), 90, FOOT_Y + 6, 56, 32, 20, FLAP_FG, "heavy", dir="ltr", font="mono")
    footer(f, sl, t, d, colour=MUTED, rule="#2A2A30")
    ticker_band(f, t, d, sl.get("_strip"))
    return f


LAYOUTS = {"overview": overview, "wall": wall, "waiting": waiting, "staff": staff}


def layout(sl: dict, out: str, **kw) -> Frame:
    return LAYOUTS[sl["kind"]](sl, out, **kw)


# --- the ticker strip ------------------------------------------------------------------------------------------

STRIP_W = 4096           # two copies of HALF; the encoder crops a moving TICK_W window out of it
HALF = STRIP_W // 2


def _width(text: str, size: float) -> float:
    """A generous estimate of a text's drawn width (no overlap: a gap is better than a collision)."""
    return len(text) * size * (0.62 if _dir(text) == "rtl" else 0.58) + 8


TICKER_WINDOW = 2        # entries in one half of the strip (one window)
ENTRY_BUDGET = (HALF - 80) / TICKER_WINDOW   # pixels one entry may take, everything included
PROJECT_CHARS = 22       # a project name on the ticker, at most
GAP = 18                 # between the parts of an entry
ENTRY_GAP = 40           # between two entries


def _cut(text: str, most: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= most else text[:max(1, most - 1)].rstrip() + "…"


def _span(pieces: List[tuple]) -> float:
    return sum((14 if k == "dot" else _width(s, size)) + GAP for k, s, _, size in pieces) + ENTRY_GAP


def fit_entry(e: dict, t: dict) -> List[tuple]:
    """One update as separate parts (mark, word, time, project, text), fitted to ENTRY_BUDGET before anything is
    drawn: the project name is capped, then the text is shortened until the whole entry fits. Never wider."""
    mark_c = "#3DDC84" if e.get("mark") == "landed" else AMBER
    head = [("dot", "", mark_c, 0),
            ("text", t["landed_mark"] if e.get("mark") == "landed" else (e.get("kind") or t["waits"]), mark_c, 22)]
    if e.get("time"):
        head.append(("num", e["time"], mark_c, 22))
    head.append(("text", _cut(e.get("project") or "", PROJECT_CHARS), CREAM, 22))
    text = " ".join((e.get("text") or "").split())
    room = ENTRY_BUDGET - _span(head)
    most = len(text)
    while most > 1 and _width(_cut(text, most), 22) + GAP > room:
        most -= 2
    pieces = head + ([("text", _cut(text, most), "#D9D4C8", 22)] if text else [])
    return [p for p in pieces if p[0] == "dot" or p[1]]


def window(entries: List[dict], k: int) -> List[int]:
    """The k-th window: TICKER_WINDOW entries in a row, walking round the full list (fewer when the list is
    shorter)."""
    n = len(entries)
    if not n:
        return []
    return [(k * TICKER_WINDOW + j) % n for j in range(min(TICKER_WINDOW, n))]


def _lay(f: Frame, items: List[List[tuple]], base: float) -> None:
    """Fitted entries laid out right to left inside the half that ends at `base` (they always fit)."""
    x = base - 24
    for pieces in items:
        for kind, s, c, size in pieces:
            w = 14 if kind == "dot" else _width(s, size)
            if kind == "dot":
                f.circle(x - 7, TICK_H / 2 + 1, 6, c=c)
            else:
                f.text(s, x - w, 3, w, TICK_H - 3, size, c, "bold" if c == CREAM else "semibold",
                       dir="ltr" if kind == "num" else _dir(s), align="r", font="mono" if kind == "num" else "system")
            x -= w + GAP
        x -= ENTRY_GAP - GAP


def ticker_strip(entries: List[dict], out: str, lang: str = "he", k: int = 0) -> Frame:
    """The ticker strip for the encoder's k-th scroll cycle: window k in the right half, window k+1 in the left
    half (the window scrolls from the right half into the left one, so window k flows into window k+1, and the
    strip of cycle k+1 starts exactly where this one ends). No entries: the station's idle line."""
    t = TEXT[lang if lang in TEXT else "he"]
    f = Frame(out, STRIP_W, TICK_H)
    f.fill(0, 0, STRIP_W, TICK_H, "#0E0E10")
    f.fill(0, 0, STRIP_W, 3, RED)
    idle = [[("dot", "", AMBER, 0), ("text", t["ticker_idle"], CREAM, 22)]]
    for base, ids in ((STRIP_W, window(entries, k)), (HALF, window(entries, k + 1))):
        _lay(f, [fit_entry(entries[i], t) for i in ids] or idle, base)
    return f


# --- moving between slides --------------------------------------------------------------------------------------

def transition(old: str, new: str, out_dir: Path, n: int = 6, ext: str = "jpg") -> List[Frame]:
    """`n` frames from slide picture `old` to `new`: the new slide slides in from the left over the old one, which
    fades (the board reads from the right, so the next page enters from the left). Only the two pictures are used."""
    frames = []
    for i in range(n):
        u = (i + 1) / (n + 1)
        e = 1 - (1 - u) ** 3
        f = Frame(str(out_dir / f"{i:02d}.{ext}"))
        f.image(old, 0, 0, W, TICK_Y, sr=[0, 0, W, TICK_Y])
        f.fill(0, 0, W, TICK_Y, "#000000" + format(int(160 * e), "02X"))
        f.push(alpha=e)
        f.image(new, -90 * (1 - e), 0, W, TICK_Y, sr=[0, 0, W, TICK_Y])
        f.pop()
        f.image(new, 0, TICK_Y, W, TICK_H, sr=[0, TICK_Y, W, TICK_H])
        frames.append(f)
    return frames


def stamp_frames(sl: dict, out_dir: Path, n: int = 5, ext: str = "jpg") -> List[Frame]:
    """The fresh note's check stamping on: large and faint to its place, in `n` frames."""
    return [wall(sl, str(out_dir / f"stamp-{i:02d}.{ext}"), stamp=1.0 + 0.9 * (1 - (i + 1) / n)) for i in range(n)]
