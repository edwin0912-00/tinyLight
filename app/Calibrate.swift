// Acoustic + Bluetooth calibration so every device sounds at the same moment and loudness at the bed.
import AVFoundation
import CoreBluetooth
import SwiftUI

private let ticksPerSecond: Double = {
    var tb = mach_timebase_info_data_t(); mach_timebase_info(&tb)
    return 1e9 * Double(tb.denom) / Double(tb.numer)
}()

/// Local wall clock (epoch seconds) of a mach host time.
func epoch(ofHost h: UInt64) -> Double {
    Date().timeIntervalSince1970 - (Double(mach_absolute_time()) - Double(h)) / ticksPerSecond
}

/// mach host time of a local epoch moment.
func host(ofEpoch e: Double) -> UInt64 {
    UInt64(max(0, Double(mach_absolute_time()) + (e - Date().timeIntervalSince1970) * ticksPerSecond))
}

struct DeviceCal: Identifiable {
    var id: String
    var offsetMs: Double
    var gain: Double
    var delayMs: Double?
    var levelDb: Double?
    var rssiRef: Double?
    var online: Bool
}

/// The bridge Mac advertises this service so other Macs can measure their distance to it.
nonisolated(unsafe) let beaconUUID = CBUUID(string: "6E4C1A20-7B31-4D2B-9C55-0B11A7E5A0C1")

@MainActor
final class Calibrator: NSObject, ObservableObject, CBCentralManagerDelegate, CBPeripheralManagerDelegate {
    @Published var devices: [DeviceCal] = []
    @Published var status = ""
    @Published var running = false
    @Published var bleDistance: Double?     // metres to the bridge beacon, smoothed
    @Published var rssi: Double?
    weak var light: Light?
    weak var player: AlarmPlayer?
    private var engine: AVAudioEngine?
    private var samples: [(t: Double, v: Float)] = []
    private var central: CBCentralManager?
    private var rssiWindow: [Double] = []
    private var beacon: CBPeripheralManager?
    @Published var isBeacon = false
    let me = Host.current().localizedName ?? "mac"

