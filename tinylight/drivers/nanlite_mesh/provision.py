"""
Bluetooth Mesh PB-GATT Provisioner for Nanlite FS-300B.

Implements the Mesh Provisioning Protocol (Mesh Profile 5.4) directly
over BLE GATT using bleak. After provisioning, keys are saved to
mesh_keys.json for use by the proxy client.
"""

import asyncio
import secrets
import struct

from bleak import BleakClient, BleakScanner
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.backends import default_backend

from .config import DEVICE_NAME, is_light, FEASYCOM_APP_KEY, KEYS_FILE, next_seq, save_keys
from .crypto import aes_cmac, k1, s1
from .mesh_proxy import MeshProxyClient

PROV_DATA_IN = "00002adb-0000-1000-8000-00805f9b34fb"
PROV_DATA_OUT = "00002adc-0000-1000-8000-00805f9b34fb"

# Provisioning PDU types
INVITE = 0x00
CAPABILITIES = 0x01
START = 0x02
PUBLIC_KEY = 0x03
CONFIRMATION = 0x05
RANDOM = 0x06
DATA = 0x07
COMPLETE = 0x08
FAILED = 0x09

SAR_COMPLETE = 0x00
SAR_FIRST = 0x01
SAR_CONTINUATION = 0x02
SAR_LAST = 0x03
TYPE_PROVISIONING = 0x03


def _aes_ccm_encrypt(key: bytes, nonce: bytes, plaintext: bytes) -> bytes:
    return AESCCM(key, tag_length=8).encrypt(nonce, plaintext, None)


def _wrap(prov_pdu: bytes) -> bytes:
    """Wrap a provisioning PDU in a Proxy PDU (SAR complete)."""
    return bytes([(SAR_COMPLETE << 6) | TYPE_PROVISIONING]) + prov_pdu


