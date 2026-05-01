# Toggle Smart Charging — `/control/smartCharge/changeChargeStatue`

Captured request/response for toggling the smart-charging switch via the BYD overseas (AU) DiLink API.
Sensitive values (user IDs, device fingerprints, signatures, ciphertexts, timestamps, VIN) have been replaced with `<…>` placeholders.

## Wire format

The transport layer uses two nested envelopes:

1. **Outer ("Bangcle") envelope** — JSON `{"request": "F<base64>"}`. The `F`-prefixed string is white-box AES-CBC ciphertext (PKCS7, zero IV) decoded against the static `bangcle_tables.bin` table set. Decoding produces the **outer payload** below.
2. **Inner AES envelope** — the `encryData` (request) / `respondData` (response) hex strings inside the outer payload are AES-128-CBC + PKCS7 + zero IV, keyed by `MD5(session.encry_token)` (see [src/pybyd/_crypto/aes.py](src/pybyd/_crypto/aes.py) and [src/pybyd/session.py:44-58](src/pybyd/session.py#L44-L58)). This per-session key is not recoverable from a packet capture alone.

Reference implementation: [src/pybyd/_transport.py](src/pybyd/_transport.py), [src/pybyd/_api/_envelope.py](src/pybyd/_api/_envelope.py), [src/pybyd/_api/smart_charging.py](src/pybyd/_api/smart_charging.py).

---

## Request

```http
POST https://dilinkappoversea-au.byd.auto/control/smartCharge/changeChargeStatue HTTP/2.0
accept-encoding: identity
content-type: application/json; charset=UTF-8
user-agent: okhttp/4.12.0

{"request":"F<bangcle-base64-ciphertext>"}
```

### Outer payload (after Bangcle decoding)

```json
{
  "appName": "",
  "countryCode": "AU",
  "encryData": "<aes128-cbc-hex of inner JSON, key=MD5(encry_token)>",
  "identifier": "<user_id>",
  "imeiMD5": "<MD5(imei)>",
  "language": "en",
  "reqTimestamp": "<unix ms>",
  "sign": "<sha1Mixed of build_sign_string(inner ∪ envelope fields, sign_key)>",
  "userType": "1",
  "ostype": "and",
  "imei": "BANGCLE01234",
  "mac": "00:00:00:00:00:00",
  "model": "Redmi Note 9S",
  "sdk": "30",
  "serviceTime": "<unix ms>",
  "mod": "Xiaomi",
  "checkcode": "<md5 over outer fields>"
}
```

Field notes (from [src/pybyd/_api/_envelope.py:53-82](src/pybyd/_api/_envelope.py#L53-L82) and [src/pybyd/config.py:51-60](src/pybyd/config.py#L51-L60)):

| Field | Source | Notes |
|---|---|---|
| `appName` | constant `""` | Always empty in this build. |
| `countryCode` | `BydConfig.country_code` | `"AU"` here; `"NL"` is the package default. |
| `encryData` | AES-128-CBC of inner JSON | Hex-encoded, uppercase. Key = `MD5(session.encry_token)`. |
| `identifier` | `Session.user_id` | Returned from login; user-identifying. |
| `imeiMD5` | `MD5(device.imei)` | Stable per install; identifying. |
| `language` | `BydConfig.language` | `"en"`. |
| `reqTimestamp` | `int(time.time()*1000)` | Used in signature. |
| `sign` | `sha1_mixed(build_sign_string(...))` | HMAC-style signature over inner ∪ outer fields, keyed by `MD5(sign_token)`. |
| `userType` | `"1"` | Hardcoded for token-auth requests. |
| `ostype` | `BydDevice.ostype` | `"and"` (Android). |
| `imei`, `mac`, `model`, `sdk`, `mod` | `BydDevice.*` | Spoofed device fingerprint; defaults shipped in [config.py](src/pybyd/config.py). The captured values match the package defaults except `model="Redmi Note 9S"`/`sdk="30"`, which are user overrides. |
| `serviceTime` | `int(time.time()*1000)` | Distinct from `reqTimestamp`; not in signature. |
| `checkcode` | `compute_checkcode(outer)` | MD5 over the outer fields; integrity check. |

### Inner payload (under `encryData`, after AES-CBC decryption)

Verified against a live decryption. Inner JSON keys are sorted alphabetically by the client before encryption:

```json
{
  "deviceType": "0",
  "imeiMD5": "<MD5(imei)>",
  "networkType": "wifi",
  "random": "<32 hex chars, secrets.token_hex(16).upper()>",
  "status": "1",
  "timeStamp": "<unix ms>",
  "timeZone": "",
  "version": "<app_inner_version, e.g. \"333\">",
  "vin": "<17-char VIN>"
}
```

`status` is `"1"` to start charging, `"0"` to stop. In the captured flow this triggers charging **directly** on the vehicle (followed by [`/control/smartCharge/changeResult`](changeResult.md) polling).

> Note: this endpoint appears to accept multiple inner-payload variants. The captured request uses `status` to drive **direct charging control**. The current pyBYD implementation at [src/pybyd/_api/smart_charging.py:41](src/pybyd/_api/smart_charging.py#L41) sends `smartChargeSwitch` instead — that is likely a separate variant for toggling the **smart-charging schedule** (i.e. enabling/disabling the configured schedule rather than starting/stopping a charge), and not interchangeable with the `status` payload documented here. Both variants reuse the standard `build_inner_base()` fields plus `timeZone: ""`; the differentiating field is the variant-specific key (`status` vs `smartChargeSwitch`). A separate capture of the smart-schedule toggle would let us document both shapes definitively.

---

## Response

```http
HTTP/2.0 200
date: <RFC1123>
content-type: application/json
vary: Origin, Access-Control-Request-Method, Access-Control-Request-Headers

{"response":"F<bangcle-base64-ciphertext>"}
```

### Outer payload (after Bangcle decoding)

```json
{
  "identifier": "<user_id>",
  "respondData": "<aes128-cbc-hex of inner JSON, key=MD5(encry_token)>",
  "code": "0",
  "message": "SUCCESS"
}
```

Non-zero `code` triggers exception mapping in [src/pybyd/_api/_common.py:64-107](src/pybyd/_api/_common.py#L64-L107):
- `"1001"` → endpoint not supported for this VIN.
- Codes in `SESSION_EXPIRED_CODES` ([src/pybyd/_constants.py](src/pybyd/_constants.py)) → re-login required.

### Inner payload (under `respondData`, after AES-CBC decryption)

Verified against a live decryption:

```json
{
  "requestSerial": "<32 hex chars, server-generated correlation token>"
}
```

That `requestSerial` is the **only** correlation key for the follow-up [`/control/smartCharge/changeResult`](changeResult.md) poll — clients must capture it from this response and feed it into the next request's inner payload. The current `CommandAck` model ([src/pybyd/models/control.py:102-117](src/pybyd/models/control.py#L102-L117)) drops this field on the floor; a `request_serial` attribute should be added (with `validation_alias="requestSerial"`) for callers that want to poll.

---

## Reproducing the decode

```python
import json, sys
sys.path.insert(0, "src")
from pybyd._crypto.bangcle import BangcleCodec

codec = BangcleCodec()

def decode_outer(env: str) -> dict:
    text = codec.decode_envelope(env).decode("utf-8")
    if text.startswith(("F{", "F[")):
        text = text[1:]
    return json.loads(text)

# With an active session, decrypt the inner layer too:
from pybyd._crypto.aes import aes_decrypt_utf8
inner_plain = aes_decrypt_utf8(outer["encryData"], session.content_key())
print(json.loads(inner_plain))
```

Without a captured `encry_token`, the inner `encryData`/`respondData` cannot be recovered from the packet alone — that key is established at login and never leaves the client.
