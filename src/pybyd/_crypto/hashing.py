"""Hash functions for BYD API signing and verification."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def md5_hex(value: str) -> str:
    """Compute MD5 of a UTF-8 string, returning uppercase hex.

    Mirrors JS: crypto.createHash('md5').update(value, 'utf8').digest('hex').toUpperCase()

    Parameters
    ----------
    value : str
        The string to hash.

    Returns
    -------
    str
        32-character uppercase hex digest.
    """
    return hashlib.md5(value.encode("utf-8")).hexdigest().upper()


def pwd_login_key(password: str) -> str:
    """Derive the login AES key from a plaintext password.

    Mirrors JS: md5Hex(md5Hex(password))

    Parameters
    ----------
    password : str
        The plaintext password.

    Returns
    -------
    str
        32-character uppercase hex digest.
    """
    return md5_hex(md5_hex(password))


def sha1_mixed(value: str) -> str:
    """Compute SHA1 with alternating-case hex and zero filtering.

    Algorithm (from client.js lines 58-76):
      1. SHA1 digest of UTF-8 encoded value -> 20 bytes
      2. For each byte at index *i*, format as 2-char hex:
         - Even *i*: uppercase
         - Odd *i*: lowercase
      3. Concatenate into a 40-char string
      4. Filter: drop any '0' character that falls at an even position

    Parameters
    ----------
    value : str
        The string to hash.

    Returns
    -------
    str
        Filtered mixed-case hex string (length <= 40).
    """
    digest = hashlib.sha1(value.encode("utf-8")).digest()

    mixed_chars: list[str] = []
    for i, byte_val in enumerate(digest):
        hex_str = f"{byte_val:02x}"
        if i % 2 == 0:
            mixed_chars.append(hex_str.upper())
        else:
            mixed_chars.append(hex_str.lower())
    mixed = "".join(mixed_chars)

    filtered: list[str] = []
    for j, ch in enumerate(mixed):
        if ch == "0" and j % 2 == 0:
            continue
        filtered.append(ch)
    return "".join(filtered)


def compute_checkcode(payload: dict[str, Any]) -> str:
    """Compute checkcode: MD5 of compact JSON with chunk reordering.

    The MD5 hex digest is reordered as:
      [24:32] + [8:16] + [16:24] + [0:8]

    Parameters
    ----------
    payload : dict
        The outer payload dict. Key insertion order is preserved
        and must match the JS client's order for identical output.

    Returns
    -------
    str
        32-character reordered hex checkcode.
    """
    json_str = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    md5 = hashlib.md5(json_str.encode("utf-8")).hexdigest()
    return md5[24:32] + md5[8:16] + md5[16:24] + md5[0:8]
