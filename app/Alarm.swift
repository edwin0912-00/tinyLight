// Sunrise alarm: center (bridge events), synced sound player, ring panel, Alarm tab with curve editor.
import SwiftUI
import AppKit
import AVFoundation

// MARK: - Curve points (light + sound), same AE-style easing as the schedule

struct APoint: Identifiable, Equatable {
    var id = UUID()
    var t: Double                 // minutes from the alarm time (negative = before)
    var value: Double             // light level or sound volume, 0-100
    var kelvin: Double? = nil     // light: white
    var hue: Double? = nil        // light: color (amber dawn start)
    var sat: Double? = nil
    var sound: String? = nil      // sound: switch file from here
    var easeIn: Double = 50
    var easeOut: Double = 50
    var hold = false

    init(t: Double, value: Double) { self.t = t; self.value = value }

    init(json j: [String: Any], key: String) {
        t = (j["t"] as? NSNumber)?.doubleValue ?? 0
        value = (j[key] as? NSNumber)?.doubleValue ?? 0
        kelvin = (j["kelvin"] as? NSNumber)?.doubleValue
        hue = (j["hue"] as? NSNumber)?.doubleValue
        sat = (j["sat"] as? NSNumber)?.doubleValue
        sound = j["sound"] as? String
        easeIn = (j["ease_in"] as? NSNumber)?.doubleValue ?? 50
        easeOut = (j["ease_out"] as? NSNumber)?.doubleValue ?? 50
        hold = j["hold"] as? Bool ?? false
    }

    func json(key: String) -> [String: Any] {
        var d: [String: Any] = ["t": (t * 2).rounded() / 2, key: Int(value.rounded()),
                                "ease_in": Int(easeIn.rounded()), "ease_out": Int(easeOut.rounded())]
        if let hue { d["hue"] = Int(hue.rounded()); d["sat"] = Int((sat ?? 100).rounded()) }
        else if key == "level" { d["kelvin"] = Int(((kelvin ?? 4000) / 50).rounded() * 50) }
        if let sound { d["sound"] = sound }
        if hold { d["hold"] = true }
        return d
    }

    var color: Color {
        if let hue { return Color(hue: hue / 360, saturation: (sat ?? 100) / 100, brightness: 1) }
        return Color(nsColor: kelvinColor(kelvin ?? 4000))
    }
}

/// Value at x on an eased point list (bridge: lib/alarm.py _segment). nil before the first point.
func curveValue(_ pts: [APoint], _ x: Double) -> (a: APoint, b: APoint, q: Double)? {
    let s = pts.sorted { $0.t < $1.t }
    guard let first = s.first, x >= first.t else { return nil }
    for (a, b) in zip(s, s.dropFirst()) where a.t <= x && x < b.t {
        let p = (x - a.t) / (b.t - a.t)
        return (a, b, a.hold ? 0 : bezier(p, a.easeOut / 100, 1 - b.easeIn / 100))
    }
    return (s.last!, s.last!, 0)
}

// MARK: - Alarm center

struct AlarmItem: Identifiable, Equatable {
    var id: String
    var time: Int            // minute of day
    var days: Set<Int>       // 0 = Monday
    var enabled: Bool
    var mode: String
    var sound: String
}

struct AlarmMode: Identifiable, Equatable {
    var id: String
    var name: String
    var autoOff: Int
    var light: [APoint]
    var sound: [APoint]
}

@MainActor
final class AlarmCenter: ObservableObject {
    @Published var alarms: [AlarmItem] = []
    @Published var modes: [AlarmMode] = []
    @Published var sounds: [String] = []
    @Published var next: [String: Date] = [:]
    @Published var state = "idle"          // idle | dawn | ringing | snoozed
    @Published var activeName = ""
    @Published var snoozeUntil: Date?
    @Published var snoozeMinutes = 10
    @Published var editing = false
    @Published var error: String?
    let player = AlarmPlayer()
    private weak var light: Light?
    private var saveTask: Task<Void, Never>?
    private var ringPanel: NSPanel?

