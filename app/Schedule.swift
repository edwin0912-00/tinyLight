// Daily light schedule: model, compact row for the panel, and the curve editor window.
import SwiftUI
import AppKit

// MARK: - Model

struct SPoint: Identifiable, Equatable {
    var id = UUID()
    var time: Int?            // minute of day, nil when the point follows the sun
    var sun: String?          // "sunset" | "sunrise"
    var offset: Int = 0       // minutes from the sun event
    var level: Double
    var kelvin: Double
    var easeIn: Double = 50   // AE-style influence arriving at the point, 0-100 %
    var easeOut: Double = 50  // influence leaving the point
    var hold = false          // keep this value until the next point

    func minute(_ sunset: Double, _ sunrise: Double) -> Double {
        if let time { return Double(time) }
        let base = sun == "sunrise" ? sunrise : sunset
        return (base + Double(offset)).truncatingRemainder(dividingBy: 1440)
    }

    var json: [String: Any] {
        var d: [String: Any] = ["level": Int(level.rounded()), "kelvin": Int((kelvin / 50).rounded() * 50),
                                "ease_in": Int(easeIn.rounded()), "ease_out": Int(easeOut.rounded())]
        if hold { d["hold"] = true }
        if let time { d["time"] = String(format: "%02d:%02d", time / 60, time % 60) }
        else { d["sun"] = sun ?? "sunset"; d["offset"] = offset }
        return d
    }

    init(json j: [String: Any]) {
        level = (j["level"] as? NSNumber)?.doubleValue ?? 0
        kelvin = (j["kelvin"] as? NSNumber)?.doubleValue ?? 4000
        easeIn = (j["ease_in"] as? NSNumber)?.doubleValue ?? 50
        easeOut = (j["ease_out"] as? NSNumber)?.doubleValue ?? 50
        hold = j["hold"] as? Bool ?? false
        if let t = j["time"] as? String {
            let p = t.split(separator: ":").compactMap { Int($0) }
            time = (p.first ?? 0) * 60 + (p.count > 1 ? p[1] : 0)
        } else {
            sun = j["sun"] as? String ?? "sunset"
            offset = (j["offset"] as? NSNumber)?.intValue ?? 0
        }
    }

    init(time: Int, level: Double, kelvin: Double) { self.time = time; self.level = level; self.kelvin = kelvin }
}

/// y for x=p on the cubic bezier (0,0)-(x1,0)-(x2,1)-(1,1). Same math as lib/schedule.py.
func bezier(_ p: Double, _ x1: Double, _ x2: Double) -> Double {
    var lo = 0.0, hi = 1.0
    for _ in 0..<30 {
        let t = (lo + hi) / 2
        let x = 3 * (1 - t) * (1 - t) * t * x1 + 3 * (1 - t) * t * t * x2 + t * t * t
        if x < p { lo = t } else { hi = t }
    }
    let t = (lo + hi) / 2
    return 3 * (1 - t) * t * t + t * t * t
}

func hhmm(_ m: Double) -> String {
    let v = (Int(m.rounded()) % 1440 + 1440) % 1440
    return String(format: "%02d:%02d", v / 60, v % 60)
}

@MainActor
final class ScheduleModel: ObservableObject {
    @Published var points: [SPoint] = []
    @Published var enabled = false
    @Published var sunset: Double = 1140
    @Published var sunrise: Double = 480
    @Published var pausedUntil: Date?
    @Published var loaded = false
    @Published var editing = false   // panel shows the curve editor instead of the light controls
    private weak var light: Light?
    private var saveTask: Task<Void, Never>?

    func attach(_ l: Light) { light = l }

    func load() async {
        guard let l = light, let j = await l.get(l.url("/schedule")) else { return }
        apply(j)
    }

