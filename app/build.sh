#!/bin/zsh
# Builds tinyLight.app (arm64, macOS 14+) into app/build/.
# Signing: SIGN_ID=<identity hash or name> for a stable identity (keeps microphone/Bluetooth grants
# across rebuilds); default is ad-hoc ("-"), which makes macOS ask for permissions after every rebuild.
set -e
cd "$(dirname $0)"
A=build/tinyLight.app
mkdir -p $A/Contents/MacOS
swiftc -O -parse-as-library -target arm64-apple-macos14 TinyLightApp.swift Schedule.swift Alarm.swift AlarmPlayer.swift Calibrate.swift PhoneQR.swift -o $A/Contents/MacOS/tinyLight
cat > $A/Contents/Info.plist <<P
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleIdentifier</key><string>dev.tinylight.bar</string>
<key>CFBundleName</key><string>tinyLight</string>
<key>CFBundleExecutable</key><string>tinyLight</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>CFBundleShortVersionString</key><string>0.1.0</string>
<key>CFBundleVersion</key><string>1</string>
<key>LSMinimumSystemVersion</key><string>14.0</string>
<key>LSUIElement</key><true/>
<key>NSMicrophoneUsageDescription</key><string>Hear calibration clicks so every speaker sounds at the same moment and loudness at your bed.</string>
<key>NSBluetoothAlwaysUsageDescription</key><string>Measure the distance between Macs to keep alarm sound in sync when one moves.</string>
<key>NSAppTransportSecurity</key><dict><key>NSAllowsLocalNetworking</key><true/><key>NSAllowsArbitraryLoads</key><true/></dict>
</dict></plist>
P
codesign --force --timestamp=none --sign "${SIGN_ID:--}" $A
echo built $A
