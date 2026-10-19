# Nanlite on Feasycom Bluetooth mesh

Verified on a Nanlite room light in October 2026. Model families reported by others with the same SoC:
FS-300B, PavoTube II, Wand. Code: `tinylight/drivers/nanlite_mesh/`.

## Link

| What | Value |
|---|---|
| Advertised | `0x1827` Mesh Provisioning (unpaired) or `0x1828` Mesh Proxy (paired); name `"\r\nAT+MODE"` |
| GATT | `0xFFF0` Feasycom (fff1 notify, fff2 write), Airoha DFU, `0x1828` proxy (2add write-without-response, 2ade notify) |
| Auth | TEA handshake on fff2: `"AUTH" + TEA(challenge) * 2`; 32 rounds worked |
| Keys | Provision with your own NetKey; AppKey = Bluetooth Mesh sample key; bind to vendor model 0x1111/0x1111 |
| Control | Vendor model opcode `C1 11 11`, 8-byte "fast" payload or the extended `set_mode` packet |
| Pairing | Remove the light in the NANLINK app -> it advertises `0x1827` -> `tinylight connect` |

macOS notes: the proxy characteristic accepts only write-without-response (BlueZ tolerated write-with-response);
mesh segments can go back to back (20 ms gap; 100 ms made a CCT change take ~0.5 s).

## Commands

| Goal | Packet | Notes |
|---|---|---|
| Brightness | fast option `0x01`, value 0-100 | Works in any mode |
| White: CCT + tint | `set_mode` 02:02, params `01 CCT(2) GM(int16 signed, 0 = neutral) 01` | **Only** way that is right on this model |
| Color | `set_mode` 03:01, params `HUE(2, 0-360) SAT(1, 0-100)` | Fast hue/sat options set values but never switch mode |
| Back to white | `set_mode` 02:02 | |

`set_mode` header: `[roll, 0x24, 00 04, 01 01, 0x01, size, BRI(2), mode, submode, params]`, sent as one
segmented vendor message.

Dead end worth knowing: the fast CCT/GM options (`0x03`/`0x04`) use the FS-300B G/M encoding (0-100,
50 = neutral). On this light that value is read as signed, so "neutral" became +50 magenta and the white
looked wrong. The user caught it by eye; the fix was to send CCT and tint only through `set_mode`.

## The light while the bridge is connected

It stops advertising. Scans (including `tinylight scan`) do not see it until the bridge disconnects.
