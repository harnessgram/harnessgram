// usage: swift scripts/generate_panel_icons.swift  - renders the Cockpit panel PNG icons from docs/brand/icon (macOS AppKit SVG rendering)
import AppKit
let root = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
let master = root.appendingPathComponent("docs/brand/icon"), out = root.appendingPathComponent("src/ctb/assets/panel")
func svg(_ n: String) -> NSImage { NSImage(contentsOf: master.appendingPathComponent(n))! }
// Opaque RGB PNG: the background fills the square, the glyph is drawn at `scale` of the canvas, centred.
func render(_ name: String, size: Int, scale: CGFloat = 1) {
    let ctx = CGContext(data: nil, width: size, height: size, bitsPerComponent: 8, bytesPerRow: 0,
                        space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
    NSGraphicsContext.current = NSGraphicsContext(cgContext: ctx, flipped: false)
    let s = CGFloat(size), full = NSRect(x: 0, y: 0, width: s, height: s)
    NSColor.black.setFill(); full.fill()
    svg("background.svg").draw(in: full)
    let g = s * scale
    svg("glyph.svg").draw(in: NSRect(x: (s - g) / 2, y: (s - g) / 2, width: g, height: g))
    NSGraphicsContext.current = nil
    try! NSBitmapImageRep(cgImage: ctx.makeImage()!).representation(using: .png, properties: [:])!
        .write(to: out.appendingPathComponent(name))
}
render("favicon-32.png", size: 32)
render("apple-touch-icon.png", size: 180)
render("icon-192.png", size: 192)
render("icon-512.png", size: 512)
render("icon-maskable-512.png", size: 512, scale: 0.8)  // the glyph stays inside the 80% maskable safe zone
