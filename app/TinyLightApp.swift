// tinyLight - menu bar control for a Bluetooth light via the tinyLight bridge.
// Config: ~/Library/Application Support/tinyLight/config.json {"url": "...", "token": "..."}
import SwiftUI
import AppKit
import ServiceManagement

// MARK: - Light color

/// Kelvin -> display color (Tanner Helland fit), so UI tints match the real light.
func kelvinColor(_ k: Double) -> NSColor {
    let t = k / 100
    let r = t <= 66 ? 255 : 329.698727446 * pow(t - 60, -0.1332047592)
    let g = t <= 66 ? 99.4708025861 * log(t) - 161.1195681661 : 288.1221695283 * pow(t - 60, -0.0755148492)
    let b = t >= 66 ? 255 : (t <= 19 ? 0 : 138.5177312231 * log(t - 10) - 305.0447927307)
    func c(_ v: Double) -> CGFloat { CGFloat(min(255, max(0, v)) / 255) }
    return NSColor(srgbRed: c(r), green: c(g), blue: c(b), alpha: 1)
}

// MARK: - Model

@MainActor
final class Light: ObservableObject {
    @Published var on = true { didSet { if on != oldValue { push(power: true) } } }
    @Published var brightness: Double = 50 { didSet { if !syncing { dirty.insert("brightness"); push() } } }
    @Published var cct: Double = 4000 { didSet { if !syncing { dirty.insert("cct"); localEdit(); push() } } }
    @Published var gm: Double = 0 { didSet { if !syncing { dirty.insert("gm"); localEdit(); push() } } }
    @Published var offline = false
    /// "cct" or "hsi" (color). Another client (agent, CLI) may switch the light to color.
    @Published var mode = "cct"
    @Published var hue: Double = 0 { didSet { if !syncing { dirty.insert("hue"); localEdit("hsi"); push() } } }
    @Published var sat: Double = 100 { didSet { if !syncing { dirty.insert("sat"); localEdit("hsi"); push() } } }
    private var lastEdit = Date.distantPast

    private func localEdit(_ m: String = "cct") { lastEdit = Date(); mode = m }

    /// White/Color switch: resend the full set for that mode so the light changes mode.
    func setMode(_ m: String) {
        guard m != mode else { return }
        withAnimation(.spring(response: 0.4, dampingFraction: 0.8)) { localEdit(m) }
        dirty.formUnion(m == "hsi" ? ["hue", "sat"] : ["cct", "gm"])
        push()
    }

    private var syncing = false
    private var dirty = Set<String>()
    private var powerPending = false
    private var sending = false
    private let base: URL
    private let token: String?

    var nsColor: NSColor {
        mode == "hsi" ? NSColor(hue: hue / 360, saturation: sat / 100, brightness: 1, alpha: 1)
                      : kelvinColor(cct)
    }
    var color: Color { Color(nsColor: nsColor) }
    var level: Double { on ? brightness / 100 : 0 }

