// Synced alarm sound player on AVAudioEngine, pinned to the built-in speakers.
import AVFoundation
import CoreAudio
import AudioToolbox
import Foundation

/// CoreAudio helpers: find the built-in speakers and read/set their volume and latency.
enum Speakers {
    static func prop<T>(_ id: AudioObjectID, _ sel: AudioObjectPropertySelector,
                        _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal, _ el: UInt32 = 0, _ v: inout T) -> Bool {
        var a = AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: el)
        var size = UInt32(MemoryLayout<T>.size)
        return AudioObjectGetPropertyData(id, &a, 0, nil, &size, &v) == noErr
    }

    /// The Mac's own speakers (transport = built-in, has output), whatever the system default is.
    static func builtIn() -> AudioObjectID? {
        var a = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices,
                                           mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, &size)
        var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &a, 0, nil, &size, &ids)
        for id in ids {
            var transport: UInt32 = 0
            _ = prop(id, kAudioDevicePropertyTransportType, kAudioObjectPropertyScopeGlobal, 0, &transport)
            var sa = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreams,
                                                mScope: kAudioObjectPropertyScopeOutput, mElement: kAudioObjectPropertyElementMain)
            var ss: UInt32 = 0
            AudioObjectGetPropertyDataSize(id, &sa, 0, nil, &ss)
            if transport == kAudioDeviceTransportTypeBuiltIn && ss > 0 { return id }
        }
        return nil
    }

    /// Output latency in seconds (device + safety + buffer + stream), used to start earlier.
    static func uid(_ id: AudioObjectID) -> String? {
        var uid: CFString = "" as CFString
        return prop(id, kAudioDevicePropertyDeviceUID, kAudioObjectPropertyScopeGlobal, 0, &uid) ? uid as String : nil
    }

    static func latency(_ id: AudioObjectID) -> Double {
        var lat: UInt32 = 0, safety: UInt32 = 0, buf: UInt32 = 0, rate: Float64 = 48000, stream: UInt32 = 0
        _ = prop(id, kAudioDevicePropertyLatency, kAudioObjectPropertyScopeOutput, 0, &lat)
        _ = prop(id, kAudioDevicePropertySafetyOffset, kAudioObjectPropertyScopeOutput, 0, &safety)
        _ = prop(id, kAudioDevicePropertyBufferFrameSize, kAudioObjectPropertyScopeGlobal, 0, &buf)
        _ = prop(id, kAudioDevicePropertyNominalSampleRate, kAudioObjectPropertyScopeGlobal, 0, &rate)
        var sa = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreams,
                                            mScope: kAudioObjectPropertyScopeOutput, mElement: kAudioObjectPropertyElementMain)
        var ss: UInt32 = 0
        AudioObjectGetPropertyDataSize(id, &sa, 0, nil, &ss)
        var streams = [AudioStreamID](repeating: 0, count: Int(ss) / 4)
        AudioObjectGetPropertyData(id, &sa, 0, nil, &ss, &streams)
        if let s = streams.first { _ = prop(s, kAudioStreamPropertyLatency, kAudioObjectPropertyScopeGlobal, 0, &stream) }
        return Double(lat + safety + buf + stream) / rate
    }

    static func volume(_ id: AudioObjectID) -> (Float32, Bool) {
        var v: Float32 = 0.5, m: UInt32 = 0
        if !prop(id, kAudioHardwareServiceDeviceProperty_VirtualMainVolume, kAudioObjectPropertyScopeOutput, 0, &v) {
            _ = prop(id, kAudioDevicePropertyVolumeScalar, kAudioObjectPropertyScopeOutput, 1, &v)
        }
        _ = prop(id, kAudioDevicePropertyMute, kAudioObjectPropertyScopeOutput, 0, &m)
        return (v, m != 0)
    }

    static func setVolume(_ id: AudioObjectID, _ v: Float32, muted: Bool) {
        var vol = v, mute: UInt32 = muted ? 1 : 0
        var a = AudioObjectPropertyAddress(mSelector: kAudioHardwareServiceDeviceProperty_VirtualMainVolume,
                                           mScope: kAudioObjectPropertyScopeOutput, mElement: kAudioObjectPropertyElementMain)
        if AudioObjectSetPropertyData(id, &a, 0, nil, UInt32(MemoryLayout<Float32>.size), &vol) != noErr {
            for ch: UInt32 in [1, 2] {
                a = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyVolumeScalar, mScope: kAudioObjectPropertyScopeOutput, mElement: ch)
                AudioObjectSetPropertyData(id, &a, 0, nil, UInt32(MemoryLayout<Float32>.size), &vol)
            }
        }
        a = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyMute, mScope: kAudioObjectPropertyScopeOutput, mElement: kAudioObjectPropertyElementMain)
        AudioObjectSetPropertyData(id, &a, 0, nil, UInt32(MemoryLayout<UInt32>.size), &mute)
    }
}