    func attach(_ l: Light) {
        guard light == nil else { return }
        light = l
        player.light = l
        l.onAlarm = { [weak self] ev in self?.handle(ev) }
        l.onPing = { [weak self] ev in
            let target = ev["device"] as? String ?? "all"
            guard let self, target == "all" || target == self.player.deviceName else { return }
            self.player.chime(volume: (ev["volume"] as? NSNumber)?.doubleValue ?? 0.8)
        }
        Task { await load() }
    }

    // Bridge event -> state, sound, ring panel. Every device gets the same event at the same moment.
    func handle(_ ev: [String: Any]) {
        let st = ev["state"] as? String ?? "idle"
        withAnimation(.spring(response: 0.4, dampingFraction: 0.8)) {
            state = st
            activeName = ev["name"] as? String ?? ""
            snoozeUntil = (ev["snooze_until"] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) }
        }
        player.update(ev)
        if st == "ringing" || st == "snoozed" { showRingPanel() } else { hideRingPanel() }
        if st == "idle" { Task { await load() } }   // next occurrence moves on after an alarm ends
    }

    func load() async {
        guard let l = light, let j = await l.get(l.url("/alarms")) else { return }
        sounds = j["sounds"] as? [String] ?? []
        snoozeMinutes = (j["snooze_minutes"] as? NSNumber)?.intValue ?? 10
        if saveTask == nil {
            alarms = (j["alarms"] as? [[String: Any]] ?? []).map { a in
                let hm = (a["time"] as? String ?? "07:00").split(separator: ":").compactMap { Int($0) }
                return AlarmItem(id: a["id"] as? String ?? UUID().uuidString, time: hm[0] * 60 + (hm.count > 1 ? hm[1] : 0),
                                 days: Set(a["days"] as? [Int] ?? Array(0..<7)), enabled: a["enabled"] as? Bool ?? true,
                                 mode: a["mode"] as? String ?? "gentle", sound: a["sound"] as? String ?? (sounds.first ?? ""))
            }
            let order = ["gentle", "long", "quick", "light", "backup"]
            modes = (j["modes"] as? [String: [String: Any]] ?? [:]).map { k, m in
                AlarmMode(id: k, name: m["name"] as? String ?? k, autoOff: (m["auto_off"] as? NSNumber)?.intValue ?? 90,
                          light: (m["light"] as? [[String: Any]] ?? []).map { APoint(json: $0, key: "level") },
                          sound: (m["sound"] as? [[String: Any]] ?? []).map { APoint(json: $0, key: "volume") })
            }.sorted { (order.firstIndex(of: $0.id) ?? 99, $0.id) < (order.firstIndex(of: $1.id) ?? 99, $1.id) }
        }
        let f = ISO8601DateFormatter()
        next = Dictionary(uniqueKeysWithValues: (j["next"] as? [[String: Any]] ?? []).compactMap { n in
            guard let id = n["id"] as? String, let s = n["next"] as? String, let d = f.date(from: s) else { return nil }
            return (id, d)
        })
        if let a = j["active"] as? [String: Any] { handle(a) }
    }

    func changed() {
        saveTask?.cancel()
        saveTask = Task {
            try? await Task.sleep(nanoseconds: 450_000_000)
            guard !Task.isCancelled, let l = light else { return }
            let body: [String: Any] = [
                "alarms": alarms.map { a in
                    ["id": a.id, "time": String(format: "%02d:%02d", a.time / 60, a.time % 60),
                     "days": a.days.sorted(), "enabled": a.enabled, "mode": a.mode, "sound": a.sound] as [String: Any]
                },
                "modes": Dictionary(uniqueKeysWithValues: modes.map { m in
                    (m.id, ["name": m.name, "auto_off": m.autoOff,
                            "light": m.light.sorted { $0.t < $1.t }.map { $0.json(key: "level") },
                            "sound": m.sound.sorted { $0.t < $1.t }.map { $0.json(key: "volume") }] as [String: Any])
                })]
            let data = try? JSONSerialization.data(withJSONObject: body)
            let j = await l.get(l.url("/alarms"), body: data)
            saveTask = nil
            if let j, let e = j["error"] as? String { error = e } else { error = nil; await load() }
        }
    }

    func stop() { Task { if let l = light { _ = await l.get(l.url("/alarm/stop")) } } }
    func snooze() { Task { if let l = light { _ = await l.get(l.url("/alarm/snooze")) } } }
    func test(_ mode: String, sound: String) {
        Task { if let l = light { _ = await l.get(l.url("/alarm/test", ["mode": mode, "sound": sound, "seconds": "45"])) } }
    }

    func add() {
        let a = AlarmItem(id: "a" + String(UUID().uuidString.prefix(6)), time: 7 * 60 + 30, days: Set(0..<5),
                          enabled: true, mode: "gentle", sound: sounds.first ?? "birds-spain")
        withAnimation(.spring(response: 0.35)) { alarms.append(a) }
        changed()
    }

    // MARK: ring panel (floating, every space, Return = stop, Space = snooze)

    func showRingPanel() {
        if ringPanel == nil {
            let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 340, height: 190),
                            styleMask: [.titled, .fullSizeContentView, .nonactivatingPanel],
                            backing: .buffered, defer: false)
            p.titlebarAppearsTransparent = true
            p.titleVisibility = .hidden
            p.isMovableByWindowBackground = true
            p.level = .screenSaver
            p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            p.contentView = NSHostingView(rootView: RingView().environmentObject(self))
            p.center()
            ringPanel = p
        }
        // Show on top but never take the keyboard: typing must not snooze or stop by accident.
        ringPanel?.orderFrontRegardless()
    }

    func hideRingPanel() { ringPanel?.orderOut(nil) }
}

