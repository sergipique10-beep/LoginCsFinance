# Troubleshooting

---

## `GET /inventory` returns 502

### Symptom

`GET /inventory` always responds with `502 Bad Gateway`. The response body contains one of:

- `"Could not reach Steam: {exc}"`
- `"Steam returned {status_code}"`
- `"Unexpected response format from Steam API"`

`GET /me` returns 200 in the same session (same API key, same steamwebapi.com host).

---

### Code paths that produce 502 (`main.py` lines ~552–598)

| Condition | Detail message in response |
|-----------|---------------------------|
| `httpx.RequestError` raised | `"Could not reach Steam: {exc}"` |
| `resp.status_code != 200` | `"Steam returned {resp.status_code}"` |
| `not isinstance(data, list)` | `"Unexpected response format from Steam API"` |

> **Known gap:** none of these paths log the upstream response before raising. The status code and body returned by steamwebapi.com are silently discarded, making the 502 opaque without adding logging first (see diagnostic step below).

---

### Likely causes (ordered by probability)

1. **API plan does not include inventory access.** steamwebapi.com may return 401 or 403 for the inventory endpoint even with a valid key, if the account plan doesn't cover it. This is distinct from a user's private inventory (which produces its own 403 already handled upstream).

2. **Wrong `STEAM_GAME` value.** The variable defaults to `"cs2"` but steamwebapi.com may expect the numeric AppID `"730"` or a different slug. A malformed game parameter can cause a non-200 or unexpected-format response. Check the [configuration docs](./configuration.md).

3. **steamwebapi.com returns an error object with HTTP 200.** If the API signals an error via a JSON dict (`{"error": "..."}`) while still returning `200 OK`, the `isinstance(data, list)` guard triggers the third 502 path.

4. **Upstream 429 (rate limit) from steamwebapi.com.** Not specifically handled — falls into the generic non-200 branch. Retrying after a back-off resolves it if this is the cause.

---

### Diagnostic step

Add logging before each `raise HTTPException(status_code=502)` in `main.py` (around lines 572, 594, 596) to surface the upstream response:

```python
logger.error(
    "steamwebapi /inventory → %s: %.500s",
    resp.status_code,
    resp.text,
)
raise HTTPException(status_code=502, detail="Steam returned ...")
```

With this in place, restart the server and repeat the failing request. The logged status code and body will identify the actual cause.

---

### Narrowing the cause using `/me`

Because `GET /me` succeeds while `GET /inventory` fails:

- The API key is valid and steamwebapi.com is reachable — rules out network failures and key problems.
- The failure is specific to the inventory endpoint — points to API plan restrictions or a wrong `STEAM_GAME` parameter.

Check your steamwebapi.com account dashboard to confirm inventory access is included in your plan, then verify the `STEAM_GAME` value in `.env`.