    func attach(_ l: Light, _ p: AlarmPlayer) {
        guard light == nil else { return }
        light = l; player = p
        l.onCalibrate = { [weak self] ev in self?.handle(ev) }
        // The Mac that runs the bridge is the anchor: it advertises, every other Mac measures.
        if ["127.0.0.1", "localhost"].contains(l.baseURL.host ?? "") {
            isBeacon = true
            beacon = CBPeripheralManager(delegate: self, queue: nil)
        } else {
            startBLE()
        }
        Task { await load() }
        Task {  // keep the bridge's view of this Mac fresh (it forgets status on restart)
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 30_000_000_000)
                report(isBeacon ? "beacon on" : (rssi == nil ? "scanning" : "scanning, beacon rssi \(Int(rssi!))"))
            }
        }
    }

    /// Bluetooth calibration: distance to the bridge beacon -> how long this Mac waits for its sound.
    func calibrateBluetooth() {
        guard let l = light else { return }
        report("scanning, beacon rssi \(Int(rssi ?? 0))")
        Task {
            try? await Task.sleep(nanoseconds: 300_000_000)
            let j = await l.get(l.url("/calibrate/ble")) ?? [:]
            status = j.isEmpty ? "No Bluetooth reading yet. Wait a few seconds and retry."
                : "Bluetooth · " + j.keys.sorted().map { k in
                    let r = j[k] as? [String: Any] ?? [:]
                    let m = (r["distance_m"] as? NSNumber).map { String(format: " %.1f m", $0.doubleValue) } ?? ""
                    return "\(k.components(separatedBy: " ").first ?? k):\(m) offset \((r["offset_ms"] as? NSNumber)?.intValue ?? 0) ms"
                }.joined(separator: " · ")
            await load()
        }
    }

    func reset() {
        guard let l = light else { return }
        Task { _ = await l.get(l.url("/calibrate/reset")); status = "Reset: no offsets, no gain"; await load() }
    }

    func load() async {
        guard let l = light, let j = await l.get(l.url("/devices")) else { return }
        let online = Set(j["_online"] as? [String] ?? [])
        var list: [DeviceCal] = []
        for name in Set(online).union(j.keys.filter { !$0.hasPrefix("_") }).sorted() {
            let d = j[name] as? [String: Any] ?? [:]
            list.append(DeviceCal(id: name, offsetMs: (d["offset_ms"] as? NSNumber)?.doubleValue ?? 0,
                                  gain: (d["gain"] as? NSNumber)?.doubleValue ?? 1,
                                  delayMs: (d["delay_ms"] as? NSNumber)?.doubleValue,
                                  levelDb: (d["level_db"] as? NSNumber)?.doubleValue,
                                  rssiRef: (d["rssi_ref"] as? NSNumber)?.doubleValue, online: online.contains(name)))
        }
        devices = list
    }

    /// Ask the bridge to run a calibration with this Mac as the microphone.
    func start() {
        guard let l = light else { return }
        running = true
        status = "Asking for the microphone…"
        AVCaptureDevice.requestAccess(for: .audio) { ok in
            Task { @MainActor in
                guard ok else { self.status = "Microphone access denied. Allow tinyLight in Privacy & Security."; self.running = false; return }
                self.startBLE()
                await l.syncClock()
                guard let j = await l.get(l.url("/calibrate", ["recorder": self.me])) else { self.running = false; return }
                if let e = j["error"] as? String { self.status = e; self.running = false }
            }
        }
    }

    func handle(_ ev: [String: Any]) {
        guard let slots = ev["slots"] as? [[String: Any]] else { return }
        let clicks = (ev["clicks"] as? NSNumber)?.intValue ?? 4
        let interval = (ev["interval"] as? NSNumber)?.doubleValue ?? 0.5
        if let mine = slots.first(where: { $0["device"] as? String == me }), let at = (mine["at"] as? NSNumber)?.doubleValue {
            player?.playClicks(atServer: at, count: clicks, interval: interval)
            report("clicks scheduled")
        }
        if ev["recorder"] as? String == me {
            record(slots: slots, clicks: clicks, interval: interval)
        }
    }

    // MARK: recording + analysis

    private func record(slots: [[String: Any]], clicks: Int, interval: Double) {
        guard let l = light else { return }
        let ats = slots.compactMap { ($0["at"] as? NSNumber)?.doubleValue }
        guard let last = ats.max() else { return }
        running = true
        status = "Listening… keep the room quiet"
        samples = []
        let e = AVAudioEngine()
        let input = e.inputNode
        let fmt = input.outputFormat(forBus: 0)
        let inLatency = Double(input.presentationLatency)
        let offset = l.serverOffset
        input.installTap(onBus: 0, bufferSize: 1024, format: fmt) { [weak self] buf, when in
            guard let ch = buf.floatChannelData?[0] else { return }
            let t0 = epoch(ofHost: when.hostTime) - inLatency + offset   // server time of sample 0
            let sr = fmt.sampleRate
            var chunk: [(Double, Float)] = []
            chunk.reserveCapacity(Int(buf.frameLength) / 48 + 1)
            var i = 0
            while i < Int(buf.frameLength) {      // 1 ms envelope (peak)
                var peak: Float = 0
                for k in i..<min(i + 48, Int(buf.frameLength)) { peak = max(peak, abs(ch[k])) }
                chunk.append((t0 + Double(i) / sr, peak))
                i += 48
            }
            Task { @MainActor in self?.samples.append(contentsOf: chunk.map { (t: $0.0, v: $0.1) }) }
        }
        do { try e.start() } catch { status = "Microphone failed: \(error.localizedDescription)"; running = false; return }
        engine = e
        let stopAt = last + Double(clicks) * interval + 1.5
        Task {
            try? await Task.sleep(nanoseconds: UInt64(max(1, stopAt - l.serverNow) * 1e9))
            e.inputNode.removeTap(onBus: 0); e.stop(); engine = nil
            await analyse(slots: slots, clicks: clicks, interval: interval)
        }
    }

    /// Send the envelope to the bridge; it finds each device's clicks (tinylight/server.py analyse_calibration).
    private func analyse(slots: [[String: Any]], clicks: Int, interval: Double) async {
        guard let l = light else { running = false; return }
        let body: [String: Any] = ["recorder": me, "rssi": rssi as Any, "slots": slots, "clicks": clicks,
                                   "interval": interval, "env": samples.map { [$0.t, Double($0.v)] }]
        let data = try? JSONSerialization.data(withJSONObject: body)
        let res = await l.get(l.url("/calibrate/result"), body: data)
        let found = res?["devices"] as? [String: [String: Any]] ?? [:]
        status = found.isEmpty ? "Nothing heard. Turn the volume up and retry."
            : "Calibrated · " + found.keys.sorted().map { k in
                let r = found[k]!
                return String(format: "%@: %.0f ms", k, ((r["delay"] as? NSNumber)?.doubleValue ?? 0) * 1000)
            }.joined(separator: " · ")
        running = false
        await load()
    }

    /// Tell the bridge what this Mac's Bluetooth is doing (diagnostics in /devices "_status").
    func report(_ ble: String) {
        guard let l = light else { return }
        let body: [String: Any] = ["device": me, "ble": ble, "beacon": isBeacon, "rssi": rssi as Any,
                                   "audio": player?.diag ?? "", "last_click": player?.lastClick ?? "",
                                   "clock_offset_ms": (l.serverOffset * 1000).rounded()]
        Task { _ = await l.get(l.url("/device_status"), body: try? JSONSerialization.data(withJSONObject: body)) }
    }

    // MARK: Bluetooth drift: distance to the bridge beacon (next to the light)

    func startBLE() {
        guard central == nil else { return }
        central = CBCentralManager(delegate: self, queue: nil)
    }

    nonisolated func centralManagerDidUpdateState(_ c: CBCentralManager) {
        let st = c.state.rawValue
        Task { @MainActor in self.report("central state \(st)") }  // 5 = poweredOn, 3 = unauthorized
        if c.state == .poweredOn {
            c.scanForPeripherals(withServices: [beaconUUID],
                                 options: [CBCentralManagerScanOptionAllowDuplicatesKey: true])
        }
    }

    nonisolated func centralManager(_ c: CBCentralManager, didDiscover p: CBPeripheral,
                                    advertisementData: [String: Any], rssi: NSNumber) {
        let v = rssi.doubleValue
        guard v < 0 else { return }
        Task { @MainActor in
            self.rssiWindow.append(v)
            if self.rssiWindow.count > 25 { self.rssiWindow.removeFirst() }
            let med = self.rssiWindow.sorted()[self.rssiWindow.count / 2]
            self.rssi = med
            self.bleDistance = pow(10, (-59 - med) / 20)   // log-distance model, 1 m = -59 dBm, n = 2
            self.player?.bleAdjustMs = self.bleAdjustMs()
            if self.rssiWindow.count == 25 { self.report("scanning, beacon rssi \(Int(med))") }
        }
    }

    nonisolated func peripheralManagerDidUpdateState(_ p: CBPeripheralManager) {
        let st = p.state.rawValue
        Task { @MainActor in self.report("beacon state \(st)") }
        if p.state == .poweredOn {
            p.startAdvertising([CBAdvertisementDataServiceUUIDsKey: [beaconUUID],
                                CBAdvertisementDataLocalNameKey: "tinyLightBeacon"])
        }
    }

    /// Moving away from the bridge Mac (next to the light and bed) delays this device's sound: play earlier by d/343 m/s.
    func bleAdjustMs() -> Double {
        guard let ref = devices.first(where: { $0.id == me })?.rssiRef, let r = rssi else { return 0 }
        let dRef = pow(10, (-59 - ref) / 20), dNow = pow(10, (-59 - r) / 20)
        return -(dNow - dRef) / 343 * 1000
    }
}

