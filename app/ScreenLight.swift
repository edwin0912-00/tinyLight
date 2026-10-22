// Screen sunrise: the built-in display becomes a second light. While an alarm runs it follows the lamp:
// display brightness rises with the lamp level (100 % while ringing) and a full-screen window shows the lamp's warm colour.
// Brightness uses the private DisplayServices API (no permission needed; Apple may change it).
// Limits: a closed lid has no screen, and nothing can draw over the lock screen.
import AppKit
import CoreGraphics
import IOKit.pwr_mgt

private typealias GetBri = @convention(c) (CGDirectDisplayID, UnsafeMutablePointer<Float>) -> Int32
private typealias SetBri = @convention(c) (CGDirectDisplayID, Float) -> Int32

final class ScreenLight {
    static let key = "screenSunrise"
    var enabled: Bool { UserDefaults.standard.bool(forKey: Self.key) }

    private let getBri: GetBri?, setBri: SetBri?
    private var window: NSWindow?
    private var saved: Float?
    private var assertion: IOPMAssertionID = 0
    private var alarmActive = false
    private var ringing = false
    private var lastLamp: [String: Any]?
    private var preview: Timer?

    init() {
        let h = dlopen("/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices", RTLD_NOW)
        getBri = dlsym(h, "DisplayServicesGetBrightness").map { unsafeBitCast($0, to: GetBri.self) }
        setBri = dlsym(h, "DisplayServicesSetBrightness").map { unsafeBitCast($0, to: SetBri.self) }
    }

    var available: Bool { getBri != nil && setBri != nil && builtin != nil }

    private var builtin: (id: CGDirectDisplayID, screen: NSScreen)? {
        for s in NSScreen.screens {
            if let n = s.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber,
               CGDisplayIsBuiltin(n.uint32Value) != 0 { return (n.uint32Value, s) }
        }
        return nil
    }

    // Alarm event: dawn / ringing / snoozed keep the screen light on; idle restores everything.
    func alarm(_ state: String) {
        alarmActive = ["dawn", "ringing", "snoozed"].contains(state)
        let wasRinging = ringing
        ringing = state == "ringing"
        if !alarmActive && preview == nil { end() }
        if ringing != wasRinging, let s = lastLamp { lamp(s) }  // jump to full at once, not at the next lamp event
    }

    // Lamp state event (brightness 0-100, cct or hue). Only acts while an alarm runs.
    func lamp(_ s: [String: Any]) {
        lastLamp = s
        guard alarmActive, enabled, preview == nil else { return }
        let on = s["on"] as? Bool ?? true
        let level = on ? ((s["brightness"] as? NSNumber)?.doubleValue ?? 0) / 100 : 0
        let color = (s["mode"] as? String) == "hsi"
            ? NSColor(hue: CGFloat(((s["hue"] as? NSNumber)?.doubleValue ?? 30) / 360), saturation: 0.85, brightness: 1, alpha: 1)
            : kelvinColor((s["cct"] as? NSNumber)?.doubleValue ?? 2700)
        show(level: ringing ? 1 : level, color: color)  // ringing: full screen brightness (owner)
    }

    /// Local preview: 15 s from dark amber to full warm light, then back. No lamp, no sound.
    func runPreview() {
        guard available else { return }
        preview?.invalidate()
        let t0 = Date()
        preview = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] t in
            guard let self else { return t.invalidate() }
            let x = Date().timeIntervalSince(t0) / 15
            if x >= 1 { t.invalidate(); self.preview = nil; if !self.alarmActive { self.end() }; return }
            self.show(level: x * x, color: self.kelvinColor(2200 + 2000 * x))
        }
    }

    private func show(level: Double, color: NSColor) {
        guard let (id, screen) = builtin, let getBri, let setBri else { return }
        if saved == nil {
            var b: Float = 0
            if getBri(id, &b) == 0 { saved = b }
            var wake: IOPMAssertionID = 0   // wake the display if it slept, then keep it on
            IOPMAssertionDeclareUserActivity("Screen sunrise" as CFString, kIOPMUserActiveLocal, &wake)
            IOPMAssertionCreateWithName(kIOPMAssertionTypePreventUserIdleDisplaySleep as CFString,
                                        IOPMAssertionLevel(kIOPMAssertionLevelOn), "Screen sunrise" as CFString, &assertion)
        }
        if window == nil {
            let w = NSWindow(contentRect: screen.frame, styleMask: .borderless, backing: .buffered, defer: false)
            w.level = NSWindow.Level(rawValue: NSWindow.Level.screenSaver.rawValue - 1)  // the ring panel stays on top
            w.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .stationary, .ignoresCycle]
            w.ignoresMouseEvents = true
            w.isOpaque = true
            w.hasShadow = false
            w.setFrame(screen.frame, display: true)
            w.orderFrontRegardless()
            window = w
        }
        // Same mapping as the phone page: colour fades in from black, screen brightness follows the lamp.
        let k = min(1, level * 1.4)
        NSAnimationContext.runAnimationGroup { c in
            c.duration = 1
            window?.animator().backgroundColor = color.blended(withFraction: 1 - k, of: .black) ?? color
        }
        _ = setBri(id, Float(max(0.03, min(1, level * 1.2))))
    }

    private func end() {
        window?.orderOut(nil)
        window = nil
        if let b = saved, let (id, _) = builtin, let setBri { _ = setBri(id, b) }
        saved = nil
        if assertion != 0 { IOPMAssertionRelease(assertion); assertion = 0 }
    }

    private func kelvinColor(_ k: Double) -> NSColor {
        let t = k / 100
        let r = t <= 66 ? 255 : 329.7 * pow(t - 60, -0.1332)
        let g = t <= 66 ? 99.47 * log(t) - 161.12 : 288.12 * pow(t - 60, -0.0755)
        let b = t >= 66 ? 255 : (t <= 19 ? 0 : 138.52 * log(t - 10) - 305.04)
        let c = { (v: Double) in CGFloat(min(255, max(0, v)) / 255) }
        return NSColor(srgbRed: c(r), green: c(g), blue: c(b), alpha: 1)
    }
}
