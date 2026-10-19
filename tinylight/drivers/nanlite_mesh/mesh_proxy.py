"""
Bluetooth Mesh Proxy client for Nanlite FS-300B.

Connects to a provisioned mesh node via GATT Proxy (0x1828) and sends
encrypted mesh messages through all three layers (network, transport, access).

The FS-300B requires a Feasycom TEA-encrypted authentication handshake on
the 0xFFF0 service before it will process vendor model messages.
"""

import asyncio
import secrets
import struct

from bleak import BleakClient

from .config import load_tea_key
from .crypto import aes_cmac, aes_ecb, aes_ccm_encrypt, aes_ccm_decrypt, k2, k4

# --- GATT UUIDs ---

PROXY_DATA_IN = "00002add-0000-1000-8000-00805f9b34fb"
PROXY_DATA_OUT = "00002ade-0000-1000-8000-00805f9b34fb"

FEASY_NOTIFY = "0000fff1-0000-1000-8000-00805f9b34fb"
FEASY_WRITE = "0000fff2-0000-1000-8000-00805f9b34fb"

# --- TEA cipher (Feasycom auth) ---

_TEA_KEY = struct.unpack(">IIII", load_tea_key())
_TEA_DELTA = 0x9E3779B9
_TEA_MASK = 0xFFFFFFFF


def _tea_encrypt(block: bytes, rounds: int = 32) -> bytes:
    v0, v1 = struct.unpack(">II", block)
    s = 0
    for _ in range(rounds):
        s = (s + _TEA_DELTA) & _TEA_MASK
        v0 = (v0 + (((v1 << 4) + _TEA_KEY[0]) ^ (v1 + s) ^ ((v1 >> 5) + _TEA_KEY[1]))) & _TEA_MASK
        v1 = (v1 + (((v0 << 4) + _TEA_KEY[2]) ^ (v0 + s) ^ ((v0 >> 5) + _TEA_KEY[3]))) & _TEA_MASK
    return struct.pack(">II", v0, v1)


# --- Mesh network layer helpers ---

def _pack_seq(seq: int) -> bytes:
    """Pack a 24-bit sequence number (big-endian)."""
    return struct.pack(">I", seq)[1:4]


def _obfuscate(priv_key: bytes, ctl_ttl_seq_src: bytes,
               iv_index: int, enc_dst_transport: bytes) -> bytes:
    pecb = aes_ecb(priv_key,
                   b"\x00" * 5 + struct.pack(">I", iv_index) + enc_dst_transport[:7])
    return bytes(a ^ b for a, b in zip(ctl_ttl_seq_src, pecb[:6]))


def _build_nonce(nonce_type: int, ctl_ttl: int, seq: int, src: int,
                 dst: int, iv_index: int) -> bytes:
    return (bytes([nonce_type, ctl_ttl]) + _pack_seq(seq)
            + struct.pack(">HHI", src, dst, iv_index))


# --- Mesh crypto engine ---

