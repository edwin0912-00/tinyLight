"""Mesh crypto primitives and key derivation (Mesh Profile 3.8.2)."""

from cryptography.hazmat.primitives.ciphers import Cipher, modes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.ciphers.algorithms import AES
from cryptography.hazmat.primitives.cmac import CMAC
from cryptography.hazmat.backends import default_backend


def aes_cmac(key: bytes, msg: bytes) -> bytes:
    c = CMAC(AES(key), backend=default_backend())
    c.update(msg)
    return c.finalize()


def aes_ecb(key: bytes, data: bytes) -> bytes:
    enc = Cipher(AES(key), modes.ECB(), backend=default_backend()).encryptor()
    return enc.update(data) + enc.finalize()


def aes_ccm_encrypt(key: bytes, nonce: bytes, plaintext: bytes,
                    aad: bytes = b"", mic_len: int = 4) -> bytes:
    return AESCCM(key, tag_length=mic_len).encrypt(nonce, plaintext, aad or None)


def aes_ccm_decrypt(key: bytes, nonce: bytes, ciphertext_with_mic: bytes,
                    aad: bytes = b"", mic_len: int = 4) -> bytes | None:
    """Decrypt and verify AES-CCM. Returns plaintext or None on auth failure."""
    try:
        return AESCCM(key, tag_length=mic_len).decrypt(nonce, ciphertext_with_mic, aad or None)
    except Exception:
        return None


def s1(m: bytes) -> bytes:
    return aes_cmac(b"\x00" * 16, m)


def k1(n: bytes, salt: bytes, p: bytes) -> bytes:
    return aes_cmac(aes_cmac(salt, n), p)


def k2(n: bytes, p: bytes) -> tuple[int, bytes, bytes]:
    """Derive NID, EncryptionKey, PrivacyKey from a network key."""
    t = aes_cmac(s1(b"smk2"), n)
    t1 = aes_cmac(t, p + b"\x01")
    t2 = aes_cmac(t, t1 + p + b"\x02")
    t3 = aes_cmac(t, t2 + p + b"\x03")
    result = bytearray((t1 + t2 + t3)[-33:])
    result[0] &= 0x7F
    return result[0], bytes(result[1:17]), bytes(result[17:33])


def k3(n: bytes) -> bytes:
    """Derive NetworkID (8 bytes) from a network key."""
    return aes_cmac(aes_cmac(s1(b"smk3"), n), b"id64\x01")[8:16]


def k4(n: bytes) -> int:
    """Derive AID (6 bits) from an application key."""
    return aes_cmac(aes_cmac(s1(b"smk4"), n), b"id6\x01")[15] & 0x3F