struct CalibrationView: View {
    @EnvironmentObject var cal: Calibrator
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("Sound sync").font(.system(size: 12, weight: .semibold))
                Text("put this Mac where your head is").font(.system(size: 11)).foregroundStyle(.secondary)
                Spacer()
                Button(cal.running ? "Listening…" : "By sound") { cal.start() }
                    .disabled(cal.running).font(.system(size: 11))
                    .help("Clicks from every Mac, measured by this Mac's microphone")
                Button("By Bluetooth") { cal.calibrateBluetooth() }
                    .font(.system(size: 11)).disabled(cal.isBeacon && cal.devices.count < 2)
                    .help("Distance to the bridge beacon; assumes you are next to this Mac")
                Button("Reset") { cal.reset() }.font(.system(size: 11))
            }
            ForEach(cal.devices) { d in
                HStack(spacing: 10) {
                    Circle().fill(d.online ? Color.green : Color.gray).frame(width: 6, height: 6)
                    Text(d.id).font(.system(size: 11)).lineLimit(1).frame(width: 160, alignment: .leading)
                    Text(verbatim: String(format: "offset %+.0f ms", d.offsetMs)).font(.system(size: 11)).monospacedDigit()
                    Text(verbatim: String(format: "gain ×%.2f", d.gain)).font(.system(size: 11)).monospacedDigit()
                    if let lv = d.levelDb { Text(verbatim: String(format: "%.0f dB", lv)).font(.system(size: 11)).foregroundStyle(.secondary) }
                    if d.id == cal.me, cal.isBeacon {
                        Text("BLE beacon").font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                    if d.id == cal.me, let m = cal.bleDistance {
                        Text(verbatim: String(format: "BLE %.1f m to bridge · %+.1f ms", m, cal.bleAdjustMs()))
                            .font(.system(size: 11)).foregroundStyle(.secondary).monospacedDigit()
                    }
                }
            }
            if !cal.status.isEmpty {
                Text(cal.status).font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(8)
        .background(.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 8))
        .task { await cal.load() }
    }
}