    init() {
        let dir = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/tinyLight")
        let cfg = (try? Data(contentsOf: dir.appendingPathComponent("config.json")))
            .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: String] } ?? [:]
        base = URL(string: cfg["url"] ?? "http://127.0.0.1:8787")!
        token = cfg["token"]
        Task { await refresh(); await runEvents() }
    }

    func url(_ path: String, _ q: [String: String] = [:]) -> URL {
        var c = URLComponents(url: base.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        var items = q.map { URLQueryItem(name: $0.key, value: $0.value) }
        if let token { items.append(URLQueryItem(name: "token", value: token)) }
        c.queryItems = items.isEmpty ? nil : items
        return c.url!
    }

    func get(_ u: URL, body: Data? = nil) async -> [String: Any]? {
        var r = URLRequest(url: u); r.timeoutInterval = 45
        if let body { r.httpMethod = "POST"; r.httpBody = body }
        guard let (d, resp) = try? await URLSession.shared.data(for: r),
              (resp as? HTTPURLResponse)?.statusCode == 200,
              let j = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else {
            offline = true; return nil
        }
        offline = false
        return j
    }

    func refresh() async {
        guard !sending, dirty.isEmpty, !powerPending, Date().timeIntervalSince(lastEdit) > 1.5,
              let s = await get(url("/state")) else { return }
        applyState(s)
    }

    // MARK: Push events (no polling): light state and alarm come from the bridge's /events stream.

    /// server clock - local clock, seconds (Cristian's method over /time, best of 5).
    @Published var serverOffset: Double = 0
    var onAlarm: (([String: Any]) -> Void)?
    var onCalibrate: (([String: Any]) -> Void)?
    var onPing: (([String: Any]) -> Void)?
    var serverNow: Double { Date().timeIntervalSince1970 + serverOffset }
    var baseURL: URL { base }
    var authToken: String? { token }

    func syncClock() async {
        var best: (rtt: Double, off: Double)?
        for _ in 0..<12 {  // keep-alive connection: the fastest sample has the smallest error
            let t0 = Date().timeIntervalSince1970
            guard let j = await get(url("/time")), let ts = (j["t"] as? NSNumber)?.doubleValue else { continue }
            let t1 = Date().timeIntervalSince1970
            if best == nil || t1 - t0 < best!.rtt { best = (t1 - t0, ts - (t0 + t1) / 2) }
        }
        if let best { serverOffset = best.off }
    }

    func runEvents() async {
        Task {  // clocks drift: re-sync every minute
            while !Task.isCancelled { try? await Task.sleep(nanoseconds: 60_000_000_000); await syncClock() }
        }
        while !Task.isCancelled {
            await syncClock()
            var req = URLRequest(url: url("/events", ["device": Host.current().localizedName ?? "mac"]))
            req.timeoutInterval = 3600
            if let (bytes, resp) = try? await URLSession.shared.bytes(for: req),
               (resp as? HTTPURLResponse)?.statusCode == 200 {
                offline = false
                do {
                    for try await line in bytes.lines where line.hasPrefix("data: ") {
                        guard let d = line.dropFirst(6).data(using: .utf8),
                              let ev = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { continue }
                        if ev["type"] as? String == "state", let st = ev["state"] as? [String: Any] { applyState(st) }
                        if ev["type"] as? String == "alarm" { onAlarm?(ev) }
                        if ev["type"] as? String == "calibrate" { onCalibrate?(ev) }
                        if ev["type"] as? String == "ping" { onPing?(ev) }
                    }
                } catch {}
            }
            offline = true
            try? await Task.sleep(nanoseconds: 2_000_000_000)  // reconnect
        }
    }

    func applyState(_ s: [String: Any]) {
        // Never overwrite a value the user is dragging or that is still on its way.
        guard !sending, dirty.isEmpty, !powerPending, Date().timeIntervalSince(lastEdit) > 1.5 else { return }
        syncing = true
        mode = s["mode"] as? String ?? "cct"
        if let v = s["hue"] as? Double { hue = v.truncatingRemainder(dividingBy: 360) }
        if let v = s["sat"] as? Double { sat = v }
        if let v = s["brightness"] as? Double, v > 0 { brightness = v }
        else if let v = s["last_on_brightness"] as? Double { brightness = v }
        if let v = s["cct"] as? Double { cct = v }
        if let v = s["gm"] as? Double { gm = v }
        syncing = false
        if let v = s["on"] as? Bool, v != on { syncing = true; on = v; syncing = false }
    }

    struct Scene: Identifiable { let name: String; let v: [String: Double]; var id: String { name } }
    @Published var scenes: [Scene] = []

    func loadScenes() async {
        guard let j = await get(url("/scenes")) else { return }
        scenes = j.compactMap { k, v in (v as? [String: Any]).map {
            Scene(name: k, v: $0.compactMapValues { ($0 as? NSNumber)?.doubleValue }) } }
            .sorted { $0.name.localizedStandardCompare($1.name) == .orderedAscending }
    }

    func applyScene(_ sc: Scene) {
        // Optimistic UI, then let the bridge apply the stored values.
        lastEdit = Date()
        syncing = true
        withAnimation(.spring(response: 0.45, dampingFraction: 0.8)) {
            on = true
            if let b = sc.v["brightness"] { brightness = b }
            if let h = sc.v["hue"] { mode = "hsi"; hue = h; sat = sc.v["sat"] ?? 100 }
            else { mode = "cct"; cct = sc.v["cct"] ?? cct; gm = sc.v["gm"] ?? 0 }
        }
        syncing = false
        Task { _ = await get(url("/scene", ["name": sc.name])) }
    }

    func saveScene(_ name: String) {
        Task { _ = await get(url("/scene/save", ["name": name])); await loadScenes() }
    }

    func deleteScene(_ name: String) {
        withAnimation { scenes.removeAll { $0.name == name } }
        Task { _ = await get(url("/scene/delete", ["name": name])) }
    }

    func colorPreset(_ h: Double) {
        withAnimation(.spring(response: 0.45, dampingFraction: 0.8)) { on = true; hue = h; sat = 100 }
    }

    private func push(power: Bool = false) {
        if syncing { return }
        if power { powerPending = true }
        guard !sending else { return }
        sending = true
        Task {
            // Coalesce: drain changes until nothing new arrived during the last send.
            while powerPending || !dirty.isEmpty {
                var q: [String: String] = [:]
                lastEdit = Date()
                if powerPending || (on && dirty.contains("brightness")) {
                    q["brightness"] = on ? String(Int(max(1, brightness.rounded()))) : "0"
                }
                // The bridge rejects mixed modes: send only the current mode's values.
                if mode == "hsi" {
                    if !dirty.isDisjoint(with: ["hue", "sat"]) {
                        q["hue"] = String(Int(hue.rounded()) % 360); q["sat"] = String(Int(sat.rounded()))
                    }
                } else {
                    if dirty.contains("cct") { q["cct"] = String(Int((cct / 50).rounded() * 50)) }
                    if dirty.contains("gm") { q["gm"] = String(Int(gm.rounded())) }
                }
                powerPending = false; dirty.removeAll()
                if !q.isEmpty { _ = await get(url("/set", q)) }
                try? await Task.sleep(nanoseconds: 60_000_000)
            }
            sending = false
        }
    }
}