/// Plays the alarm sound in sync with every other device: position = (shared clock - start)
/// + this device's calibration offset + its own output latency. Volume follows the curve.
/// AVAudioPlayer with currentDevice = built-in speakers (AVAudioEngine hung on a Mac whose default output was a virtual device).
@MainActor
final class AlarmPlayer {
    weak var light: Light?
    private var players: [String: AVAudioPlayer] = [:]
    private var current: String?
    private var timer: Timer?
    private var ev: [String: Any] = [:]
    private var pts: [APoint] = []
    private var saved: (Float32, Bool)?
    private var offsetMs: Double = 0
    private var gain: Double = 1
    var bleAdjustMs: Double = 0      // Bluetooth drift correction (Calibrator)
    private let device: AudioObjectID? = Speakers.builtIn()
    private lazy var deviceUID: String? = device.flatMap(Speakers.uid)
    private lazy var outLatency: Double = device.map(Speakers.latency) ?? 0
    let deviceName = Host.current().localizedName ?? "mac"
    var diag: String {
        String(format: "speaker id %d uid %@ latency %.1f ms", Int(device ?? 0), deviceUID ?? "nil", outLatency * 1000)
    }

    func update(_ e: [String: Any]) {
        let st = e["state"] as? String ?? "idle"
        guard st == "dawn" || st == "ringing" || st == "snoozed",
              (e["sound_points"] as? [[String: Any]])?.isEmpty == false else { stopAll(); return }
        if (e["key"] as? String) != (ev["key"] as? String) { stopAll() }
        ev = e
        pts = (e["sound_points"] as? [[String: Any]] ?? []).map { APoint(json: $0, key: "volume") }
        Task { await loadOffset() }
        if timer == nil {
            timer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] _ in
                MainActor.assumeIsolated { self?.tick() }
            }
        }
    }

    private func loadOffset() async {
        guard let l = light, let j = await l.get(l.url("/devices")),
              let d = j[deviceName] as? [String: Any] else { return }
        offsetMs = (d["offset_ms"] as? NSNumber)?.doubleValue ?? 0
        gain = (d["gain"] as? NSNumber)?.doubleValue ?? 1
    }

    private func soundName(at x: Double) -> String? {
        var name = ev["sound"] as? String
        for p in pts.sorted(by: { $0.t < $1.t }) where p.t <= x && p.sound != nil { name = p.sound }
        return name
    }

    private func tick() {
        guard let l = light, let T = (ev["T"] as? NSNumber)?.doubleValue else { return }
        let scale = (ev["scale"] as? NSNumber)?.doubleValue ?? 1
        // What the listener should hear now = what leaves the speaker after outLatency.
        let now = l.serverNow + outLatency - (offsetMs + bleAdjustMs) / 1000
        let x = (now - T) / 60 * scale
        let snoozed = (ev["state"] as? String) == "snoozed"
        // Snooze: fade out over 4 s; afterwards the sound returns over fade_in seconds with the light.
        let snoozeAt = (ev["snooze_at"] as? NSNumber)?.doubleValue ?? 0
        let fadeOut = snoozed ? max(0, 1 - (now - snoozeAt) / 4) : 1
        guard let seg = curveValue(pts, x), fadeOut > 0, let name = soundName(at: x) else {
            players.values.forEach { $0.volume = 0 }
            return
        }
        var v = (seg.a.value + (seg.b.value - seg.a.value) * seg.q) * fadeOut
        let fadeIn = (ev["fade_in"] as? NSNumber)?.doubleValue ?? 30
        if !snoozed, let r = (ev["resume_at"] as? NSNumber)?.doubleValue { v *= min(1, max(0, (now - r) / fadeIn)) }
        guard let p = player(name) else { return }
        if current != name {
            if let old = current { players[old]?.volume = 0 }
            current = name
            let startX = pts.filter { $0.sound == name }.map(\.t).min() ?? pts.map(\.t).min() ?? x
            // Start 0.25 s from now at the exact shared position, so all devices line up.
            let lead = 0.25
            let elapsed = max(0, (x - startX) * 60 / scale + lead)
            p.currentTime = elapsed.truncatingRemainder(dividingBy: max(1, p.duration))
            p.volume = 0
            p.play(atTime: p.deviceCurrentTime + lead)
            raiseVolume()
        }
        p.volume = Float(min(1, pow(v / 100, 2) * gain))  // perceptual curve x calibrated gain
    }

    private func cacheURL(_ name: String) -> URL {
        let dir = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("tinyLight/sounds", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir.appendingPathComponent(name)
    }

    private func player(_ name: String) -> AVAudioPlayer? {
        if let p = players[name] { return p }
        let f = cacheURL(name + ".m4a")
        if !FileManager.default.fileExists(atPath: f.path), let l = light,
           let data = try? Data(contentsOf: l.url("/sounds/\(name).m4a")) {
            try? data.write(to: f)  // same file on every device, fetched from the bridge once
        }
        guard let p = try? AVAudioPlayer(contentsOf: f) else { return nil }
        p.currentDevice = deviceUID     // built-in speakers, whatever the system default is
        p.numberOfLoops = -1
        p.prepareToPlay()
        players[name] = p
        return p
    }

    /// The alarm must be audible: unmute the speakers and lift them to at least 50 %, restore after.
    private func raiseVolume() {
        guard saved == nil, let d = device else { return }
        let cur = Speakers.volume(d)
        saved = cur
        Speakers.setVolume(d, max(cur.0, 0.5), muted: false)
    }

    private func restoreVolume() {
        if let s = saved, let d = device { Speakers.setVolume(d, s.0, muted: s.1); saved = nil }
    }

    func stopAll() {
        timer?.invalidate(); timer = nil
        let ps = Array(players.values)
        ps.forEach { $0.setVolume(0, fadeDuration: 0.5) }
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 600_000_000)
            ps.forEach { $0.stop() }
            self.restoreVolume()
        }
        players = [:]
        current = nil
        ev = [:]
    }

    /// Calibration: 4 short 2.5 kHz clicks that leave this speaker exactly at bridge time `at`.
    private var clickPlayer: AVAudioPlayer?
    var lastClick = ""
    func playClicks(atServer at: Double, count: Int, interval: Double) {
        guard let l = light else { return }
        let sr = 48000.0
        let n = Int(sr * (Double(count) * interval + 0.2)), w = Int(sr * 0.004)
        var pcm = [Int16](repeating: 0, count: n)
        for k in 0..<count {
            let s0 = Int(Double(k) * interval * sr)
            for i in 0..<w {
                let win = 0.5 - 0.5 * cos(2 * Double.pi * Double(i) / Double(w))
                pcm[s0 + i] = Int16(30000 * win * sin(2 * Double.pi * 2500 * Double(i) / sr))
            }
        }
        var wav = Data()
        func u32(_ v: UInt32) { withUnsafeBytes(of: v.littleEndian) { wav.append(contentsOf: $0) } }
        func u16(_ v: UInt16) { withUnsafeBytes(of: v.littleEndian) { wav.append(contentsOf: $0) } }
        wav.append(contentsOf: Array("RIFF".utf8)); u32(UInt32(36 + n * 2)); wav.append(contentsOf: Array("WAVEfmt ".utf8))
        u32(16); u16(1); u16(1); u32(UInt32(sr)); u32(UInt32(sr) * 2); u16(2); u16(16)
        wav.append(contentsOf: Array("data".utf8)); u32(UInt32(n * 2))
        pcm.withUnsafeBytes { wav.append(contentsOf: $0) }
        let f = cacheURL("clicks.wav")
        try? wav.write(to: f)
        guard let p = try? AVAudioPlayer(contentsOf: f) else { lastClick = "player init failed"; return }
        p.currentDevice = deviceUID
        p.prepareToPlay()
        raiseVolume()
        let delay = at - l.serverNow - outLatency     // leave the speaker exactly at `at`
        let ok = p.play(atTime: p.deviceCurrentTime + max(0.05, delay))
        lastClick = String(format: "play(atTime:)=%d delay %.2f s device %@ dev %.2f", ok ? 1 : 0, delay, p.currentDevice ?? "nil", p.deviceCurrentTime)
        clickPlayer = p
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: UInt64(max(0, delay + Double(count) * interval + 1) * 1e9))
            if self.current == nil { self.restoreVolume() }
            self.clickPlayer = nil
        }
    }

    /// Ping: two soft bell tones (880 + 1320 Hz) on the built-in speakers.
    private var chimePlayer: AVAudioPlayer?
    func chime(volume: Double) {
        let sr = 48000.0, n = Int(sr * 1.5)
        var pcm = [Int16](repeating: 0, count: n)
        for (f, start) in [(880.0, 0.0), (1320.0, 0.18)] {
            let s0 = Int(start * sr)
            for i in 0..<(n - s0) {
                let t = Double(i) / sr
                let env = min(1, t / 0.02) * exp(-t * 4)
                let v = Double(pcm[s0 + i]) + 12000 * env * sin(2 * Double.pi * f * t)
                pcm[s0 + i] = Int16(max(-32767, min(32767, v)))
            }
        }
        var wav = Data()
        func u32(_ v: UInt32) { withUnsafeBytes(of: v.littleEndian) { wav.append(contentsOf: $0) } }
        func u16(_ v: UInt16) { withUnsafeBytes(of: v.littleEndian) { wav.append(contentsOf: $0) } }
        wav.append(contentsOf: Array("RIFF".utf8)); u32(UInt32(36 + n * 2)); wav.append(contentsOf: Array("WAVEfmt ".utf8))
        u32(16); u16(1); u16(1); u32(UInt32(sr)); u32(UInt32(sr) * 2); u16(2); u16(16)
        wav.append(contentsOf: Array("data".utf8)); u32(UInt32(n * 2))
        pcm.withUnsafeBytes { wav.append(contentsOf: $0) }
        let f = cacheURL("chime.wav")
        try? wav.write(to: f)
        guard let p = try? AVAudioPlayer(contentsOf: f) else { return }
        p.currentDevice = deviceUID
        p.volume = Float(volume)
        p.play()
        chimePlayer = p
    }

    /// Preview a sound locally (Alarm tab ▶).
    private var previewing: String?
    func togglePreview(_ name: String) -> Bool {
        if let p = previewing { players[p]?.stop(); players[p] = nil; previewing = nil; if p == name { return false } }
        guard let pl = player(name) else { return false }
        pl.currentTime = 0
        pl.volume = 0.35
        pl.play()
        previewing = name
        return true
    }
}