    private func apply(_ j: [String: Any], keepPoints: Bool = false) {
        enabled = j["enabled"] as? Bool ?? false
        if !keepPoints, !saveIsPending, let ps = j["points"] as? [[String: Any]] {
            var fresh = ps.map(SPoint.init(json:))
            // Keep ids stable (same order as we sent) so the selected point stays selected.
            if fresh.count == points.count { for i in fresh.indices { fresh[i].id = points[i].id } }
            if fresh != points { points = fresh }
        }
        if let t = j["today"] as? [String: Any] {
            sunset = minutes(t["sunset"] as? String) ?? sunset
            sunrise = minutes(t["sunrise"] as? String) ?? sunrise
        }
        pausedUntil = (j["paused_until"] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) }
            .flatMap { $0 > Date() ? $0 : nil }
        loaded = true
    }

    private var saveIsPending: Bool { saveTask != nil }

    private func minutes(_ s: String?) -> Double? {
        guard let p = s?.split(separator: ":").compactMap({ Double($0) }), p.count == 2 else { return nil }
        return p[0] * 60 + p[1]
    }

    func setEnabled(_ on: Bool) {
        enabled = on
        Task { if let l = light, let j = await l.get(l.url("/schedule/enable", ["on": on ? "1" : "0"])) { apply(j) } }
    }

    func resume() {
        pausedUntil = nil
        Task { if let l = light { _ = await l.get(l.url("/schedule/resume")) } }
    }

    /// Debounced save: the editor writes after the drag settles.
    func changed() {
        saveTask?.cancel()
        saveTask = Task {
            try? await Task.sleep(nanoseconds: 450_000_000)
            guard !Task.isCancelled, let l = light,
                  let body = try? JSONSerialization.data(withJSONObject: ["points": points.map(\.json)]) else { return }
            let j = await l.get(l.url("/schedule"), body: body)
            saveTask = nil
            if let j { apply(j, keepPoints: true) }  // local points are the truth while editing
        }
    }

    func sorted() -> [(SPoint, Double)] {
        points.map { ($0, $0.minute(sunset, sunrise)) }.sorted { $0.1 < $1.1 }
    }

    /// Same math as the bridge (lib/schedule.py value_at).
    func value(at m: Double) -> (level: Double, kelvin: Double) {
        let s = sorted()
        guard s.count >= 2 else { return (0, 4000) }
        let prev = s.last { $0.1 <= m } ?? s.last!
        let i = s.firstIndex { $0.0.id == prev.0.id }!
        let next = s[(i + 1) % s.count]
        let ta = prev.1 <= m ? prev.1 : prev.1 - 1440
        let tb = next.1 > ta ? next.1 : next.1 + 1440
        let p = tb > ta ? min(1, max(0, (m - ta) / (tb - ta))) : 0
        let q = prev.0.hold ? 0 : bezier(p, prev.0.easeOut / 100, 1 - next.0.easeIn / 100)
        return (prev.0.level + (next.0.level - prev.0.level) * q,
                prev.0.kelvin + (next.0.kelvin - prev.0.kelvin) * q)
    }

    var nowMinute: Double {
        let c = Calendar.current.dateComponents([.hour, .minute], from: Date())
        return Double((c.hour ?? 0) * 60 + (c.minute ?? 0))
    }
}

// MARK: - Panel row

struct ScheduleRow: View {
    @EnvironmentObject var light: Light
    @EnvironmentObject var model: ScheduleModel

    var body: some View {
        let now = model.value(at: model.nowMinute)
        HStack(spacing: 8) {
            MiniCurve().frame(width: 64, height: 22)
                .onTapGesture { open() }
            VStack(alignment: .leading, spacing: 1) {
                Text("Schedule").font(.system(size: 12))
                Group {
                    if !model.enabled { Text("Off") }
                    else if let p = model.pausedUntil { Text("Paused until \(hhmm(minuteOf(p)))") }
                    else { Text(verbatim: "\(Int(now.level.rounded()))% · \(Int((now.kelvin / 100).rounded()) * 100)K now") }
                }
                .font(.system(size: 11)).foregroundStyle(.secondary).monospacedDigit()
            }
            Spacer()
            if model.enabled, model.pausedUntil != nil {
                Button("Resume") { model.resume() }.buttonStyle(.plain).font(.system(size: 11))
                    .padding(.horizontal, 8).padding(.vertical, 4)
                    .background(.primary.opacity(0.08), in: Capsule())
            }
            Button { open() } label: { Image(systemName: "slider.horizontal.3") }
                .buttonStyle(.plain).help("Edit the daily curve")
            Toggle("", isOn: Binding(get: { model.enabled }, set: { model.setEnabled($0) }))
                .toggleStyle(.switch).controlSize(.mini).labelsHidden()
        }
        .task {
            model.attach(light)
            while !Task.isCancelled { await model.load(); try? await Task.sleep(nanoseconds: 5_000_000_000) }
        }
    }

    func open() {
        withAnimation(.spring(response: 0.45, dampingFraction: 0.85)) { model.editing = true }
    }

    func minuteOf(_ d: Date) -> Double {
        let c = Calendar.current.dateComponents([.hour, .minute], from: d)
        return Double((c.hour ?? 0) * 60 + (c.minute ?? 0))
    }
}