// MARK: - Controls

struct GlowSlider: View {
    @Binding var value: Double
    let range: ClosedRange<Double>
    let track: [Color]
    let thumb: Color
    var enabled = true
    @State private var dragging = false

    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width, d: CGFloat = 22
            let f = (value - range.lowerBound) / (range.upperBound - range.lowerBound)
            ZStack(alignment: .leading) {
                Capsule().fill(LinearGradient(colors: track, startPoint: .leading, endPoint: .trailing))
                    .overlay(Capsule().strokeBorder(.white.opacity(0.08), lineWidth: 0.5))
                Circle()
                    .fill(thumb)
                    .overlay(Circle().strokeBorder(.white, lineWidth: 2))
                    .shadow(color: thumb.opacity(enabled ? 0.8 : 0), radius: dragging ? 8 : 4)
                    .frame(width: d, height: d)
                    .scaleEffect(dragging ? 1.18 : 1)
                    .offset(x: CGFloat(f) * (w - d))
            }
            .saturation(enabled ? 1 : 0)
            .opacity(enabled ? 1 : 0.45)
            .contentShape(Rectangle())
            .gesture(DragGesture(minimumDistance: 0)
                .onChanged { g in
                    if !dragging { withAnimation(.spring(response: 0.25, dampingFraction: 0.6)) { dragging = true } }
                    let p = min(1, max(0, (g.location.x - d / 2) / (w - d)))
                    value = range.lowerBound + Double(p) * (range.upperBound - range.lowerBound)
                }
                .onEnded { _ in withAnimation(.spring(response: 0.35, dampingFraction: 0.55)) { dragging = false } })
        }
        .frame(height: 22)
        .animation(.easeOut(duration: 0.25), value: enabled)
    }
}

struct PowerSwitch: View {
    @Binding var on: Bool
    let tint: Color
    var body: some View {
        Capsule()
            .fill(on ? tint : Color.primary.opacity(0.15))
            .frame(width: 40, height: 24)
            .overlay(alignment: on ? .trailing : .leading) {
                Circle().fill(.white).shadow(radius: 1).padding(2)
            }
            .onTapGesture { withAnimation(.spring(response: 0.35, dampingFraction: 0.65)) { on.toggle() } }
            .accessibilityLabel(on ? "Turn light off" : "Turn light on")
    }
}

