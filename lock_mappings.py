"""
Script to encrypt clean JSON mappings into uploaded_episodes.json
Usage:
    python lock_mappings.py <password> [input_json] [output_file]
"""
import sys
import os
import json
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

AES_SALT_LEN = 16
AES_NONCE_LEN = 12

def derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100_000
    )
    return kdf.derive(password.encode())

def encrypt_history(input_path: str, output_path: str, password: str):
    with open(input_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    raw_data = json.dumps(data, indent=2).encode("utf-8")
    salt = os.urandom(AES_SALT_LEN)
    nonce = os.urandom(AES_NONCE_LEN)
    key = derive_key(password, salt)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, raw_data, None)
    
    with open(output_path, "wb") as f:
        f.write(salt + nonce + ciphertext)
    print(f"[SUCCESS] Encrypted '{input_path}' -> '{output_path}' with AES-256-GCM!")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python lock_mappings.py <PASSWORD> [clean_json_path] [output_bin_path]")
        sys.exit(1)
    
    pwd = sys.argv[1]
    in_json = sys.argv[2] if len(sys.argv) > 2 else r"d:\Downloads\unlocked_anime_mappings_clean.json"
    out_bin = sys.argv[3] if len(sys.argv) > 3 else r"C:\Users\wwwmd\Desktop\Workspace\clixarena\heaven-setup\uploaded_episodes.json"
    
    encrypt_history(in_json, out_bin, pwd)