// MARK: - Ring panel

struct RingView: View {
    @EnvironmentObject var center: AlarmCenter
    var body: some View {
        VStack(spacing: 14) {
            Image(systemName: "sun.horizon.fill").font(.system(size: 30)).foregroundStyle(.orange)
                .symbolEffect(.pulse, isActive: center.state == "ringing")
            VStack(spacing: 2) {
                Text(Date.now, style: .time).font(.system(size: 28, weight: .semibold)).monospacedDigit()
                Text(center.state == "snoozed" ? "Snoozed" : center.activeName)
                    .font(.system(size: 12)).foregroundStyle(.secondary)
            }
            HStack(spacing: 10) {
                Button("Snooze \(center.snoozeMinutes) min") { center.snooze() }
                    .keyboardShortcut(.space, modifiers: [])
                    .controlSize(.large)
                Button("Stop") { center.stop() }
                    .keyboardShortcut(.defaultAction)
                    .controlSize(.large).buttonStyle(.borderedProminent).tint(.orange)
            }
            Text("Stops on every device").font(.system(size: 10)).foregroundStyle(.tertiary)
        }
        .padding(22)
        .frame(width: 340)
    }
}

// MARK: - Panel row + Alarm tab

func daysLabel(_ d: Set<Int>) -> String {
    if d.count == 7 { return "Every day" }
    if d == Set(0..<5) { return "Weekdays" }
    if d == Set([5, 6]) { return "Weekend" }
    let n = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
    return d.sorted().map { n[$0] }.joined(separator: " ")
}