struct ScenesRow: View {
    @EnvironmentObject var light: Light
    @State private var naming = false
    @State private var name = ""
    @FocusState private var focused: Bool

    func dot(_ sc: Light.Scene) -> Color {
        if let h = sc.v["hue"] { return Color(hue: h / 360, saturation: (sc.v["sat"] ?? 100) / 100, brightness: 1) }
        return Color(nsColor: kelvinColor(sc.v["cct"] ?? 4000))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text("Scenes").font(.system(size: 12)).foregroundStyle(.secondary)
            FlowChips {
                ForEach(light.scenes) { sc in
                    Button { light.applyScene(sc) } label: {
                        HStack(spacing: 5) {
                            Circle().fill(dot(sc)).frame(width: 7, height: 7)
                            Text(sc.name).font(.system(size: 11)).lineLimit(1)
                        }
                        .padding(.horizontal, 9).padding(.vertical, 6)
                        .background(.primary.opacity(0.07), in: Capsule())
                    }
                    .buttonStyle(.plain)
                    .contextMenu { Button("Delete \"\(sc.name)\"", role: .destructive) { light.deleteScene(sc.name) } }
                    .transition(.scale.combined(with: .opacity))
                }
                if naming {
                    TextField("Name", text: $name)
                        .textFieldStyle(.plain).font(.system(size: 11)).frame(width: 90)
                        .padding(.horizontal, 9).padding(.vertical, 5)
                        .background(.primary.opacity(0.1), in: Capsule())
                        .focused($focused)
                        .onSubmit {
                            let n = name.trimmingCharacters(in: .whitespaces)
                            if !n.isEmpty { light.saveScene(n) }
                            withAnimation(.spring(response: 0.3)) { naming = false; name = "" }
                        }
                        .onExitCommand { withAnimation { naming = false; name = "" } }
                } else {
                    Button { withAnimation(.spring(response: 0.3)) { naming = true }; focused = true } label: {
                        Image(systemName: "plus").font(.system(size: 10, weight: .semibold))
                            .frame(width: 26, height: 24)
                            .background(.primary.opacity(0.07), in: Capsule())
                    }
                    .buttonStyle(.plain)
                    .help("Save the current light as a scene")
                }
            }
        }
        .animation(.spring(response: 0.35, dampingFraction: 0.8), value: light.scenes.map(\.name))
        .task { await light.loadScenes() }
    }
}

/// Wrapping row of chips.
struct FlowChips: Layout {
    var spacing: CGFloat = 6
    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let w = proposal.width ?? 268
        var x: CGFloat = 0, y: CGFloat = 0, row: CGFloat = 0
        for v in subviews {
            let s = v.sizeThatFits(.unspecified)
            if x > 0, x + s.width > w { x = 0; y += row + spacing; row = 0 }
            x += s.width + spacing; row = max(row, s.height)
        }
        return CGSize(width: w, height: y + row)
    }
    func placeSubviews(in b: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = b.minX, y = b.minY, row: CGFloat = 0
        for v in subviews {
            let s = v.sizeThatFits(.unspecified)
            if x > b.minX, x + s.width > b.maxX { x = b.minX; y += row + spacing; row = 0 }
            v.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(s))
            x += s.width + spacing; row = max(row, s.height)
        }
    }
}

struct ModeSwitch: View {
    let mode: String
    let tint: Color
    let pick: (String) -> Void
    @Namespace private var ns

    var body: some View {
        HStack(spacing: 0) {
            ForEach([("cct", "White"), ("hsi", "Color")], id: \.0) { m in
                Text(m.1)
                    .font(.system(size: 12, weight: mode == m.0 ? .semibold : .regular))
                    .foregroundStyle(mode == m.0 ? .primary : .secondary)
                    .frame(maxWidth: .infinity).padding(.vertical, 5)
                    .background {
                        if mode == m.0 {
                            Capsule().fill(tint.opacity(0.28))
                                .overlay(Capsule().strokeBorder(tint.opacity(0.6), lineWidth: 0.5))
                                .matchedGeometryEffect(id: "pill", in: ns)
                        }
                    }
                    .contentShape(Capsule())
                    .onTapGesture { pick(m.0) }
            }
        }
        .padding(3)
        .background(.primary.opacity(0.07), in: Capsule())
        .animation(.spring(response: 0.38, dampingFraction: 0.78), value: mode)
    }
}