class MeshCrypto:
    """Stateful mesh message encryption using a single network/app key pair."""

    MAX_UNSEG_ACCESS = 11
    SEG_MAX_PAYLOAD = 12

    def __init__(self, net_key: bytes, app_key: bytes, dev_key: bytes,
                 iv_index: int = 0, src_addr: int = 0x0001):
        self.app_key = app_key
        self.dev_key = dev_key
        self.iv_index = iv_index
        self.src_addr = src_addr
        self._seq = 1

        self.nid, self.enc_key, self.priv_key = k2(net_key, b"\x00")
        self.aid = k4(app_key)

    def next_seq(self) -> int:
        seq = self._seq
        self._seq += 1
        return seq

    # -- Network PDU construction --

    def _encrypt_network(self, ctl: int, ttl: int, seq: int,
                         dst: int, transport_pdu: bytes) -> bytes:
        mic_len = 8 if ctl else 4
        nonce = _build_nonce(0x00, (ctl << 7) | (ttl & 0x7F),
                             seq, self.src_addr, 0x0000, self.iv_index)
        plaintext = struct.pack(">H", dst) + transport_pdu
        enc = aes_ccm_encrypt(self.enc_key, nonce, plaintext, mic_len=mic_len)

        header = bytes([(ctl << 7) | (ttl & 0x7F)]) + _pack_seq(seq) + struct.pack(">H", self.src_addr)
        obf = _obfuscate(self.priv_key, header, self.iv_index, enc)
        ivi_nid = ((self.iv_index & 1) << 7) | (self.nid & 0x7F)
        return bytes([ivi_nid]) + obf + enc

    # -- Access message construction --

    def _access_keys(self, dst: int, seq: int, use_dev_key: bool, aszmic: int = 0):
        """Return (encryption_key, nonce, akf_aid) for an access message."""
        if use_dev_key:
            nonce = _build_nonce(0x02, aszmic << 7, seq, self.src_addr, dst, self.iv_index)
            return self.dev_key, nonce, 0x00
        nonce = _build_nonce(0x01, aszmic << 7, seq, self.src_addr, dst, self.iv_index)
        return self.app_key, nonce, 0x40 | (self.aid & 0x3F)

    def build_access_message(self, dst: int, access_payload: bytes,
                             use_dev_key: bool = False, ttl: int = 4) -> list[bytes]:
        """Build network PDUs for an access message (auto-segments if needed)."""
        if len(access_payload) <= self.MAX_UNSEG_ACCESS:
            return [self._build_unsegmented(dst, access_payload, use_dev_key, ttl)]
        return self._build_segmented(dst, access_payload, use_dev_key, ttl)

    def _build_unsegmented(self, dst: int, payload: bytes,
                           use_dev_key: bool, ttl: int) -> bytes:
        seq = self.next_seq()
        key, nonce, akf_aid = self._access_keys(dst, seq, use_dev_key)
        enc = aes_ccm_encrypt(key, nonce, payload, mic_len=4)
        lower = bytes([akf_aid & 0x7F]) + enc
        return self._encrypt_network(0, ttl, seq, dst, lower)

    def _build_segmented(self, dst: int, payload: bytes,
                         use_dev_key: bool, ttl: int) -> list[bytes]:
        seq0 = self.next_seq()
        seq_zero = seq0 & 0x1FFF
        key, nonce, akf_aid = self._access_keys(dst, seq0, use_dev_key)
        upper = aes_ccm_encrypt(key, nonce, payload, mic_len=4)

        seg_n = (len(upper) - 1) // self.SEG_MAX_PAYLOAD
        pdus = []
        for seg_o in range(seg_n + 1):
            chunk = upper[seg_o * self.SEG_MAX_PAYLOAD:(seg_o + 1) * self.SEG_MAX_PAYLOAD]
            hdr = bytes([0x80 | (akf_aid & 0x7F)])
            hdr += struct.pack(">I", (seq_zero << 10) | (seg_o << 5) | seg_n)[1:4]
            seq = seq0 if seg_o == 0 else self.next_seq()
            pdus.append(self._encrypt_network(0, ttl, seq, dst, hdr + chunk))
        return pdus

    # -- Network PDU decryption --

    def decrypt_network_pdu(self, pdu: bytes) -> dict | None:
        """Decrypt a received network PDU. Returns parsed fields or None on failure."""
        if len(pdu) < 14:
            return None
        ivi_nid = pdu[0]
        if (ivi_nid & 0x7F) != self.nid:
            return None

        obfuscated = pdu[1:7]
        enc_payload = pdu[7:]

        pecb = aes_ecb(self.priv_key,
                       b"\x00" * 5 + struct.pack(">I", self.iv_index) + enc_payload[:7])
        deobf = bytes(a ^ b for a, b in zip(obfuscated, pecb[:6]))

        ctl = (deobf[0] >> 7) & 1
        ttl = deobf[0] & 0x7F
        seq = int.from_bytes(deobf[1:4], "big")
        src = int.from_bytes(deobf[4:6], "big")
        mic_len = 8 if ctl else 4

        nonce = _build_nonce(0x00, deobf[0], seq, src, 0x0000, self.iv_index)
        plaintext = aes_ccm_decrypt(self.enc_key, nonce, enc_payload, mic_len=mic_len)
        if plaintext is None:
            return None

        dst = int.from_bytes(plaintext[:2], "big")
        transport_pdu = plaintext[2:]

        result = {"ctl": ctl, "ttl": ttl, "seq": seq, "src": src,
                  "dst": dst, "transport_pdu": transport_pdu}

        # Try to decrypt upper transport (unsegmented access message)
        if not ctl and len(transport_pdu) >= 5:
            seg = (transport_pdu[0] >> 7) & 1
            if not seg:
                akf = (transport_pdu[0] >> 6) & 1
                key = self.app_key if akf else self.dev_key
                nonce_type = 0x01 if akf else 0x02
                nonce = _build_nonce(nonce_type, 0x00, seq, src, dst, self.iv_index)
                access = aes_ccm_decrypt(key, nonce, transport_pdu[1:], mic_len=4)
                if access is not None:
                    result["access_pdu"] = access
        return result

    # -- Proxy PDU wrapping --

    def build_proxy_pdu(self, network_pdu: bytes, pdu_type: int = 0x00) -> bytes:
        return bytes([pdu_type & 0x3F]) + network_pdu

    def build_proxy_config_message(self, opcode: int, params: bytes = b"") -> bytes:
        """Build a Proxy Configuration message (proxy nonce type 0x03)."""
        seq = self.next_seq()
        dst = 0x0000
        transport_pdu = bytes([opcode]) + params

        nonce = _build_nonce(0x03, 0x00, seq, self.src_addr, 0x0000, self.iv_index)
        plaintext = struct.pack(">H", dst) + transport_pdu
        enc = aes_ccm_encrypt(self.enc_key, nonce, plaintext, mic_len=8)

        header = bytes([0x80]) + _pack_seq(seq) + struct.pack(">H", self.src_addr)
        obf = _obfuscate(self.priv_key, header, self.iv_index, enc)
        ivi_nid = ((self.iv_index & 1) << 7) | (self.nid & 0x7F)
        net_pdu = bytes([ivi_nid]) + obf + enc
        return self.build_proxy_pdu(net_pdu, pdu_type=0x02)


