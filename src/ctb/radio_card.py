"""The radio's picture: a simple summary card rendered to PNG.

macOS only: a small Swift program (CoreText through AppKit, so Hebrew, numbers and emoji are laid out right to
left correctly) is compiled once into the state folder and reused. Without a Swift compiler the card falls
back to a plain dark frame made by ffmpeg, so the stream still has a picture.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

from .config import private_dir

WIDTH, HEIGHT = 1280, 720

SWIFT = r"""
import AppKit
import Foundation

struct Section: Decodable { let head: String; let lines: [String]; let tone: String? }
struct Card: Decodable { let w: Int; let h: Int; let title: String; let time: String; let rtl: Bool
                         let sections: [Section]; let foot: String }

let data = FileHandle.standardInput.readDataToEndOfFile()
guard CommandLine.arguments.count == 2, let card = try? JSONDecoder().decode(Card.self, from: data) else { exit(2) }
let W = CGFloat(card.w), H = CGFloat(card.h)
guard let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: card.w, pixelsHigh: card.h, bitsPerSample: 8,
                                 samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                                 bytesPerRow: 0, bitsPerPixel: 0),
      let ctx = NSGraphicsContext(bitmapImageRep: rep) else { exit(3) }
NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = ctx
let bg = NSGradient(starting: NSColor(calibratedRed: 0.06, green: 0.09, blue: 0.16, alpha: 1),
                    ending: NSColor(calibratedRed: 0.10, green: 0.17, blue: 0.30, alpha: 1))!
bg.draw(in: NSRect(x: 0, y: 0, width: W, height: H), angle: -90)

func para(_ size: CGFloat) -> NSMutableParagraphStyle {
    let p = NSMutableParagraphStyle()
    p.alignment = card.rtl ? .right : .left
    p.baseWritingDirection = card.rtl ? .rightToLeft : .leftToRight
    p.lineBreakMode = .byTruncatingTail
    return p
}
func draw(_ text: String, _ size: CGFloat, _ weight: NSFont.Weight, _ color: NSColor, _ y: CGFloat) -> CGFloat {
    let attrs: [NSAttributedString.Key: Any] = [.font: NSFont.systemFont(ofSize: size, weight: weight),
                                                .foregroundColor: color, .paragraphStyle: para(size)]
    let s = NSAttributedString(string: text, attributes: attrs)
    let h = ceil(size * 1.35)
    s.draw(with: NSRect(x: 56, y: H - y - h, width: W - 112, height: h), options: [.usesLineFragmentOrigin, .truncatesLastVisibleLine])
    return y + h
}
let white = NSColor(calibratedWhite: 0.97, alpha: 1)
let hint = NSColor(calibratedRed: 0.62, green: 0.70, blue: 0.82, alpha: 1)
let tones: [String: NSColor] = ["warn": NSColor(calibratedRed: 1.0, green: 0.78, blue: 0.35, alpha: 1),
                                 "ok": NSColor(calibratedRed: 0.55, green: 0.88, blue: 0.62, alpha: 1)]
var y: CGFloat = 40
y = draw(card.title, 46, .heavy, white, y)
y = draw(card.time, 24, .medium, hint, y) + 18
for sec in card.sections {
    if y > H - 110 { break }
    y = draw(sec.head, 32, .bold, tones[sec.tone ?? ""] ?? white, y) + 2
    for line in sec.lines {
        if y > H - 100 { break }
        y = draw(line, 26, .regular, white, y)
    }
    y += 16
}
_ = draw(card.foot, 22, .medium, hint, H - 64)
NSGraphicsContext.restoreGraphicsState()
guard let png = rep.representation(using: .png, properties: [:]) else { exit(4) }
let out = URL(fileURLWithPath: CommandLine.arguments[1])
let tmp = out.deletingLastPathComponent().appendingPathComponent(".card-\(getpid()).png")
do { try png.write(to: tmp) } catch { exit(5) }
if rename(tmp.path, out.path) != 0 { try? FileManager.default.removeItem(at: tmp); exit(5) }
"""


def _binary(folder: Path) -> Optional[Path]:
    """The compiled renderer, built once per source version; None when no Swift compiler is available."""
    digest = hashlib.sha256(SWIFT.encode()).hexdigest()[:12]
    exe = folder / f"card-{digest}"
    if exe.exists():
        return exe
    swiftc = shutil.which("swiftc")
    if not swiftc:
        return None
    private_dir(folder)
    src = folder / f"card-{digest}.swift"
    src.write_text(SWIFT, encoding="utf-8")
    try:
        r = subprocess.run([swiftc, "-O", "-o", str(exe) + ".tmp", str(src)], capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode or not Path(str(exe) + ".tmp").exists():
        return None
    os.replace(str(exe) + ".tmp", exe)
    for old in folder.glob("card-*"):
        if old.name not in (exe.name, src.name):
            try:
                old.unlink()
            except OSError:
                pass
    return exe


def render(folder: Path, out: Path, title: str, time_text: str, sections: List[dict], foot: str,
           rtl: bool = True) -> bool:
    """Write the card to `out` (atomically). True when the text card was drawn."""
    exe = _binary(folder) if os.uname().sysname == "Darwin" else None
    if exe is not None:
        card = {"w": WIDTH, "h": HEIGHT, "title": title, "time": time_text, "rtl": rtl, "foot": foot,
                "sections": [{"head": s["head"], "lines": list(s.get("lines") or []), "tone": s.get("tone")}
                             for s in sections]}
        try:
            r = subprocess.run([str(exe), str(out)], input=json.dumps(card, ensure_ascii=False).encode("utf-8"),
                               capture_output=True, timeout=30)
            if r.returncode == 0 and out.exists():
                return True
        except (OSError, subprocess.TimeoutExpired):
            pass
    if not out.exists():
        plain(out)
    return False


def plain(out: Path) -> None:
    """A plain dark frame, so ffmpeg always has a picture to read."""
    tmp = out.with_name(".plain-" + out.name)
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"color=c=0x101828:s={WIDTH}x{HEIGHT}", "-frames:v", "1", str(tmp)], capture_output=True, timeout=60)
    if tmp.exists():
        os.replace(tmp, out)