struct Row<C: View>: View {
    let title: String, value: String
    @ViewBuilder let content: C
    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text(title).foregroundStyle(.secondary)
                Spacer()
                Text(value).monospacedDigit().contentTransition(.numericText())
            }
            .font(.system(size: 12))
            content
        }
    }
}

// MARK: - Panel

struct Panel: View {
    @EnvironmentObject var light: Light
    @EnvironmentObject var schedule: ScheduleModel
    @EnvironmentObject var alarm: AlarmCenter
    @State private var showPhone = false

    var body: some View {
        let c = light.color
        ZStack(alignment: .topLeading) {
            if alarm.editing {
                AlarmView()
                    .transition(.asymmetric(insertion: .opacity.combined(with: .scale(scale: 0.97, anchor: .topTrailing)),
                                            removal: .opacity))
            } else if schedule.editing {
                ScheduleEditor()
                    .transition(.asymmetric(insertion: .opacity.combined(with: .scale(scale: 0.97, anchor: .topTrailing)),
                                            removal: .opacity))
            } else {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Circle().fill(light.on ? c : .gray.opacity(0.4))
                    .frame(width: 10, height: 10)
                    .shadow(color: c.opacity(light.level), radius: 3 + 8 * light.level)
                Text("Room light").font(.system(size: 13, weight: .semibold))
                if light.offline {
                    Text("offline").font(.system(size: 11)).foregroundStyle(.orange)
                        .transition(.opacity)
                }
                Spacer()
                PowerSwitch(on: $light.on, tint: c)
            }
            Row(title: "Brightness", value: "\(Int(light.brightness.rounded()))%") {
                GlowSlider(value: $light.brightness, range: 1...100,
                           track: [Color(white: 0.18), c], thumb: c, enabled: light.on)
            }
            ModeSwitch(mode: light.mode, tint: c) { light.setMode($0) }
            Group {
                if light.mode == "hsi" {
                    VStack(alignment: .leading, spacing: 16) {
                        Row(title: "Hue", value: "\(Int(light.hue.rounded()) % 360)°") {
                            GlowSlider(value: $light.hue, range: 0...359,
                                       track: stride(from: 0.0, through: 1, by: 1 / 6).map {
                                           Color(hue: $0, saturation: 1, brightness: 1) },
                                       thumb: Color(hue: light.hue / 360, saturation: 1, brightness: 1),
                                       enabled: light.on)
                        }
                        Row(title: "Saturation", value: "\(Int(light.sat.rounded()))%") {
                            GlowSlider(value: $light.sat, range: 0...100,
                                       track: [.white, Color(hue: light.hue / 360, saturation: 1, brightness: 1)],
                                       thumb: c, enabled: light.on)
                        }
                        HStack(spacing: 8) {
                            ForEach([0.0, 30, 55, 120, 190, 240, 285, 320], id: \.self) { h in
                                Button { light.colorPreset(h) } label: {
                                    Circle().fill(Color(hue: h / 360, saturation: 1, brightness: 1))
                                        .frame(width: 22, height: 22)
                                        .overlay(Circle().strokeBorder(.white.opacity(
                                            abs(light.hue - h) < 1 ? 0.95 : 0), lineWidth: 2))
                                        .frame(maxWidth: .infinity)
                                }
                                .buttonStyle(.plain)
                                .accessibilityLabel("Hue \(Int(h))")
                            }
                        }
                    }
                    .transition(.asymmetric(insertion: .move(edge: .trailing).combined(with: .opacity),
                                            removal: .move(edge: .trailing).combined(with: .opacity)))
                } else {
                    VStack(alignment: .leading, spacing: 16) {
                        Row(title: "Temperature", value: "\(Int((light.cct / 50).rounded() * 50))K") {
                            GlowSlider(value: $light.cct, range: 2700...6500,
                                       track: stride(from: 2700.0, through: 6500, by: 760).map {
                                           Color(nsColor: kelvinColor($0)) },
                                       thumb: c, enabled: light.on)
                        }
                        Row(title: "Tint", value: light.gm.rounded() == 0 ? "0"
                                : String(format: "%+d", Int(light.gm.rounded()))) {
                            GlowSlider(value: $light.gm, range: -50...50,
                                       track: [Color(red: 0.44, green: 0.82, blue: 0.55), Color(white: 0.25),
                                               Color(red: 0.88, green: 0.5, blue: 0.84)],
                                       thumb: light.gm < -2 ? Color(red: 0.44, green: 0.82, blue: 0.55)
                                           : light.gm > 2 ? Color(red: 0.88, green: 0.5, blue: 0.84) : .white,
                                       enabled: light.on)
                        }
                    }
                    .transition(.asymmetric(insertion: .move(edge: .leading).combined(with: .opacity),
                                            removal: .move(edge: .leading).combined(with: .opacity)))
                }
            }
            .clipped()
            ScenesRow()
            Divider().opacity(0.5)
            ScheduleRow()
            AlarmRow()
            if showPhone {
                PhoneQRView().transition(.opacity.combined(with: .move(edge: .bottom)))
            }
            HStack {
                Button { withAnimation(.spring(response: 0.4, dampingFraction: 0.85)) { showPhone.toggle() } } label: {
                    Label(showPhone ? "Hide phone" : "Phone", systemImage: "qrcode")
                }
                .buttonStyle(.plain).font(.system(size: 11)).foregroundStyle(.secondary)
                .help("QR codes for the phone: bedside night mode and remote")
                Spacer()
                Button("Quit") { NSApp.terminate(nil) }
                    .buttonStyle(.plain).font(.system(size: 11)).foregroundStyle(.tertiary)
            }
        }
        .transition(.opacity)
            }
        }
        .padding(16)
        .frame(width: alarm.editing ? 680 : (schedule.editing ? 600 : 300))
        .animation(.spring(response: 0.45, dampingFraction: 0.85), value: schedule.editing)
        .animation(.spring(response: 0.45, dampingFraction: 0.85), value: alarm.editing)
        .background(alignment: .top) {
            // Soft light spill from the top, strength = brightness.
            RadialGradient(colors: [c.opacity(0.45 * light.level), .clear],
                           center: .top, startRadius: 0, endRadius: 260)
                .allowsHitTesting(false)
        }
        .animation(.easeInOut(duration: 0.35), value: light.on)
        .animation(.easeOut(duration: 0.2), value: light.cct)
        .animation(.easeOut(duration: 0.2), value: light.offline)
        .animation(.easeInOut(duration: 0.35), value: light.mode)
        .task {
            // Live state while the panel is open: agents and the CLI change the light too.
            while !Task.isCancelled {
                await light.refresh()
                try? await Task.sleep(nanoseconds: 30_000_000_000)
            }
        }
    }
}