# --- High-level proxy client ---

class MeshProxyClient:
    """Send mesh messages through a GATT Proxy connection."""

    def __init__(self, net_key: bytes, app_key: bytes, dev_key: bytes,
                 iv_index: int = 0, src_addr: int = 0x0001):
        self.crypto = MeshCrypto(net_key, app_key, dev_key, iv_index, src_addr)
        self._client: BleakClient | None = None
        self._feasy_queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._filter_ready: asyncio.Event = asyncio.Event()
        self._rx_queue: asyncio.Queue[dict] = asyncio.Queue()

    # -- Notification handlers --

    def _on_feasy_notify(self, _sender, data: bytearray):
        self._feasy_queue.put_nowait(bytes(data))

    def _on_proxy_notify(self, _sender, data: bytearray):
        pdu_type = data[0] & 0x3F
        if pdu_type == 0x02:  # Proxy Configuration (filter status response)
            self._filter_ready.set()
        elif pdu_type == 0x00:  # Network PDU — try to decrypt
            parsed = self.crypto.decrypt_network_pdu(bytes(data[1:]))
            if parsed and "access_pdu" in parsed:
                self._rx_queue.put_nowait(parsed)

    async def _feasycom_auth(self, fetch_iv: bool = False) -> int | None:
        """Authenticate with Feasycom 0xFFF0 service.

        Args:
            fetch_iv: If True, also send $FSCCMD002$ to read the device's
                      current IV index and return it.  Leave False for normal
                      commands to keep auth latency low.

        Returns the device's current IV index when fetch_iv=True, else None.
        """
        try:
            await self._client.stop_notify(FEASY_NOTIFY)
        except Exception:
            pass
        await self._client.start_notify(FEASY_NOTIFY, self._on_feasy_notify)
        await asyncio.sleep(0.2)

        challenge = secrets.token_bytes(4) + b"\x00\x00\x00\x00"
        for rounds in (32, 2, 1):
            encrypted = _tea_encrypt(challenge, rounds)
            packet = b"AUTH" + encrypted + encrypted
            await self._client.write_gatt_char(FEASY_WRITE, packet, response=False)
            try:
                await asyncio.wait_for(self._feasy_queue.get(), timeout=2.0)
                break
            except asyncio.TimeoutError:
                continue

        # $FSCCMD001$ → "$001-XXXX$" where XXXX is the device's mesh address (hex)
        await self._client.write_gatt_char(FEASY_WRITE, b"$FSCCMD001$\r\n", response=False)
        try:
            resp = await asyncio.wait_for(self._feasy_queue.get(), timeout=2.0)
            resp_str = resp.decode("ascii", errors="replace")
            if "$001" in resp_str:
                addr = resp_str.split("-")[1].rstrip("$\r\n")
                print(f"Device mesh address: 0x{addr}")
        except asyncio.TimeoutError:
            pass

        if not fetch_iv:
            return None

        # $FSCCMD002$ → "$002-XHHHHHHHH$" where X=ivUpdateActive, HHHHHHHH=ivIndex (hex)
        # Only sent when explicitly requested (e.g. `cli.py query`) to avoid adding
        # up to 2s of latency to every normal command connection.
        await asyncio.sleep(0.1)
        await self._client.write_gatt_char(FEASY_WRITE, b"$FSCCMD002$\r\n", response=False)
        try:
            resp2 = await asyncio.wait_for(self._feasy_queue.get(), timeout=2.0)
            resp2_str = resp2.decode("ascii", errors="replace")
            if "$002" in resp2_str:
                # Format: "$002-XHHHHHHHH$\r\n" (positions 6-13 are 8 hex digits)
                iv_hex = resp2_str[6:14]
                iv_index = int(iv_hex, 16)
                return iv_index
        except (asyncio.TimeoutError, ValueError):
            pass
        return None

    # -- Connection lifecycle --

    async def connect(self, device, do_auth: bool = True, fetch_iv: bool = False):
        """Connect to a mesh proxy node.

        Args:
            device: A BLEDevice object (preferred) or address string.
        """
        for attempt in range(3):
            try:
                self._client = BleakClient(device, timeout=15.0)
                await self._client.connect()
                break
            except Exception:
                if attempt == 2:
                    raise
                await asyncio.sleep(1.0)
        addr = device.address if hasattr(device, "address") else device
        print(f"Connected to {addr}")

        if do_auth:
            try:
                device_iv = await self._feasycom_auth(fetch_iv=fetch_iv)
                if device_iv is not None and device_iv != self.crypto.iv_index:
                    print(f"IV index updated: {self.crypto.iv_index:#010x} → {device_iv:#010x}")
                    self.crypto.iv_index = device_iv
            except Exception as e:
                print(f"Feasycom auth warning: {e}")

        # Initial subscription with a no-op so BlueZ registers the notification
        # channel during service setup (required on some platforms, e.g. Pi).
        await self._client.start_notify(PROXY_DATA_OUT, lambda _s, _d: None)
        await asyncio.sleep(0.3)
        await self._setup_proxy_filter()
        await asyncio.sleep(0.3)

        # Re-subscribe with the real callback and re-send the filter.
        # This two-pass approach ensures notifications are delivered reliably.
        self._filter_ready = asyncio.Event()
        await self._client.stop_notify(PROXY_DATA_OUT)
        await asyncio.sleep(0.2)
        await self._client.start_notify(PROXY_DATA_OUT, self._on_proxy_notify)
        await asyncio.sleep(0.5)
        await self._setup_proxy_filter()
        try:
            await asyncio.wait_for(self._filter_ready.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            print("WARNING: No filter confirmation (proxy may not forward messages)")
        else:
            print("Proxy filter confirmed.")
        await asyncio.sleep(1.0)

    async def disconnect(self):
        if self._client and self._client.is_connected:
            for char in (FEASY_NOTIFY, PROXY_DATA_OUT):
                try:
                    await self._client.stop_notify(char)
                except Exception:
                    pass
            try:
                await self._client.disconnect()
            except EOFError:
                pass

    async def _write_proxy(self, data: bytes):
        await self._client.write_gatt_char(PROXY_DATA_IN, data, response=False)

    async def _setup_proxy_filter(self):
        """Configure an accept-list proxy filter with our source address."""
        await self._write_proxy(self.crypto.build_proxy_config_message(0x00, b"\x00"))
        await asyncio.sleep(0.1)
        addr_bytes = struct.pack(">H", self.crypto.src_addr)
        await self._write_proxy(self.crypto.build_proxy_config_message(0x01, addr_bytes))
        await asyncio.sleep(0.1)

    # -- Message sending --

    async def send_mesh_message(self, dst: int, access_payload: bytes,
                                use_dev_key: bool = False, ttl: int = 4):
        pdus = self.crypto.build_access_message(dst, access_payload, use_dev_key, ttl)
        for i, net_pdu in enumerate(pdus):
            await self._write_proxy(self.crypto.build_proxy_pdu(net_pdu))
            if i < len(pdus) - 1:
                await asyncio.sleep(0.02)

    async def send_vendor_message(self, dst: int, opcode: int,
                                  company_id: int, parameters: bytes):
        access = bytes([opcode, company_id & 0xFF, (company_id >> 8) & 0xFF]) + parameters
        await self.send_mesh_message(dst, access)

    async def query_vendor_message(self, dst: int, opcode: int,
                                   company_id: int, parameters: bytes,
                                   timeout: float = 3.0) -> dict | None:
        """Send a vendor message and wait for a decrypted response."""
        while not self._rx_queue.empty():
            self._rx_queue.get_nowait()
        await self.send_vendor_message(dst, opcode, company_id, parameters)
        try:
            return await asyncio.wait_for(self._rx_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