class MeshProvisioner:
    """Provisions a BLE Mesh device via PB-GATT."""

    def __init__(self):
        self._rx_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._reassembly_buf = bytearray()
        self.net_key = secrets.token_bytes(16)
        self.net_key_index = 0
        self.flags = 0x00
        self.iv_index = 0x00000000
        self.unicast_address = 0x0002

    def _on_notify(self, _sender, data: bytearray):
        raw = bytes(data)
        sar = (raw[0] >> 6) & 0x03
        if (raw[0] & 0x3F) != TYPE_PROVISIONING:
            return

        if sar == SAR_COMPLETE:
            self._rx_queue.put_nowait(raw[1:])
        elif sar == SAR_FIRST:
            self._reassembly_buf = bytearray(raw[1:])
        elif sar == SAR_CONTINUATION:
            self._reassembly_buf.extend(raw[1:])
        elif sar == SAR_LAST:
            self._reassembly_buf.extend(raw[1:])
            self._rx_queue.put_nowait(bytes(self._reassembly_buf))
            self._reassembly_buf.clear()

    async def _recv(self, timeout: float = 15.0) -> bytes:
        return await asyncio.wait_for(self._rx_queue.get(), timeout=timeout)

    async def _send(self, client: BleakClient, pdu: bytes):
        proxy = _wrap(pdu)
        mtu = max(getattr(client, "mtu_size", 23) - 3, 20)

        if len(proxy) <= mtu:
            await client.write_gatt_char(PROV_DATA_IN, proxy, response=False)
            return

        # SAR segmentation for large PDUs (e.g. public key exchange)
        first = proxy[1:mtu]
        await client.write_gatt_char(
            PROV_DATA_IN,
            bytes([(SAR_FIRST << 6) | TYPE_PROVISIONING]) + first,
            response=False,
        )
        await asyncio.sleep(0.02)
        remaining = proxy[len(first) + 1:]
        while len(remaining) > mtu - 1:
            chunk = remaining[:mtu - 1]
            remaining = remaining[mtu - 1:]
            await client.write_gatt_char(
                PROV_DATA_IN,
                bytes([(SAR_CONTINUATION << 6) | TYPE_PROVISIONING]) + chunk,
                response=False,
            )
            await asyncio.sleep(0.02)
        await client.write_gatt_char(
            PROV_DATA_IN,
            bytes([(SAR_LAST << 6) | TYPE_PROVISIONING]) + remaining,
            response=False,
        )

    def _check(self, raw: bytes, expected: int):
        if raw[0] == FAILED:
            raise RuntimeError(f"Provisioning failed: error 0x{raw[1]:02x}")
        if raw[0] != expected:
            raise RuntimeError(f"Expected PDU type 0x{expected:02x}, got 0x{raw[0]:02x}")

    async def provision(self, device) -> dict:
        """Provision a device and return the resulting keys."""
        addr = device.address if hasattr(device, "address") else device
        print(f"Connecting to {addr}...")
        client = BleakClient(device, timeout=15.0)
        await client.connect()
        try:
            return await self._do_provision(client, addr)
        finally:
            try:
                await client.disconnect()
            except (EOFError, Exception):
                pass

    async def _do_provision(self, client, addr) -> dict:
        """Run the provisioning protocol."""
        print(f"Connected (MTU={client.mtu_size})")
        await client.start_notify(PROV_DATA_OUT, self._on_notify)
        await asyncio.sleep(0.5)

        invite_params = bytes([5])
        await self._send(client, bytes([INVITE]) + invite_params)

        cap = await self._recv()
        self._check(cap, CAPABILITIES)
        num_elements = cap[1]
        print(f"Capabilities: {num_elements} element(s)")

        start_params = bytes([0x00, 0x00, 0x00, 0x00, 0x00])
        await self._send(client, bytes([START]) + start_params)
        await asyncio.sleep(0.1)

        priv_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
        pub = priv_key.public_key().public_numbers()
        our_xy = pub.x.to_bytes(32, "big") + pub.y.to_bytes(32, "big")
        await self._send(client, bytes([PUBLIC_KEY]) + our_xy)

        dev_pk = await self._recv()
        self._check(dev_pk, PUBLIC_KEY)
        dev_xy = dev_pk[1:65]
        dev_pub = ec.EllipticCurvePublicNumbers(
            int.from_bytes(dev_xy[:32], "big"),
            int.from_bytes(dev_xy[32:], "big"),
            ec.SECP256R1(),
        ).public_key(default_backend())
        ecdh_secret = priv_key.exchange(ec.ECDH(), dev_pub)

        conf_inputs = invite_params + cap[1:] + start_params + our_xy + dev_xy
        conf_salt = s1(conf_inputs)
        conf_key = k1(ecdh_secret, conf_salt, b"prck")
        auth_value = b"\x00" * 16
        our_random = secrets.token_bytes(16)
        our_conf = aes_cmac(conf_key, our_random + auth_value)

        await self._send(client, bytes([CONFIRMATION]) + our_conf)
        dev_conf_raw = await self._recv()
        self._check(dev_conf_raw, CONFIRMATION)
        dev_conf = dev_conf_raw[1:17]

        await self._send(client, bytes([RANDOM]) + our_random)
        dev_rand_raw = await self._recv()
        self._check(dev_rand_raw, RANDOM)
        dev_random = dev_rand_raw[1:17]

        if aes_cmac(conf_key, dev_random + auth_value) != dev_conf:
            raise RuntimeError("Device confirmation mismatch!")

        prov_salt = s1(conf_salt + our_random + dev_random)
        session_key = k1(ecdh_secret, prov_salt, b"prsk")
        session_nonce = k1(ecdh_secret, prov_salt, b"prsn")[3:16]
        dev_key = k1(ecdh_secret, prov_salt, b"prdk")

        prov_data = (
            self.net_key
            + struct.pack(">H", self.net_key_index)
            + bytes([self.flags])
            + struct.pack(">I", self.iv_index)
            + struct.pack(">H", self.unicast_address)
        )
        encrypted = _aes_ccm_encrypt(session_key, session_nonce, prov_data)
        await self._send(client, bytes([DATA]) + encrypted)

        done = await self._recv()
        self._check(done, COMPLETE)
        try:
            await client.stop_notify(PROV_DATA_OUT)
        except Exception:
            pass

        result = {
            "net_key": self.net_key.hex(),
            "app_key": FEASYCOM_APP_KEY,
            "dev_key": dev_key.hex(),
            "net_key_index": self.net_key_index,
            "iv_index": self.iv_index,
            "unicast_address": self.unicast_address,
            "device_address": addr,
            "element_count": num_elements,
            "seq": 1,
        }
        print(f"\nProvisioned successfully!")
        print(f"  Unicast: 0x{self.unicast_address:04x}")
        print(f"  Elements: {num_elements}")
        return result