// MARK: - Menu bar icon

func menuIcon(on: Bool, color: NSColor) -> NSImage {
    let cfg = NSImage.SymbolConfiguration(pointSize: 14, weight: .regular)
        .applying(.init(paletteColors: [on ? color : .tertiaryLabelColor]))
    let img = NSImage(systemSymbolName: on ? "lightbulb.fill" : "lightbulb",
                      accessibilityDescription: "Room light")!.withSymbolConfiguration(cfg)!
    img.isTemplate = !on
    return img
}

@main
struct TinyLightApp: App {
    @StateObject private var light = Light()
    @StateObject private var schedule = ScheduleModel()
    @StateObject private var alarm = AlarmCenter()
    @StateObject private var calibrator = Calibrator()

    init() {
        if SMAppService.mainApp.status != .enabled { try? SMAppService.mainApp.register() }
    }

    var body: some Scene {
        MenuBarExtra {
            Panel().environmentObject(light).environmentObject(schedule).environmentObject(alarm)
                .environmentObject(calibrator)
        } label: {
            Image(nsImage: menuIcon(on: light.on, color: light.nsColor))
                .task {  // alarm and calibration must work without opening the menu
                    alarm.attach(light)
                    calibrator.attach(light, alarm.player)
                }
        }
        .menuBarExtraStyle(.window)

    }
}
