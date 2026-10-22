// Phone link as a QR code: one page (/app) = remote, with night mode inside it. URLs come from the bridge (/links):
// the permanent Tailscale HTTPS address when it exists, else the home-network address with the token.
import SwiftUI
import AppKit
import CoreImage.CIFilterBuiltins

func qrImage(_ text: String, size: CGFloat = 132) -> NSImage? {
    let f = CIFilter.qrCodeGenerator()
    f.message = Data(text.utf8)
    f.correctionLevel = "M"
    guard let ci = f.outputImage else { return nil }
    let scaled = ci.transformed(by: CGAffineTransform(scaleX: size / ci.extent.width * 2, y: size / ci.extent.height * 2))
    let rep = NSCIImageRep(ciImage: scaled)
    let img = NSImage(size: NSSize(width: size, height: size))
    img.addRepresentation(rep)
    return img
}

struct PhoneQRView: View {
    @EnvironmentObject var light: Light
    @State private var link: String?
    @State private var note = ""
    @State private var copied: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            card("Room light", "Remote and night mode", link)
            Text(note).font(.system(size: 10)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
        .task { await load() }
    }

    func card(_ title: String, _ sub: String, _ url: String?) -> some View {
        VStack(spacing: 6) {
            if let url, let img = qrImage(url) {
                Image(nsImage: img).interpolation(.none).resizable().frame(width: 118, height: 118)
                    .padding(6).background(.white, in: RoundedRectangle(cornerRadius: 10))
            } else {
                RoundedRectangle(cornerRadius: 10).fill(.primary.opacity(0.06)).frame(width: 130, height: 130)
                    .overlay(ProgressView().controlSize(.small))
            }
            Text(title).font(.system(size: 12, weight: .semibold))
            Text(sub).font(.system(size: 10)).foregroundStyle(.secondary)
            Button(copied == url && url != nil ? "Copied" : "Copy link") {
                guard let url else { return }
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(url, forType: .string)
                withAnimation { copied = url }
            }
            .buttonStyle(.plain).font(.system(size: 10))
            .padding(.horizontal, 8).padding(.vertical, 3)
            .background(.primary.opacity(0.08), in: Capsule())
        }
        .frame(maxWidth: .infinity)
    }

    func load() async {
        guard let j = await light.get(light.url("/links")) else { note = "Bridge not reachable."; return }
        if let ts = j["tailscale"] as? String {
            link = ts + "/app"
            note = "Permanent link over Tailscale (turn Tailscale on in the phone). Scan, then Share → Add to Home Screen."
        } else if let lan = j["lan"] as? String, let tok = j["token"] as? String {
            link = lan + "/app?token=" + tok
            note = "Home Wi-Fi only (no HTTPS: the phone microphone stays off). Scan, then Add to Home Screen."
        }
    }
}