struct AlarmRow: View {
    @EnvironmentObject var light: Light
    @EnvironmentObject var center: AlarmCenter
    var body: some View {
        let on = center.alarms.filter(\.enabled)
        let soon = on.compactMap { a in center.next[a.id].map { (a, $0) } }.min { $0.1 < $1.1 }
        HStack(spacing: 8) {
            Image(systemName: "alarm").font(.system(size: 15)).frame(width: 64, height: 22)
                .foregroundStyle(center.state == "idle" ? Color.secondary : Color.orange)
                .background(.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 5))
            VStack(alignment: .leading, spacing: 1) {
                Text("Alarm").font(.system(size: 12))
                Group {
                    if center.state != "idle" { Text(center.state == "snoozed" ? "Snoozed" : center.activeName) }
                    else if let s = soon {
                        Text(verbatim: "\(s.1.formatted(.dateTime.weekday(.abbreviated).hour().minute())) · \(center.modes.first { $0.id == s.0.mode }?.name ?? "")")
                    } else { Text("None set") }
                }
                .font(.system(size: 11)).foregroundStyle(.secondary)
            }
            Spacer()
            if center.state != "idle" {
                Button("Stop") { center.stop() }.buttonStyle(.plain).font(.system(size: 11))
                    .padding(.horizontal, 8).padding(.vertical, 4)
                    .background(Color.orange.opacity(0.25), in: Capsule())
            }
            Button { withAnimation(.spring(response: 0.45, dampingFraction: 0.85)) { center.editing = true } } label: {
                Image(systemName: "slider.horizontal.3")
            }
            .buttonStyle(.plain).help("Alarms and dawn curves")
        }
        .onAppear { center.attach(light) }
        .task {  // the first load can fail at login (network not up yet); edits also come from the phone
            while !Task.isCancelled { await center.load(); try? await Task.sleep(nanoseconds: 60_000_000_000) }
        }
    }
}

struct AlarmView: View {
    @EnvironmentObject var center: AlarmCenter
    @State private var modeId = "gentle"
    @State private var previewing: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                Button { withAnimation(.spring(response: 0.45, dampingFraction: 0.85)) { center.editing = false } } label: {
                    Label("Light", systemImage: "chevron.left").font(.system(size: 12))
                }
                .buttonStyle(.plain).foregroundStyle(.secondary)
                Text("Alarms").font(.system(size: 13, weight: .semibold))
                Text("morning only · 04:00-11:59").font(.system(size: 11)).foregroundStyle(.secondary)
                Spacer()
                Button { center.add() } label: { Label("Add", systemImage: "plus") }.font(.system(size: 11))
            }
            if let e = center.error {
                Text(e).font(.system(size: 11)).foregroundStyle(.red)
            }
            ForEach($center.alarms) { $a in alarmCard($a) }
            if center.alarms.isEmpty {
                Text("No alarms yet. Add one, then shape its dawn below.")
                    .font(.system(size: 12)).foregroundStyle(.secondary)
            }
            Divider().opacity(0.5)
            HStack {
                Picker("", selection: $modeId) {
                    ForEach(center.modes) { Text($0.name).tag($0.id) }
                }
                .labelsHidden().frame(width: 170)
                Text("dawn curves").font(.system(size: 11)).foregroundStyle(.secondary)
                Spacer()
                Button("Test 45 s") { center.test(modeId, sound: center.alarms.first?.sound ?? center.sounds.first ?? "") }
                    .font(.system(size: 11)).help("Runs the whole alarm in 45 s on every device")
            }
            if let i = center.modes.firstIndex(where: { $0.id == modeId }) {
                AlarmCurveEditor(mode: $center.modes[i]).environmentObject(center)
            }
            CalibrationView()
        }
        .task { await center.load() }
    }

    func alarmCard(_ a: Binding<AlarmItem>) -> some View {
        HStack(spacing: 10) {
            Toggle("", isOn: Binding(get: { a.wrappedValue.enabled }, set: { a.wrappedValue.enabled = $0; center.changed() }))
                .toggleStyle(.switch).controlSize(.mini).labelsHidden()
            DatePicker("", selection: Binding(
                get: { Calendar.current.date(bySettingHour: a.wrappedValue.time / 60, minute: a.wrappedValue.time % 60, second: 0, of: .now)! },
                set: { d in
                    let c = Calendar.current.dateComponents([.hour, .minute], from: d)
                    a.wrappedValue.time = min(11 * 60 + 59, max(4 * 60, (c.hour ?? 7) * 60 + (c.minute ?? 0)))
                    center.changed()
                }), displayedComponents: .hourAndMinute)
                .labelsHidden().datePickerStyle(.field).frame(width: 70)
            HStack(spacing: 2) {
                ForEach(0..<7, id: \.self) { d in
                    let on = a.wrappedValue.days.contains(d)
                    Text(["M", "T", "W", "T", "F", "S", "S"][d]).font(.system(size: 10, weight: on ? .bold : .regular))
                        .frame(width: 18, height: 18)
                        .background(on ? Color.orange.opacity(0.3) : Color.primary.opacity(0.06), in: Circle())
                        .onTapGesture {
                            if on { a.wrappedValue.days.remove(d) } else { a.wrappedValue.days.insert(d) }
                            center.changed()
                        }
                }
            }
            Picker("", selection: Binding(get: { a.wrappedValue.mode }, set: { a.wrappedValue.mode = $0; modeId = $0; center.changed() })) {
                ForEach(center.modes) { Text($0.name).tag($0.id) }
            }
            .labelsHidden().frame(width: 130)
            Picker("", selection: Binding(get: { a.wrappedValue.sound }, set: { a.wrappedValue.sound = $0; center.changed() })) {
                ForEach(center.sounds, id: \.self) { Text($0).tag($0) }
            }
            .labelsHidden().frame(width: 140)
            Button {
                previewing = center.player.togglePreview(a.wrappedValue.sound) ? a.wrappedValue.sound : nil
            } label: { Image(systemName: previewing == a.wrappedValue.sound ? "stop.fill" : "play.fill") }
                .buttonStyle(.plain).help("Listen")
            Spacer(minLength: 0)
            Button(role: .destructive) {
                withAnimation { center.alarms.removeAll { $0.id == a.wrappedValue.id } }
                center.changed()
            } label: { Image(systemName: "trash") }.buttonStyle(.plain).foregroundStyle(.secondary)
        }
        .font(.system(size: 11))
        .padding(8)
        .background(.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 8))
    }
}