/// Tiny preview of the day curve, colored by warmth.
struct MiniCurve: View {
    @EnvironmentObject var model: ScheduleModel
    var body: some View {
        Canvas { ctx, size in
            drawCurve(ctx, size, model, width: 2, step: 15)
            let x = model.nowMinute / 1440 * size.width
            ctx.fill(Path(ellipseIn: CGRect(x: x - 2.5, y: size.height - model.value(at: model.nowMinute).level / 100 * size.height - 2.5, width: 5, height: 5)), with: .color(.primary))
        }
        .opacity(model.enabled ? 1 : 0.4)
        .background(.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 5))
    }
}

@MainActor func drawCurve(_ ctx: GraphicsContext, _ size: CGSize, _ model: ScheduleModel, width: CGFloat, step: Double) {
    guard model.points.count >= 2 else { return }
    var m = 0.0
    while m < 1440 {
        let a = model.value(at: m), b = model.value(at: min(1440, m + step))
        var p = Path()
        p.move(to: CGPoint(x: m / 1440 * size.width, y: size.height - a.level / 100 * size.height))
        p.addLine(to: CGPoint(x: min(1440, m + step) / 1440 * size.width, y: size.height - b.level / 100 * size.height))
        ctx.stroke(p, with: .color(Color(nsColor: kelvinColor(a.kelvin))),
                   style: StrokeStyle(lineWidth: width, lineCap: .round))
        m += step
    }
}

struct PulseRing: View {
    let color: Color
    @State private var on = false
    var body: some View {
        Circle().stroke(color, lineWidth: 2)
            .frame(width: 10, height: 10)
            .scaleEffect(on ? 2.6 : 1).opacity(on ? 0 : 0.8)
            .onAppear { withAnimation(.easeOut(duration: 1.8).repeatForever(autoreverses: false)) { on = true } }
    }
}

// MARK: - Editor window

