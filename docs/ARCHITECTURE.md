# PlayZ TV Auto-Updater — Technical Docs

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                   GitHub Actions (hourly)                    │
│                                                              │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐  │
│  │  Checkout    │ →  │ Setup Python │ →  │ pip install  │  │
│  └──────────────┘    └──────────────┘    └──────────────┘  │
│                                                ↓            │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  playztv_auto_update.py                              │   │
│  │                                                       │   │
│  │  1. Fetch Firebase Remote Config → api_url           │   │
│  │  2. Decrypt app.txt (AES Key2/IV2)                   │   │
│  │  3. Decrypt events.txt (char-sub + 2×b64 + AES K1)   │   │
│  │  4. For each event: fetch + decrypt stream file      │   │
│  │  5. Decrypt categories.txt + sports.txt              │   │
│  │  6. Verify each stream (parallel, 8 workers)         │   │
│  │  7. Generate M3U variants + status.json              │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                ↓            │
│  ┌──────────────┐    ┌──────────────┐                     │
│  │  git add     │ →  │  git commit  │ → push to main      │
│  └──────────────┘    └──────────────┘                     │
│                                                              │
│  ┌─────────────────────────────────────────────────────┐   │
│  │  Cleanup: delete workflow runs older than 30         │   │
│  └─────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

## Encryption Layers (Detailed)

### Layer 1: Firebase Remote Config

The app stores its `api_url` in Firebase Remote Config so it can be changed without app update.

- **Endpoint:** `POST https://firebaseremoteconfig.googleapis.com/v1/projects/playz-tv/namespaces/firebase:fetch?key=<API_KEY>`
- **Auth:** Just the API key in URL (no JWT needed for client fetch)
- **Headers:** `X-Android-Package: com.playz.tv`, `X-Android-Cert: 12a75869902c4f8a6826eb`
- **Response:** `{"entries":{"api_url":"https://adsalliances.shop/"},"state":"UPDATE","templateVersion":"32"}`

The current `api_url` is `https://adsalliances.shop/` (template v32). The old `https://playztv.online/` is dead (NXDOMAIN).

### Layer 2: AES-CBC Encryption

Two key/IV pairs found in `pa/C3262f.java` (lines 292-305):

| Pair | Key (hex) | IV (hex) | Used for |
|------|-----------|----------|----------|
| Key1/IV1 | `622f316a6d6c356e6b3478356b37704e` | `31346e4d6b386d4e354b6c354b4c376c` | All encrypted files except `app.txt` |
| Key2/IV2 | `6d354b6c356e6b34784b316b4e37704e` | `6b354b346e4d386d4b6c4e4c376c3135` | Only `app.txt` |

As ASCII:
- Key1 = `b/1jml5nk4x5k7pN`
- IV1 = `14nMk8mN5Kl5KL7l`
- Key2 = `m5Kl5nk4xK1kN7pN`
- IV2 = `k5K4nM8mKlNL7l15`

### Layer 3: Char Substitution Cipher

In `AbstractC3751a.java` (the `m8614b` method):

```
SRC: aAbBcCdDeEfFgGhHiIjJkKlLmMnNoOpPqQrRsStTuUvVwWxXyYzZ
DST: fFgGjJkKaApPbBmMoOzZeEnNcCdDrRqQtTvVuUxXhHiIwWyYlLsS
```

This is applied to the base64 string **before** decoding. So the full pipeline for non-`app.txt` files is:

```
encrypted_text (base64-like)
  → char-substitute each char via INV_MAP (DST→SRC)
  → base64 decode → still base64 text!
  → base64 decode again → AES ciphertext
  → AES-CBC decrypt with Key1/IV1
  → JSON
```

The double-base64 encoding is unusual — it suggests the developer wanted to obfuscate the fact that AES is being used.

## Stream URL Format

PlayZ TV stream URLs use a custom pipe-delimited format for HTTP headers:

```
https://example.com/stream.m3u8|referer=https://example.com&user-agent=Mozilla/5.0...&origin=https://example.com
```

The script parses this and applies headers during verification. When you paste the URL into Televizo, it understands this format natively.

## Verification Strategy

The verifier is conservative — it tries hard to confirm a stream works before marking it OK:

```python
def verify_hls(url, headers):
    1. Fetch the playlist (5KB cap)
    2. Check it starts with #EXTM3U
    3. If it's a master playlist (has #EXT-X-STREAM-INF):
       a. Parse first variant URL
       b. Fetch variant playlist
       c. Verify it's also M3U8
       d. Try HEAD request on first .ts segment
    4. If media playlist (no #EXT-X-STREAM-INF), it's directly playable
```

This catches:
- Dead URLs (404, 502, etc.)
- Geoblocked streams (403 from GitHub's IP)
- DNS failures
- Invalid M3U8 syntax
- Master playlists whose variants are dead

It does NOT verify:
- Actual video playback (would require a player)
- DRM-protected streams (can't decrypt without keys)
- Geo-unlocked streams (verifier runs from US/EU)

## Why Verification Rate Varies

You'll see verification rates between 30% and 70% depending on:

1. **Time of day** — More streams are live during Asian evening hours
2. **Geoblocking** — Streams from India/Bangladesh may block GitHub's US IPs
3. **CDN rotation** — Some streams use rotating tokens that expire
4. **Event timing** — Live event streams only exist during the event

## Failure Modes & Recovery

| Failure | Recovery |
|---------|----------|
| Firebase API down | Script exits with status 1, status.json records error |
| API URL changed | Script auto-fetches new URL from Firebase |
| AES keys changed (new APK) | Script fails to decrypt — manual key update needed |
| GitHub Actions delayed | Next run picks up where left off (idempotent) |
| All streams fail verification | Empty playlists committed (not a crash) |

## Performance Budget

- **Total runtime:** ~50 seconds (well under 10-min timeout)
- **Network requests:** ~150 (26 events × ~3 stream files + 110 stream verifications)
- **Parallelism:** 8 workers for stream verification
- **Memory:** ~50 MB peak (Python + crypto lib)
- **Disk:** ~500 KB output files per run

## Cron Schedule Reference

GitHub Actions cron uses UTC. Bangladesh is UTC+6.

| Cron expression | UTC time | Bangladesh time |
|----------------|----------|-----------------|
| `0 * * * *` | Every hour at :00 | Every hour at :00 (6:00, 7:00, ...) |
| `0 */2 * * *` | Every 2 hours at :00 | Every 2 hours |
| `0 0 * * *` | Daily at 00:00 UTC | Daily at 6:00 AM |
| `0 */6 * * *` | Every 6 hours | 4× per day |
| `*/30 * * * *` | Every 30 minutes | Every 30 minutes (may hit rate limits) |

**Recommendation:** Keep `0 * * * *` (hourly). More frequent runs don't help much since events change slowly, and GitHub may rate-limit you.
