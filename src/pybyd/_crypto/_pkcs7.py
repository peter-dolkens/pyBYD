"""PKCS#7 padding for the Bangcle white-box AES layer."""

from __future__ import annotations


def add_pkcs7(data: bytes, block_size: int = 16) -> bytes:
    """Add PKCS#7 padding.

    If *data* length is already a multiple of *block_size*, a full block
    of padding is appended (per the PKCS#7 spec).

    Parameters
    ----------
    data : bytes
        Data to pad.
    block_size : int
        Block size in bytes (default 16).

    Returns
    -------
    bytes
        Padded data whose length is a multiple of *block_size*.
    """
    remainder = len(data) % block_size
    pad_len = block_size if remainder == 0 else block_size - remainder
    return data + bytes([pad_len] * pad_len)


def strip_pkcs7(data: bytes) -> bytes:
    """Strip PKCS#7 padding, returning data as-is if padding is invalid.

    Parameters
    ----------
    data : bytes
        Potentially padded data.

    Returns
    -------
    bytes
        Unpadded data, or the original *data* if padding is invalid.
    """
    if not data:
        return data
    pad = data[-1]
    if pad == 0 or pad > 16:
        return data
    if len(data) < pad:
        return data
    if all(b == pad for b in data[-pad:]):
        return data[:-pad]
    return data