struct ScheduleEditor: View {
    @EnvironmentObject var model: ScheduleModel
    @EnvironmentObject var light: Light
    @State private var selected: UUID?
    @State private var dragging: UUID?
    @State private var grab: CGSize = .zero   // cursor offset from the point centre at drag start
    private let pad = EdgeInsets(top: 18, leading: 36, bottom: 26, trailing: 14)

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 10) {
                Button {
                    withAnimation(.spring(response: 0.45, dampingFraction: 0.85)) { model.editing = false }
                } label: {
                    Label("Light", systemImage: "chevron.left").font(.system(size: 12))
                }
                .buttonStyle(.plain).foregroundStyle(.secondary)
                Text("Daily light").font(.system(size: 13, weight: .semibold))
                Text("sunset \(hhmm(model.sunset)) · sunrise \(hhmm(model.sunrise))")
                    .font(.system(size: 11)).foregroundStyle(.secondary)
                Spacer()
                if model.enabled, model.pausedUntil != nil {
                    Button("Resume") { model.resume() }.font(.system(size: 11))
                }
                Toggle("", isOn: Binding(get: { model.enabled }, set: { model.setEnabled($0) }))
                    .toggleStyle(.switch).controlSize(.small).labelsHidden()
            }
            GeometryReader { geo in
                let plot = CGRect(x: pad.leading, y: pad.top,
                                  width: geo.size.width - pad.leading - pad.trailing,
                                  height: geo.size.height - pad.top - pad.bottom)
                ZStack(alignment: .topLeading) {
                    Canvas { ctx, _ in grid(ctx, plot) ; curve(ctx, plot) }
                    if let p = model.points.first(where: { $0.id == selected }) { easeHandles(p, plot) }
                    TimelineView(.periodic(from: .now, by: 30)) { _ in nowMarker(plot) }
                    ForEach(model.points) { p in handle(p, plot) }
                }
                .contentShape(Rectangle())
                .onTapGesture(count: 2) { loc in add(at: loc, plot) }
            }
            .frame(height: 220)
            inspector
        }
        .task { model.attach(light); await model.load() }
    }

    // Plot helpers
    func px(_ m: Double, _ r: CGRect) -> CGFloat { r.minX + m / 1440 * r.width }
    func py(_ v: Double, _ r: CGRect) -> CGFloat { r.maxY - v / 100 * r.height }

    func grid(_ ctx: GraphicsContext, _ r: CGRect) {
        for h in stride(from: 0, through: 24, by: 3) {
            let x = px(Double(h * 60), r)
            ctx.stroke(Path { $0.move(to: CGPoint(x: x, y: r.minY)); $0.addLine(to: CGPoint(x: x, y: r.maxY)) },
                       with: .color(.primary.opacity(0.08)), lineWidth: 1)
            ctx.draw(Text(String(format: "%02d:00", h % 24)).font(.system(size: 10)).foregroundStyle(.secondary),
                     at: CGPoint(x: x, y: r.maxY + 12))
        }
        for v in [0.0, 25, 50, 75, 100] {
            let y = py(v, r)
            ctx.stroke(Path { $0.move(to: CGPoint(x: r.minX, y: y)); $0.addLine(to: CGPoint(x: r.maxX, y: y)) },
                       with: .color(.primary.opacity(v == 0 ? 0.15 : 0.06)), lineWidth: 1)
            ctx.draw(Text("\(Int(v))").font(.system(size: 10)).foregroundStyle(.secondary),
                     at: CGPoint(x: r.minX - 16, y: y))
        }
        for (m, label) in [(model.sunrise, "sunrise"), (model.sunset, "sunset")] {
            let x = px(m, r)
            ctx.stroke(Path { $0.move(to: CGPoint(x: x, y: r.minY)); $0.addLine(to: CGPoint(x: x, y: r.maxY)) },
                       with: .color(.orange.opacity(0.6)), style: StrokeStyle(lineWidth: 1, dash: [4, 4]))
            ctx.draw(Text(label).font(.system(size: 10)).foregroundStyle(.orange), at: CGPoint(x: x, y: r.minY - 9))
        }
        let nx = px(model.nowMinute, r)
        ctx.stroke(Path { $0.move(to: CGPoint(x: nx, y: r.minY)); $0.addLine(to: CGPoint(x: nx, y: r.maxY)) },
                   with: .color(.primary.opacity(0.35)), lineWidth: 1)
    }

    func curve(_ ctx: GraphicsContext, _ r: CGRect) {
        var c = ctx
        c.translateBy(x: r.minX, y: r.minY)
        // soft fill under the curve
        var area = Path()
        area.move(to: CGPoint(x: 0, y: r.height))
        for m in stride(from: 0.0, through: 1440, by: 6) {
            area.addLine(to: CGPoint(x: m / 1440 * r.width, y: r.height - model.value(at: m).level / 100 * r.height))
        }
        area.addLine(to: CGPoint(x: r.width, y: r.height)); area.closeSubpath()
        c.fill(area, with: .color(.primary.opacity(0.05)))
        drawCurve(c, r.size, model, width: 4, step: 4)
    }

    func handle(_ p: SPoint, _ r: CGRect) -> some View {
        let m = p.minute(model.sunset, model.sunrise)
        let isSel = selected == p.id
        return Circle()
            .fill(Color(nsColor: kelvinColor(p.kelvin)))
            .overlay(Circle().strokeBorder(isSel ? Color.primary : .white, lineWidth: isSel ? 2.5 : 2))
            .overlay(alignment: .top) {
                if p.sun != nil { Image(systemName: "sun.horizon.fill").font(.system(size: 8)).foregroundStyle(.orange).offset(y: -12) }
            }
            .shadow(color: .black.opacity(0.25), radius: 2)
            .frame(width: 16, height: 16)
            .scaleEffect(dragging == p.id ? 1.35 : (isSel ? 1.15 : 1))
            .animation(.spring(response: 0.25, dampingFraction: 0.6), value: dragging)
            .position(x: px(m, r), y: py(p.level, r))
            .gesture(DragGesture(minimumDistance: 0)
                .onChanged { g in
                    selected = p.id
                    // A click selects only; move after a real drag, without jumping to the cursor.
                    guard dragging == p.id || hypot(g.translation.width, g.translation.height) > 3 else { return }
                    if dragging != p.id {
                        dragging = p.id
                        grab = CGSize(width: g.startLocation.x - px(m, r), height: g.startLocation.y - py(p.level, r))
                    }
                    guard let i = model.points.firstIndex(where: { $0.id == p.id }) else { return }
                    let loc = CGPoint(x: g.location.x - grab.width, y: g.location.y - grab.height)
                    let nm = min(1439, max(0, (loc.x - r.minX) / r.width * 1440))
                    let snapped = (nm / 5).rounded() * 5
                    if model.points[i].time != nil { model.points[i].time = Int(snapped) }
                    else {
                        let base = model.points[i].sun == "sunrise" ? model.sunrise : model.sunset
                        model.points[i].offset = Int(snapped - base)
                    }
                    model.points[i].level = min(100, max(0, (Double((r.maxY - loc.y) / r.height) * 100).rounded()))
                }
                .onEnded { _ in
                    if dragging == p.id { model.changed() }
                    dragging = nil
                })
    }

    /// "Now": current time and the light's real level (manual value while the schedule is paused).
    func nowMarker(_ r: CGRect) -> some View {
        let m = model.nowMinute
        let curve = model.value(at: m)
        let manual = !model.enabled || model.pausedUntil != nil
        let level = manual ? (light.on ? light.brightness : 0) : curve.level
        let kelvin = manual && light.mode != "hsi" ? light.cct : curve.kelvin
        let tint = manual ? light.color : Color(nsColor: kelvinColor(kelvin))
        return ZStack {
            PulseRing(color: tint)
            Circle().fill(tint).frame(width: 10, height: 10)
                .overlay(Circle().strokeBorder(Color.primary, lineWidth: 1.5))
            Text(verbatim: "Now \(hhmm(m)) · \(Int(level.rounded()))%\(manual ? " manual" : "")")
                .font(.system(size: 10, weight: .semibold)).monospacedDigit()
                .padding(.horizontal, 6).padding(.vertical, 2)
                .background(.thinMaterial, in: Capsule())
                .fixedSize()
                .offset(y: level > 80 ? 18 : -18)
        }
        .position(x: px(m, r), y: py(level, r))
        .allowsHitTesting(false)
        .animation(.spring(response: 0.6, dampingFraction: 0.8), value: level)
    }

    /// Minutes to the previous and next point (wrap-aware), for influence handles.
    func neighbours(_ p: SPoint) -> (prev: Double, next: Double) {
        let s = model.sorted()
        guard let i = s.firstIndex(where: { $0.0.id == p.id }), s.count > 1 else { return (60, 60) }
        let m = s[i].1, mp = s[(i - 1 + s.count) % s.count].1, mn = s[(i + 1) % s.count].1
        let dn = (mn - m + 1440).truncatingRemainder(dividingBy: 1440)
        let dp = (m - mp + 1440).truncatingRemainder(dividingBy: 1440)
        return (dp == 0 ? 1440 : dp, dn == 0 ? 1440 : dn)
    }

    /// After Effects style influence handles: drag sideways to soften the curve at the point.
    @ViewBuilder func easeHandles(_ p: SPoint, _ r: CGRect) -> some View {
        let m = p.minute(model.sunset, model.sunrise)
        let n = neighbours(p)
        let y = py(p.level, r)
        let x0 = px(m, r)
        let xin = px(m - p.easeIn / 100 * n.prev, r)
        let xout = px(m + (p.hold ? 0 : p.easeOut) / 100 * n.next, r)
        Path { path in
            path.move(to: CGPoint(x: max(r.minX, xin), y: y)); path.addLine(to: CGPoint(x: x0, y: y))
            if !p.hold { path.addLine(to: CGPoint(x: min(r.maxX, xout), y: y)) }
        }
        .stroke(Color.primary.opacity(0.5), lineWidth: 1)
        handleKnob(x: max(r.minX, xin), y: y) { x in
            setEase(p.id, inbound: true, value: (x0 - x) / (r.width / 1440) / n.prev * 100)
        }
        if !p.hold {
            handleKnob(x: min(r.maxX, xout), y: y) { x in
                setEase(p.id, inbound: false, value: (x - x0) / (r.width / 1440) / n.next * 100)
            }
        }
    }

    func handleKnob(x: CGFloat, y: CGFloat, drag: @escaping (CGFloat) -> Void) -> some View {
        RoundedRectangle(cornerRadius: 2)
            .fill(Color.primary)
            .frame(width: 9, height: 9)
            .rotationEffect(.degrees(45))
            .position(x: x, y: y)
            .gesture(DragGesture(minimumDistance: 0)
                .onChanged { drag($0.location.x) }
                .onEnded { _ in model.changed() })
    }

    func setEase(_ id: UUID, inbound: Bool, value: Double) {
        guard let i = model.points.firstIndex(where: { $0.id == id }) else { return }
        let v = min(100, max(0, value.rounded()))
        if inbound { model.points[i].easeIn = v } else { model.points[i].easeOut = v }
    }

    func add(at loc: CGPoint, _ r: CGRect) {
        let m = Int(((loc.x - r.minX) / r.width * 1440 / 5).rounded() * 5)
        guard (0..<1440).contains(m) else { return }
        let v = model.value(at: Double(m))
        let p = SPoint(time: m, level: v.level.rounded(), kelvin: v.kelvin)
        model.points.append(p)
        selected = p.id
        model.changed()
    }

    @ViewBuilder var inspector: some View {
        if let i = model.points.firstIndex(where: { $0.id == selected }) {
            let p = model.points[i]
            VStack(alignment: .leading, spacing: 10) {
                HStack(spacing: 14) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(hhmm(p.minute(model.sunset, model.sunrise))).font(.system(size: 18, weight: .semibold)).monospacedDigit()
                    Text(p.sun == nil ? "fixed time" : "\(p.sun!) \(p.offset >= 0 ? "+" : "")\(p.offset) min")
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                }
                .frame(width: 96, alignment: .leading)
                VStack(alignment: .leading, spacing: 4) {
                    Text("Level \(Int(p.level))%").font(.system(size: 11)).foregroundStyle(.secondary)
                    Slider(value: Binding(get: { model.points[i].level },
                                          set: { model.points[i].level = $0.rounded(); model.changed() }), in: 0...100)
                }
                VStack(alignment: .leading, spacing: 4) {
                    Text(verbatim: "Warmth \(Int((p.kelvin / 50).rounded() * 50))K").font(.system(size: 11)).foregroundStyle(.secondary)
                    Slider(value: Binding(get: { model.points[i].kelvin },
                                          set: { model.points[i].kelvin = $0; model.changed() }), in: 2700...6500)
                        .tint(Color(nsColor: kelvinColor(p.kelvin)))
                }
                }
                HStack(alignment: .top, spacing: 14) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(verbatim: "Ease in \(Int(p.easeIn))% · out \(Int(p.easeOut))%")
                        .font(.system(size: 11)).foregroundStyle(.secondary)
                    HStack(spacing: 4) {
                        ForEach([("Linear", 0.0), ("Easy", 33.0), ("Soft", 66.0), ("Max", 90.0)], id: \.0) { e in
                            Button(e.0) {
                                withAnimation(.spring(response: 0.35, dampingFraction: 0.8)) {
                                    model.points[i].easeIn = e.1; model.points[i].easeOut = e.1
                                    model.points[i].hold = false
                                }
                                model.changed()
                            }
                            .buttonStyle(.plain).font(.system(size: 10))
                            .padding(.horizontal, 6).padding(.vertical, 3)
                            .background(.primary.opacity(abs(p.easeIn - e.1) < 1 && abs(p.easeOut - e.1) < 1 ? 0.2 : 0.07),
                                        in: Capsule())
                        }
                    }
                    Toggle("Hold until next", isOn: Binding(get: { model.points[i].hold },
                                                            set: { model.points[i].hold = $0; model.changed() }))
                        .font(.system(size: 11))
                }
                VStack(alignment: .leading, spacing: 6) {
                    Toggle("Follow sunset", isOn: Binding(
                        get: { model.points[i].sun != nil },
                        set: { on in
                            let m = model.points[i].minute(model.sunset, model.sunrise)
                            if on { model.points[i].sun = "sunset"; model.points[i].offset = Int(m - model.sunset); model.points[i].time = nil }
                            else { model.points[i].time = Int(m); model.points[i].sun = nil }
                            model.changed()
                        }))
                        .font(.system(size: 11))
                    Button(role: .destructive) {
                        guard model.points.count > 2 else { return }
                        model.points.remove(at: i); selected = nil; model.changed()
                    } label: { Label("Delete", systemImage: "trash") }
                        .font(.system(size: 11)).disabled(model.points.count <= 2)
                }
            }
            }
            .padding(12)
            .background(.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 10))
            .overlay(alignment: .topTrailing) {
                Button { withAnimation(.easeOut(duration: 0.2)) { selected = nil } } label: {
                    Image(systemName: "xmark").font(.system(size: 9, weight: .semibold))
                        .frame(width: 18, height: 18).background(.primary.opacity(0.08), in: Circle())
                }
                .buttonStyle(.plain).padding(6).help("Close point settings")
            }
            .transition(.opacity.combined(with: .move(edge: .bottom)))
        } else {
            Text("Drag a point to move it. Double-click the chart to add a point. Select a point: drag its side handles to soften the curve, set warmth and sunset follow.")
                .font(.system(size: 12)).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, minHeight: 58, alignment: .leading)
        }
    }
}
