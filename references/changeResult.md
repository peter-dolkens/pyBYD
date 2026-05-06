# Smart-Charging Result Poll — `/control/smartCharge/changeResult`

Captured request/response sent shortly after `/control/smartCharge/changeChargeStatue`, on the BYD overseas (AU) DiLink API.
Sensitive values (user IDs, device fingerprints, signatures, ciphertexts, timestamps, VIN, requestSerial) have been replaced with `<…>` placeholders.

This endpoint is **not currently implemented in pyBYD** ([src/pybyd/_api/smart_charging.py](src/pybyd/_api/smart_charging.py) only sends the toggle, never polls for its result). It follows the same `/<command> → /<command>Result` pattern as `/control/remoteControl` + `/control/remoteControlResult` ([src/pybyd/_api/control.py:394-409](src/pybyd/_api/control.py#L394-L409)) and `/vehicleInfo/.../vehicleRealTime{,Result}` ([src/pybyd/_api/realtime.py](src/pybyd/_api/realtime.py)).

In the capture this poll fires **~1.24 s after** the `changeChargeStatue` request (`reqTimestamp` 1777557674040 → 1777557675281), consistent with a one-shot result poll rather than long-running polling.

## Wire format

Identical envelope structure to `changeChargeStatue` — see [changeChargeStatue.md](changeChargeStatue.md#wire-format) for the full Bangcle + AES-CBC layering and key derivation. Only the inner payload and the response body differ.

---

## Request

```http
POST https://dilinkappoversea-au.byd.auto/control/smartCharge/changeResult HTTP/2.0
accept-encoding: identity
content-type: application/json; charset=UTF-8
user-agent: okhttp/4.12.0

{"request":"F<bangcle-base64-ciphertext>"}
```

### Outer payload (after Bangcle decoding)

Same fields as the toggle — only the values of `encryData`, `sign`, `reqTimestamp`, `serviceTime`, and `checkcode` change between requests.

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

### Inner payload (under `encryData`, after AES-CBC decryption)

Verified against a live decryption. Inner JSON keys are sorted alphabetically by the client before encryption:

```json
{
  "deviceType": "0",
  "imeiMD5": "<MD5(imei)>",
  "networkType": "wifi",
  "random": "<32 hex chars, secrets.token_hex(16).upper()>",
  "requestSerial": "<32 hex chars, copied verbatim from changeChargeStatue response>",
  "timeStamp": "<unix ms>",
  "version": "<app_inner_version, e.g. \"333\">",
  "vin": "<17-char VIN>"
}
```

`build_inner_base()` already accepts a `request_serial` argument ([src/pybyd/_api/_common.py:38-61](src/pybyd/_api/_common.py#L38-L61)) — implementing this endpoint should reuse it the same way [`_fetch_control_endpoint()`](src/pybyd/_api/control.py#L58-L70) does. The serial comes from the toggle's `respondData` ([changeChargeStatue.md](changeChargeStatue.md#response)).

In the capture, the same serial value `EAA3…7CC` appears in (a) the toggle response's `respondData` and (b) this poll's `encryData` `requestSerial` field — confirming the correlation flow.

Note that this poll **does not** include `timeZone`, while the matching `changeChargeStatue` request does. The two endpoints' inner shapes are *not* identical despite both being under `/control/smartCharge/`.

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

`code`/`message` mapping is the standard one ([src/pybyd/_api/_common.py:64-107](src/pybyd/_api/_common.py#L64-L107)).

### Inner payload (under `respondData`, after AES-CBC decryption)

Three response shapes observed, depending on whether and how the underlying change has settled.

**Pending** (returned while the change is still propagating):

```json
{
  "res": 1,
  "language": "en",
  "userId": "<user_id>"
}
```

**Terminal — success** (returned once the toggle has been applied):

```json
{
  "res": 2,
  "message": "Operation successful"
}
```

**Terminal — failure** (observed during live testing in two distinct "already in target state" scenarios):

```json
{
  "res": 3,
  "message": "Operation failure"
}
```

> ⚠ **Caveat:** the cloud's `res` does **not** reliably reflect actual vehicle state changes. During live testing a `status: "0"` (intended stop) request returned `res: 2 "Operation successful"` but the vehicle kept charging — and per [`changeChargeStatue.md`](changeChargeStatue.md#L88) the BYD app has no in-app stop-charge action at all, so the `status: "0"` flow appears to be a cloud-only no-op. So `res: 2` is at best an "accepted by cloud" signal, not a guarantee that the action took effect.
>
> `res: 3` ("Operation failure") has now been observed twice, both consistent with "vehicle already in target state":
> 1. A back-to-back duplicate `status: "1"` (start) request while a previous start was still settling.
> 2. A `status: "1"` (start) request issued while the battery was already at 100% SoC (nothing to charge).
>
> Treat `res: 3` as a soft-reject that callers should surface as a friendly "already in target state / nothing to do" rather than a hard error.

Field semantics:

| Field | Type | Notes |
|---|---|---|
| `res` | int | State code. **`1` = pending / still in progress** — keep polling. **`2` = terminal "success"** — cloud accepted the request, but see caveat above. **`3` = terminal "failure"** — observed twice, both consistent with "already in target state" (duplicate start; start while at 100% SoC). Treat any `res` other than 1/2 as failure for client-side error handling. |
| `message` | str | Only present on terminal responses (`res != 1`). Captured values are the literal strings `"Operation successful"` and `"Operation failure"`. Callers should match `res`, not `message`, since the string is locale-dependent (the inner request carries no `language`, so this string appears to come from the user's account/profile language). |
| `language` | str | Only present in pending responses. Echoes the session language. |
| `userId` | str | Only present in pending responses. Echoes the account user id. |

Notably absent: there is **no `requestSerial` echo** in either response shape, despite it being the correlation key in the request. Callers must track the serial themselves.

### Polling behaviour

In the capture this endpoint was hit **5 times** by the official client (the user originally recalled "6 in total"; the log shows 5):

| Poll # | `reqTimestamp` (ms) | Δ from toggle | `res` |
|---|---|---|---|
| toggle (`changeChargeStatue`) | 1777557674040 | — | (returns `requestSerial`) |
| 1 | 1777557675281 | +1.24 s | `1` (pending) |
| 5 | 1777557683473 | +9.43 s | `2` (terminal, `message: "Operation successful"`) |

Polls 2–4 weren't captured in the log, but the spacing implies ~2 s between attempts. All polls reuse the **same `requestSerial`** with a fresh `random` and `timeStamp` per request.

After the terminal `res: 2`, the client immediately fires `/control/smartCharge/homePage` (Δ +9.71 s from the toggle, +0.28 s after the terminal poll) to refresh the UI with the now-current smart-charging state — that is a separate endpoint and not part of this control flow.

This matches the polling pattern used by [`_execute_remote_control_with_polling()`](src/pybyd/_api/control.py#L394-L447):
- ~2 s `poll_interval` between attempts.
- A `poll_attempts` cap of around 5–6.
- A "ready" check that succeeds when `res == 2` (continue while `res == 1`).
- `res == 3` has been observed twice with `message: "Operation failure"`: once on a back-to-back duplicate start, and once on a start request while the battery was already at 100% SoC. Both observations are consistent with "vehicle already in target state". Treat as `BydRemoteControlError` but consider surfacing a friendlier message at the UI seam.
- Any other `res` value, or exhausting attempts with `res == 1`, should also be treated as failure (`BydRemoteControlError`).

Open questions to resolve with more captures:
- ~~What does `status: "0"` actually do?~~ **Answered, mostly:** the BYD app has no in-app stop-charge button, and live testing confirms `status: "0"` is a cloud-side no-op for an active charge. To programmatically interrupt a charge, capture and use the smart-charging schedule update path (`/control/smartCharge/saveOrUpdate`) — e.g. set `targetSoc` to current SoC.
- Are there vehicle-state failure causes (offline, comms error, charger fault) that produce a different `res` or message?
- ~~Confirm `res: 3` semantics — is it "already in target state", or a more general "vehicle rejected"?~~ **Two observations now, both "already in target state" (duplicate start, and 100% SoC start). Hypothesis is well supported but not exhaustively proven** — there could still be other vehicle-side rejects that also surface as `res: 3`.

---

## Implementing in pyBYD

This needs three coordinated changes:

1. **[`build_inner_base()`](src/pybyd/_api/_common.py#L38-L61)** — emit `timeZone: ""` for the toggle path. (The result poll does *not* carry `timeZone`, so the field must be optional, matching how `vin`/`requestSerial` are already optional.)
2. **[`toggle_smart_charging()`](src/pybyd/_api/smart_charging.py#L20-L52)** — rename `smartChargeSwitch` → `status` (current code is sending an unrecognised key), and capture the `requestSerial` returned in `respondData` so the caller can poll.
3. **New `get_smart_charging_result()`** in `smart_charging.py`, modelled on [`_fetch_control_endpoint()`](src/pybyd/_api/control.py#L58-L70) + [`_execute_remote_control_with_polling()`](src/pybyd/_api/control.py#L394-L447):

```python
_RESULT_ENDPOINT = "/control/smartCharge/changeResult"

async def _poll_smart_charge_result(
    config: BydConfig,
    session: Session,
    transport: Transport,
    vin: str,
    *,
    request_serial: str,
    poll_attempts: int = 6,
    poll_interval: float = 2.0,
) -> dict[str, Any]:
    """Poll /control/smartCharge/changeResult until res == 2 (terminal), or attempts exhausted."""
    last: dict[str, Any] = {}
    for attempt in range(1, poll_attempts + 1):
        if attempt > 1:
            await asyncio.sleep(poll_interval)

        inner = build_inner_base(config, vin=vin, request_serial=request_serial)
        last = await post_token_json(
            endpoint=_RESULT_ENDPOINT,
            config=config,
            session=session,
            transport=transport,
            inner=inner,
            vin=vin,
            not_supported_codes=ENDPOINT_NOT_SUPPORTED_CODES,
        ) or {}

        res = last.get("res")
        if res == 2:
            return last  # terminal — inspect last["message"] if needed
        if res != 1:  # anything other than pending → unexpected/failure
            raise BydRemoteControlError(
                f"smartCharge changeResult res={res}",
                code=str(res),
                endpoint=_RESULT_ENDPOINT,
            )
    # Exhausted attempts while still pending → treat as timeout/failure
    raise BydRemoteControlError(
        "smartCharge changeResult timed out (res stayed at 1)",
        code="timeout",
        endpoint=_RESULT_ENDPOINT,
    )
```

Then `toggle_smart_charging()` should chain the two:

```python
ack = await _toggle(...)              # returns requestSerial
result = await _poll_smart_charge_result(..., request_serial=ack.request_serial)
```

`CommandAck` ([src/pybyd/models/control.py:102-117](src/pybyd/models/control.py#L102-L117)) needs a `request_serial: str | None = Field(default=None, validation_alias="requestSerial")` field added to surface the serial to callers.
