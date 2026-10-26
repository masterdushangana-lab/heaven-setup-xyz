"""
AES-256-GCM Password Encryption for Mapping JSON
Compatible with Web Crypto API (SubtleCrypto) in Cloudflare Pages / Workers / Browser.
Uses PBKDF2-SHA256 (600,000 iterations), salt (16 bytes), nonce (12 bytes), tag (16 bytes).
"""

import os
import json
import base64
import hashlib
from typing import Dict, Any


def encrypt_mapping(data: dict, password: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt = os.urandom(16)
    nonce = os.urandom(12)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600000, 32)
    aesgcm = AESGCM(key)
    plaintext_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
    ciphertext = aesgcm.encrypt(nonce, plaintext_bytes, b"CLIXARENA-ANIME-MAPPINGS")

    return {
        "version": 1,
        "algorithm": "AES-256-GCM",
        "kdf": "PBKDF2-SHA256",
        "iterations": 600000,
        "salt": base64.b64encode(salt).decode("utf-8"),
        "nonce": base64.b64encode(nonce).decode("utf-8"),
        "ciphertext": base64.b64encode(ciphertext).decode("utf-8")
    }


def decrypt_mapping(encrypted_doc: dict, password: str) -> dict:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if not isinstance(encrypted_doc, dict) or encrypted_doc.get("algorithm") != "AES-256-GCM":
        raise ValueError("Invalid encrypted document format")

    salt = base64.b64decode(encrypted_doc["salt"])
    nonce = base64.b64decode(encrypted_doc["nonce"])
    ciphertext = base64.b64decode(encrypted_doc["ciphertext"])

    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 600000, 32)
    aesgcm = AESGCM(key)
    plaintext_bytes = aesgcm.decrypt(nonce, ciphertext, b"CLIXARENA-ANIME-MAPPINGS")
    return json.loads(plaintext_bytes.decode("utf-8"))