async def configure_node(keys: dict):
    """Send Config AppKey Add and Config Model App Bind to the newly provisioned node.

    These two Config messages (encrypted with dev_key) are required after
    provisioning so the device learns the application key and binds it to the
    vendor model.  Without them every vendor model message is silently dropped.
    """
    net_key = bytes.fromhex(keys["net_key"])
    app_key = bytes.fromhex(keys["app_key"])
    dev_key = bytes.fromhex(keys["dev_key"])
    unicast = keys["unicast_address"]
    iv_index = keys["iv_index"]

    print("\nWaiting for device to switch to proxy mode...")
    await asyncio.sleep(4.0)

    print(f"Scanning for {DEVICE_NAME} (proxy mode)...")
    target_device = await BleakScanner.find_device_by_filter(
        lambda d, adv: is_light(d, adv),
        timeout=12.0,
    )
    if target_device is None:
        print("Device not found in proxy mode — skipping configuration.")
        return

    client = MeshProxyClient(net_key, app_key, dev_key, iv_index, src_addr=0x0001)
    client.crypto._seq = next_seq(keys)
    await client.connect(target_device)
    await asyncio.sleep(0.5)

    # Config AppKey Add (opcode 0x00, encrypted with dev_key)
    # Payload: packed 12-bit NetKeyIndex=0 + 12-bit AppKeyIndex=0 (3 bytes) + AppKey (16 bytes)
    config_app_key_add = bytes([0x00, 0x00, 0x00, 0x00]) + app_key  # opcode + 3-byte indices + key
    print("Sending Config AppKey Add...")
    await client.send_mesh_message(unicast, config_app_key_add, use_dev_key=True)
    try:
        resp = await asyncio.wait_for(client._rx_queue.get(), timeout=3.0)
        status = resp["access_pdu"][2] if len(resp.get("access_pdu", b"")) > 2 else -1
        print(f"Config AppKey Status: {status:#04x} ({'OK' if status == 0 else 'error'})")
    except asyncio.TimeoutError:
        print("Config AppKey Add: no response (may still have worked)")

    await asyncio.sleep(0.5)

    # Config Model App Bind (opcode 0x803D, encrypted with dev_key)
    # Payload: ElementAddress (2B LE) + AppKeyIndex (2B LE) + CompanyID (2B LE) + ModelID (2B LE)
    config_model_app_bind = (
        bytes([0x80, 0x3D])
        + struct.pack("<H", unicast)   # element address
        + struct.pack("<H", 0)         # AppKeyIndex = 0
        + struct.pack("<HH", 0x1111, 0x1111)  # vendor model: CompanyID + ModelID
    )
    print("Sending Config Model App Bind...")
    await client.send_mesh_message(unicast, config_model_app_bind, use_dev_key=True)
    try:
        resp = await asyncio.wait_for(client._rx_queue.get(), timeout=3.0)
        status = resp["access_pdu"][2] if len(resp.get("access_pdu", b"")) > 2 else -1
        print(f"Config Model App Status: {status:#04x} ({'OK' if status == 0 else 'error'})")
    except asyncio.TimeoutError:
        print("Config Model App Bind: no response (may still have worked)")

    keys["seq"] = client.crypto._seq
    save_keys(keys)
    try:
        await client.disconnect()
    except Exception:
        pass
    print("Configuration complete.")


async def main():
    """Scan for an unprovisioned FS-300B and provision it."""
    print(f"Scanning for {DEVICE_NAME}...")
    found_event = asyncio.Event()
    target_device = None

    def on_detect(device, _adv_data):
        if not is_light(device, _adv_data, "1827"):
            return
        nonlocal target_device
        if not found_event.is_set():
            target_device = device
            found_event.set()

    scanner = BleakScanner(detection_callback=on_detect)
    await scanner.start()
    try:
        await asyncio.wait_for(found_event.wait(), timeout=10.0)
    except asyncio.TimeoutError:
        await scanner.stop()
        print("Device not found. Is the light in pairing mode?")
        return

    prov = MeshProvisioner()
    result = await prov.provision(target_device)
    await scanner.stop()

    save_keys(result)
    print(f"Keys saved to {KEYS_FILE}")
    await configure_node(result)


if __name__ == "__main__":
    asyncio.run(main())