// MARK: - Curve editor for one mode: light (colored) + sound (dashed), minutes around the alarm

struct AlarmCurveEditor: View {
    @Binding var mode: AlarmMode
    @EnvironmentObject var center: AlarmCenter
    @State private var layer = "light"
    @State private var selected: UUID?
    @State private var dragging: UUID?
    @State private var grab: CGSize = .zero

    var xMin: Double { min(-35, ((mode.light + mode.sound).map(\.t).min() ?? -30) - 5) }
    var xMax: Double { max(15, min(Double(mode.autoOff), ((mode.light + mode.sound).filter { !$0.hold }.map(\.t).max() ?? 10) + 5)) }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Picker("", selection: $layer) {
                Text("Light").tag("light"); Text("Sound").tag("sound")
            }
            .pickerStyle(.segmented).labelsHidden().frame(width: 180)
            GeometryReader { geo in
                let r = CGRect(x: 30, y: 12, width: geo.size.width - 40, height: geo.size.height - 32)
                ZStack(alignment: .topLeading) {
                    Canvas { ctx, _ in draw(ctx, r) }
                    ForEach(points) { p in handle(p, r) }
                }
                .contentShape(Rectangle())
                .onTapGesture(count: 2) { loc in add(loc, r) }
            }
            .frame(height: 170)
            inspector
        }
    }

    var points: [APoint] { layer == "light" ? mode.light : mode.sound }
    func setPoints(_ f: (inout [APoint]) -> Void) {
        if layer == "light" { f(&mode.light) } else { f(&mode.sound) }
        center.changed()
    }

    func px(_ t: Double, _ r: CGRect) -> CGFloat { r.minX + (t - xMin) / (xMax - xMin) * r.width }
    func py(_ v: Double, _ r: CGRect) -> CGFloat { r.maxY - v / 100 * r.height }

    func draw(_ ctx: GraphicsContext, _ r: CGRect) {
        var t = (xMin / 5).rounded(.up) * 5
        while t <= xMax {
            let x = px(t, r)
            ctx.stroke(Path { $0.move(to: CGPoint(x: x, y: r.minY)); $0.addLine(to: CGPoint(x: x, y: r.maxY)) },
                       with: .color(.primary.opacity(t == 0 ? 0.5 : 0.07)), lineWidth: t == 0 ? 1.5 : 1)
            ctx.draw(Text(t == 0 ? "alarm" : String(format: "%+.0f", t)).font(.system(size: 9)).foregroundStyle(.secondary),
                     at: CGPoint(x: x, y: r.maxY + 10))
            t += 5
        }
        for v in [0.0, 50, 100] {
            ctx.draw(Text("\(Int(v))").font(.system(size: 9)).foregroundStyle(.secondary), at: CGPoint(x: r.minX - 14, y: py(v, r)))
        }
        // light: colored by its own color; sound: dashed purple
        var x = xMin
        while x < xMax {
            if let a = curveValue(mode.light, x), let b = curveValue(mode.light, x + 0.25) {
                let va = a.a.value + (a.b.value - a.a.value) * a.q, vb = b.a.value + (b.b.value - b.a.value) * b.q
                let col: Color = a.a.hue != nil ? a.a.color : Color(nsColor: kelvinColor((a.a.kelvin ?? 4000) + ((a.b.kelvin ?? a.a.kelvin ?? 4000) - (a.a.kelvin ?? 4000)) * a.q))
                ctx.stroke(Path { $0.move(to: CGPoint(x: px(x, r), y: py(va, r))); $0.addLine(to: CGPoint(x: px(x + 0.25, r), y: py(vb, r))) },
                           with: .color(col.opacity(layer == "light" ? 1 : 0.35)), style: StrokeStyle(lineWidth: 4, lineCap: .round))
            }
            x += 0.25
        }
        var s = Path(); var started = false
        x = xMin
        while x <= xMax {
            if let a = curveValue(mode.sound, x) {
                let v = a.a.value + (a.b.value - a.a.value) * a.q
                let p = CGPoint(x: px(x, r), y: py(v, r))
                if started { s.addLine(to: p) } else { s.move(to: p); started = true }
            }
            x += 0.25
        }
        ctx.stroke(s, with: .color(Color.purple.opacity(layer == "sound" ? 0.95 : 0.35)), style: StrokeStyle(lineWidth: 2.5, dash: [6, 4]))
    }

    func handle(_ p: APoint, _ r: CGRect) -> some View {
        let sel = selected == p.id
        return Circle()
            .fill(layer == "light" ? p.color : Color.purple)
            .overlay(Circle().strokeBorder(sel ? Color.primary : .white, lineWidth: sel ? 2.5 : 2))
            .frame(width: 14, height: 14)
            .scaleEffect(dragging == p.id ? 1.35 : 1)
            .animation(.spring(response: 0.25, dampingFraction: 0.6), value: dragging)
            .position(x: px(p.t, r), y: py(p.value, r))
            .gesture(DragGesture(minimumDistance: 0)
                .onChanged { g in
                    selected = p.id
                    guard dragging == p.id || hypot(g.translation.width, g.translation.height) > 3 else { return }
                    if dragging != p.id {
                        dragging = p.id
                        grab = CGSize(width: g.startLocation.x - px(p.t, r), height: g.startLocation.y - py(p.value, r))
                    }
                    let loc = CGPoint(x: g.location.x - grab.width, y: g.location.y - grab.height)
                    let t = ((xMin + (loc.x - r.minX) / r.width * (xMax - xMin)) * 2).rounded() / 2
                    let v = min(100, max(0, (Double((r.maxY - loc.y) / r.height) * 100).rounded()))
                    if layer == "light" {
                        if let i = mode.light.firstIndex(where: { $0.id == p.id }) { mode.light[i].t = t; mode.light[i].value = v }
                    } else if let i = mode.sound.firstIndex(where: { $0.id == p.id }) { mode.sound[i].t = t; mode.sound[i].value = v }
                }
                .onEnded { _ in
                    if dragging == p.id { center.changed() }
                    dragging = nil
                })
    }

    func add(_ loc: CGPoint, _ r: CGRect) {
        let t = ((xMin + (loc.x - r.minX) / r.width * (xMax - xMin)) * 2).rounded() / 2
        let v = min(100, max(0, (Double((r.maxY - loc.y) / r.height) * 100).rounded()))
        var p = APoint(t: t, value: v)
        if layer == "light" { p.kelvin = 3000 }
        setPoints { $0.append(p) }
        selected = p.id
    }

    @ViewBuilder var inspector: some View {
        let list = points
        if let i = list.firstIndex(where: { $0.id == selected }) {
            let p = list[i]
            HStack(spacing: 12) {
                Text(p.t == 0 ? "at alarm" : String(format: "%+.1f min", p.t)).font(.system(size: 13, weight: .semibold)).monospacedDigit()
                    .frame(width: 80, alignment: .leading)
                Text(verbatim: "\(layer == "light" ? "Level" : "Volume") \(Int(p.value))%").font(.system(size: 11)).foregroundStyle(.secondary)
                if layer == "light" {
                    Toggle("Color", isOn: Binding(get: { p.hue != nil }, set: { on in
                        setPoints { pts in
                            if on { pts[i].hue = 25; pts[i].sat = 100 } else { pts[i].hue = nil; pts[i].sat = nil; pts[i].kelvin = pts[i].kelvin ?? 2700 }
                        }
                    })).font(.system(size: 11))
                    if p.hue != nil {
                        Slider(value: Binding(get: { p.hue ?? 25 }, set: { v in setPoints { $0[i].hue = v.rounded() } }), in: 0...60)
                            .frame(width: 90).help("Hue: red to amber")
                    } else {
                        Slider(value: Binding(get: { p.kelvin ?? 3000 }, set: { v in setPoints { $0[i].kelvin = v } }), in: 2700...6500)
                            .tint(p.color).frame(width: 90)
                        Text(verbatim: "\(Int(((p.kelvin ?? 3000) / 50).rounded() * 50))K").font(.system(size: 11)).foregroundStyle(.secondary).monospacedDigit()
                    }
                }
                Menu("Ease \(Int(p.easeIn))/\(Int(p.easeOut))") {
                    ForEach([("Linear", 0.0), ("Easy", 33.0), ("Soft", 66.0), ("Max", 90.0)], id: \.0) { e in
                        Button(e.0) { setPoints { $0[i].easeIn = e.1; $0[i].easeOut = e.1; $0[i].hold = false } }
                    }
                    Button(p.hold ? "Release hold" : "Hold until next") { setPoints { $0[i].hold.toggle() } }
                }
                .font(.system(size: 11)).frame(width: 96)
                Spacer()
                Button(role: .destructive) {
                    guard list.count > 2 else { return }
                    setPoints { $0.removeAll { $0.id == p.id } }
                    selected = nil
                } label: { Image(systemName: "trash") }.buttonStyle(.plain).disabled(list.count <= 2)
                Button { selected = nil } label: { Image(systemName: "xmark") }.buttonStyle(.plain)
            }
            .padding(10)
            .background(.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 10))
        } else {
            Text("Light = solid line, sound = dashed. Drag points, double-click to add, select a point to set color, warmth and easing.")
                .font(.system(size: 11)).foregroundStyle(.secondary)
        }
    }
}
