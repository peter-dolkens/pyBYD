"""Request signing for BYD API."""

from __future__ import annotations


def build_sign_string(fields: dict[str, str], password: str) -> str:
    """Build the sign string by sorting fields and appending password.

    Algorithm (from client.js lines 78-82):
      1. Sort field keys alphabetically
      2. Join as ``key=value`` pairs with ``&``
      3. Append ``&password=<password>``

    Parameters
    ----------
    fields : dict[str, str]
        The fields to include in the signature.
    password : str
        The password (or derived key) to append.

    Returns
    -------
    str
        The concatenated sign string.
    """
    keys = sorted(fields.keys())
    joined = "&".join(f"{key}={'null' if fields[key] is None else fields[key]}" for key in keys)
    return f"{joined}&password={password}"
