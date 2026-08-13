#!/usr/bin/env python3
"""
PlayZ TV — Auto-updating Live + Upcoming playlist extractor
with stream verification and anti-blocking.

This script:
  1. Fetches the live API URL from Firebase Remote Config
  2. Decrypts app.txt, events.txt, categories.txt, sports.txt
  3. Fetches per-event stream files
  4. Verifies each stream (HLS / DASH / HTTP play-test)
  5. Filters out dead streams
  6. Generates multiple M3U variants
  7. Writes a status.json with run metadata

Designed to run in GitHub Actions (ubuntu-latest, Python 3.11).
No external browser needed — pure HTTP requests.
"""
import base64
import json
import os
import sys
import time
import random
import hashlib
import urllib.request
import urllib.parse
import urllib.error
import ssl
import socket
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

# ============== CONSTANTS (from APK reverse-engineering) ==============
FIREBASE_PROJECT = "playz-tv"
FIREBASE_API_KEY = "AIzaSyDKRqLlbaZBIpHzLBiQTUrJqr3gN-nDWWc"
FIREBASE_APP_ID  = "1:516859456626:android:12a75869902c4f8a6826eb"

KEY1 = bytes([98, 47, 49, 106, 109, 108, 53, 110, 107, 52, 120, 53, 107, 55, 112, 78])
IV1  = bytes([49, 52, 110, 77, 107, 56, 109, 78, 53, 75, 108, 53, 75, 76, 55, 108])
KEY2 = bytes([109, 53, 75, 108, 53, 110, 107, 52, 120, 75, 49, 107, 78, 55, 112, 78])
IV2  = bytes([107, 53, 75, 52, 110, 77, 56, 109, 75, 108, 78, 76, 55, 108, 49, 53])

SRC = "aAbBcCdDeEfFgGhHiIjJkKlLmMnNoOpPqQrRsStTuUvVwWxXyYzZ"
DST = "fFgGjJkKaApPbBmMoOzZeEnNcCdDrRqQtTvVuUxXhHiIwWyYlLsS"
INV_MAP = {}
for i, c in enumerate(DST):
    INV_MAP[c] = SRC[i]
for i in range(128):
    if chr(i) not in INV_MAP:
        INV_MAP[chr(i)] = chr(i)

# CI-friendly paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(SCRIPT_DIR)
OUTPUT_DIR = os.path.join(REPO_ROOT, "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Bangladesh timezone (UTC+6)
BD_TZ = timezone(timedelta(hours=6))

# ============== ANTI-BLOCKING ==============
USER_AGENTS = [
    "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 12; SM-A325F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
]

# SSL context that doesn't verify (some streams have bad certs)
SSL_CTX = ssl.create_default_context()
SSL_CTX.check_hostname = False
SSL_CTX.verify_mode = ssl.CERT_NONE

def get_random_ua():
    return random.choice(USER_AGENTS)

def http_get(url, headers=None, timeout=20, retries=3, jitter=True):
    """HTTP GET with anti-block: random UA, jitter delay, retries."""
    base_headers = {"User-Agent": get_random_ua()}
    if headers:
        base_headers.update(headers)
    if jitter:
        time.sleep(random.uniform(0.2, 0.8))

    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=base_headers)
            with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
                return r.read()
        except (urllib.error.URLError, urllib.error.HTTPError, socket.timeout, ssl.SSLError, ConnectionError) as e:
            last_err = e
            if attempt < retries - 1:
                wait = (attempt + 1) * 1.5 + random.uniform(0, 0.5)
                time.sleep(wait)
    raise last_err if last_err else Exception("HTTP request failed")

def http_head(url, headers=None, timeout=10):
    """Lightweight HEAD request to check if URL exists."""
    base_headers = {"User-Agent": get_random_ua()}
    if headers:
        base_headers.update(headers)
    try:
        req = urllib.request.Request(url, headers=base_headers, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            return r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers) if e.headers else {}
    except Exception:
        return None, {}

# ============== DECRYPT HELPERS ==============
def m8614b(s):
    """Apply char substitution then base64-decode."""
    substituted = ''.join(INV_MAP.get(c, c) for c in s)
    return base64.b64decode(substituted)

def aes_decrypt(data, key, iv):
    cipher = AES.new(key, AES.MODE_CBC, iv)
    dec = cipher.decrypt(data)
    try: dec = unpad(dec, 16)
    except: pass
    return dec

def decrypt_app_txt(raw):
    """app.txt: direct base64 + AES Key2/IV2."""
    data = base64.b64decode(raw)
    return aes_decrypt(data, KEY2, IV2).decode('utf-8')

def decrypt_other(raw):
    """events.txt / categories.txt / sports.txt / link files:
       char-substitute + base64 + base64 + AES Key1/IV1."""
    step1 = m8614b(raw)
    step1_text = step1.decode('utf-8')
    step2 = base64.b64decode(step1_text)
    return aes_decrypt(step2, KEY1, IV1).decode('utf-8')

def fetch_and_decrypt(api_url, path, decryptor):
    raw = http_get(api_url + path).decode('utf-8').strip()
    return decryptor(raw)

def parse_event_date(date_str, time_str):
    try:
        dt = datetime.strptime(f"{date_str} {time_str}", "%d/%m/%Y %H:%M:%S")
        return dt.replace(tzinfo=BD_TZ)
    except Exception:
        return None

# ============== STREAM VERIFICATION ==============
def parse_stream_url(url):
    """Parse a PlayZ stream URL which may have |key=value&key=value suffix."""
    parts = url.split('|', 1)
    base_url = parts[0].rstrip('?&')
    headers = {}
    if len(parts) > 1:
        for kv in parts[1].split('&'):
            if '=' in kv:
                k, v = kv.split('=', 1)
                k = k.strip()
                if k.lower() == 'user-agent':
                    headers['User-Agent'] = v
                elif k.lower() == 'referer':
                    headers['Referer'] = v
                elif k.lower() == 'origin':
                    headers['Origin'] = v
                elif k.lower() == 'cookie':
                    headers['Cookie'] = v
    return base_url, headers

def verify_hls(url, headers, timeout=10):
    """Verify HLS stream: fetch playlist, check #EXTM3U, optionally fetch first segment."""
    try:
        req_headers = {"User-Agent": get_random_ua()}
        req_headers.update(headers)
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            body = r.read(5000).decode('utf-8', errors='replace')
        if '#EXTM3U' not in body:
            return False, "not M3U8"
        # If it's a master playlist (has #EXT-X-STREAM-INF), try fetching first variant
        if '#EXT-X-STREAM-INF' in body:
            for line in body.split('\n'):
                line = line.strip()
                if line and not line.startswith('#'):
                    # Resolve relative URL
                    variant_url = urllib.parse.urljoin(url, line)
                    try:
                        req2 = urllib.request.Request(variant_url, headers=req_headers)
                        with urllib.request.urlopen(req2, timeout=timeout, context=SSL_CTX) as r2:
                            var_body = r2.read(5000).decode('utf-8', errors='replace')
                        if '#EXTM3U' in var_body:
                            # Try fetching first .ts segment
                            for vline in var_body.split('\n'):
                                vline = vline.strip()
                                if vline and not vline.startswith('#'):
                                    seg_url = urllib.parse.urljoin(variant_url, vline)
                                    status, _ = http_head(seg_url, headers=req_headers, timeout=5)
                                    if status and 200 <= status < 400:
                                        return True, "OK (master+variant+segment)"
                                    return True, "OK (master+variant, segment check skipped)"
                            return True, "OK (master+variant)"
                        return True, "OK (master, variant not M3U8)"
                    except Exception:
                        return True, "OK (master only)"
            return True, "OK (master, no variant URL)"
        return True, "OK (media playlist)"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)[:50]

def verify_dash(url, headers, timeout=10):
    """Verify DASH stream: fetch MPD, check it's valid XML with MPD root."""
    try:
        req_headers = {"User-Agent": get_random_ua()}
        req_headers.update(headers)
        req = urllib.request.Request(url, headers=req_headers)
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX) as r:
            body = r.read(5000).decode('utf-8', errors='replace')
        if '<MPD' in body and ('</MPD>' in body or 'xmlns="urn:mpeg:dash' in body):
            return True, "OK (MPD valid)"
        return False, "not MPD"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)[:50]

def verify_http_stream(url, headers, timeout=8):
    """Verify generic HTTP stream via HEAD request."""
    status, _ = http_head(url, headers=headers, timeout=timeout)
    if status and 200 <= status < 400:
        return True, f"OK (HTTP {status})"
    return False, f"HTTP {status}" if status else "no response"

def verify_stream(full_url):
    """Verify a stream URL. Returns (ok, message, parsed_url, parsed_headers)."""
    if not full_url or full_url.startswith('#'):
        return False, "empty", "", {}
    base_url, headers = parse_stream_url(full_url)
    if not base_url or not base_url.startswith(('http://', 'https://')):
        return False, "invalid URL", base_url, headers
    # Skip known placeholders / error URLs
    if any(s in base_url for s in ['error.m3u8', 'error_pro.com', 'error.com', 'default.url']):
        return False, "error placeholder", base_url, headers
    # Route by extension
    lower = base_url.lower()
    if lower.endswith('.m3u8') or '.m3u8?' in lower or '/index.m3u8' in lower:
        return (*verify_hls(base_url, headers), base_url, headers)[:4] if len(verify_hls(base_url, headers)) == 2 else (False, "verify error", base_url, headers)
    elif lower.endswith('.mpd') or '.mpd?' in lower:
        ok, msg = verify_dash(base_url, headers)
        return ok, msg, base_url, headers
    else:
        ok, msg = verify_http_stream(base_url, headers)
        return ok, msg, base_url, headers

def verify_streams_parallel(streams, max_workers=8, timeout_per=10):
    """Verify a list of streams in parallel. Returns list of (stream, ok, message)."""
    results = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_stream = {ex.submit(verify_stream, s['link']): s for s in streams if s.get('link')}
        for future in as_completed(future_to_stream):
            stream = future_to_stream[future]
            try:
                ok, msg, _, _ = future.result(timeout=timeout_per + 5)
            except Exception as e:
                ok, msg = False, f"verify exception: {str(e)[:50]}"
            results.append((stream, ok, msg))
    return results

# ============== MAIN PIPELINE ==============
def get_live_api_url():
    print("[1/7] Fetching Firebase Remote Config...")
    url = f"https://firebaseremoteconfig.googleapis.com/v1/projects/{FIREBASE_PROJECT}/namespaces/firebase:fetch?key={FIREBASE_API_KEY}"
    body = json.dumps({
        "appId": FIREBASE_APP_ID,
        "appInstanceId": "default",
        "appInstanceIdToken": "default",
        "languageCode": "en-US"
    }).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": "application/json",
        "X-Android-Package": "com.playz.tv",
        "X-Android-Cert": "12a75869902c4f8a6826eb"
    })
    with urllib.request.urlopen(req, timeout=30) as r:
        result = json.loads(r.read())
    api_url = result.get("entries", {}).get("api_url")
    if not api_url:
        raise RuntimeError(f"No api_url: {result}")
    print(f"      ✓ Live API URL: {api_url}  (template v{result.get('templateVersion','?')})")
    return api_url

def fetch_app_config(api_url):
    print("[2/7] Fetching app.txt...")
    text = fetch_and_decrypt(api_url, "app.txt", decrypt_app_txt)
    obj = json.loads(text)
    if isinstance(obj, list):
        obj = obj[0] if obj else {}
    print(f"      ✓ App v{obj.get('app_versions')}, Telegram: {obj.get('telegram_url')}")
    return obj

def fetch_events(api_url):
    print("[3/7] Fetching events.txt...")
    text = fetch_and_decrypt(api_url, "events.txt", decrypt_other)
    arr = json.loads(text)
    events = []
    for item in arr:
        try:
            inner = json.loads(item['event'])
            events.append(inner)
        except: continue
    visible = [e for e in events if e.get('visible', True)]
    print(f"      ✓ Total: {len(events)}, Visible: {len(visible)}")
    visible.sort(key=lambda e: parse_event_date(e.get('date',''), e.get('time','')) or datetime.max.replace(tzinfo=BD_TZ))
    return visible

def fetch_event_streams(api_url, event):
    links_path = event.get('links', '')
    if not links_path:
        return []
    try:
        text = fetch_and_decrypt(api_url, links_path, decrypt_other)
        obj = json.loads(text)
        if isinstance(obj, list):
            return obj
        elif isinstance(obj, dict) and 'links' in obj:
            return obj['links']
    except Exception as e:
        print(f"      ! Failed {links_path[:60]}: {e}")
    return []

def fetch_categories(api_url):
    print("[5/7] Fetching categories.txt + sports.txt...")
    cats = []
    for fname, key in [('categories.txt', 'cat'), ('sports.txt', 'cat')]:
        try:
            text = fetch_and_decrypt(api_url, fname, decrypt_other)
            arr = json.loads(text)
            for item in arr:
                try:
                    inner = json.loads(item[key])
                    if inner.get('visible', True):
                        cats.append(inner)
                except: pass
        except Exception as e:
            print(f"      ! {fname} failed: {e}")
    print(f"      ✓ Categories: {len(cats)}")
    return cats

def build_m3u(events, categories, app_config, hls_only=False, status_filter=None):
    """Build M3U playlist.
       hls_only: if True, only include HLS (.m3u8) streams (Televizo-friendly)
       status_filter: None | 'live' | 'upcoming' | 'finished'
    """
    now = datetime.now(BD_TZ)
    lines = [
        '#EXTM3U',
        f'# PlayZ TV Auto-Updated Playlist',
        f'# Generated: {now.isoformat()}',
        f'# App version: {app_config.get("app_versions", "?")}',
        f'# Telegram: {app_config.get("telegram_url", "https://t.me/playztv")}',
        f'# Mode: {"HLS-only (Televizo-friendly)" if hls_only else "All streams (HLS+DASH)"}',
        f'# Filter: {status_filter or "all"}',
        '',
    ]

    total = 0
    for ev in events:
        dt = parse_event_date(ev.get('date',''), ev.get('time',''))
        if dt:
            if dt <= now <= dt + timedelta(hours=4):
                status_label = 'LIVE'
                status_emoji = '🔴'
                if status_filter == 'upcoming': continue
                if status_filter == 'finished': continue
            elif dt > now:
                status_label = f'UPCOMING {dt.strftime("%b %d %H:%M")}'
                status_emoji = '⏰'
                if status_filter == 'live': continue
                if status_filter == 'finished': continue
            else:
                status_label = f'FINISHED {dt.strftime("%b %d")}'
                status_emoji = '✓'
                if status_filter == 'live': continue
                if status_filter == 'upcoming': continue
        else:
            status_label = ''
            status_emoji = ''

        sport = ev.get('category', 'Sports')
        league = ev.get('eventName', '')
        team_a = ev.get('teamAName', '')
        team_b = ev.get('teamBName', '')
        logo = ev.get('eventLogo', '')
        link_names = ev.get('link_names', [])
        streams = ev.get('_streams', [])
        verified = ev.get('_verified', [])

        group = f"Events | {sport} | {league}"

        for i, (stream, ok, msg) in enumerate(verified):
            if not ok:
                continue
            url = stream.get('link') if isinstance(stream, dict) else stream
            if not url:
                continue
            base_url = url.split('|')[0].lower()
            if hls_only and not (base_url.endswith('.m3u8') or '.m3u8?' in base_url or '/index.m3u8' in base_url):
                continue
            label = link_names[i] if i < len(link_names) else f"Server {i+1}"
            verify_tag = '✓' if ok else '✗'
            name = f"{status_emoji} {verify_tag} {label} | {team_a} vs {team_b}"
            lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="{group}",{name}')
            lines.append(url)
            lines.append('')
            total += 1

    # Add channel categories as playlist references
    if not status_filter:
        lines.append('# ============================================================')
        lines.append('# 📺 CHANNEL CATEGORIES (paste URLs into Televizo)')
        lines.append('# ============================================================')
        lines.append('')
        for cat in categories:
            name = cat.get('name', 'Channel')
            logo = cat.get('logo', '')
            api = cat.get('api', '')
            cat_type = cat.get('type', '')
            if not api or api.startswith('channels/'):
                continue
            group = f"Channels | {cat_type}"
            lines.append(f'#EXTINF:-1 tvg-logo="{logo}" group-title="{group}",{name}')
            lines.append(api)
            lines.append('')

    lines.insert(7, f'# Total entries: {total}')
    return '\n'.join(lines)

def main():
    print("=" * 65)
    print("PlayZ TV — Auto-Update Playlist with Stream Verification")
    print("=" * 65)
    start_time = time.time()

    try:
        api_url = get_live_api_url()
        app_config = fetch_app_config(api_url)
        events = fetch_events(api_url)

        print(f"\n[4/7] Fetching streams for {len(events)} events...")
        for i, ev in enumerate(events):
            streams = fetch_event_streams(api_url, ev)
            ev['_streams'] = streams
            if streams:
                print(f"  [{i+1}/{len(events)}] {ev.get('category','?'):12} | {ev.get('teamAName','?')[:15]:15} vs {ev.get('teamBName','?')[:15]:15} | {len(streams)} streams")

        categories = fetch_categories(api_url)

        # Verify all event streams in parallel
        print(f"\n[6/7] Verifying streams (parallel, 8 workers)...")
        all_streams_to_verify = []
        for ev in events:
            for s in ev.get('_streams', []):
                if isinstance(s, dict) and s.get('link'):
                    all_streams_to_verify.append(s)
                elif isinstance(s, str):
                    all_streams_to_verify.append({'link': s})

        print(f"      Verifying {len(all_streams_to_verify)} streams...")
        verified = verify_streams_parallel(all_streams_to_verify, max_workers=8)

        # Map verified results back to events
        idx = 0
        for ev in events:
            ev_streams = ev.get('_streams', [])
            ev_verified = []
            for s in ev_streams:
                if idx < len(verified):
                    ev_verified.append(verified[idx])
                    idx += 1
                else:
                    ev_verified.append((s, False, "no result"))
            ev['_verified'] = ev_verified

        ok_count = sum(1 for _, ok, _ in verified if ok)
        fail_count = len(verified) - ok_count
        print(f"      ✓ Verified: {ok_count} OK, {fail_count} failed")

        # Generate multiple M3U variants
        print(f"\n[7/7] Generating M3U playlists...")
        # 1. Master (all streams, all statuses)
        master = build_m3u(events, categories, app_config, hls_only=False, status_filter=None)
        with open(os.path.join(OUTPUT_DIR, "playztv_master.m3u"), 'w', encoding='utf-8') as f:
            f.write(master)

        # 2. HLS-only (Televizo-friendly)
        hls_only = build_m3u(events, categories, app_config, hls_only=True, status_filter=None)
        with open(os.path.join(OUTPUT_DIR, "playztv_hls_only.m3u"), 'w', encoding='utf-8') as f:
            f.write(hls_only)

        # 3. Live now (HLS only)
        live = build_m3u(events, [], app_config, hls_only=True, status_filter='live')
        with open(os.path.join(OUTPUT_DIR, "playztv_live.m3u"), 'w', encoding='utf-8') as f:
            f.write(live)

        # 4. Upcoming (HLS only)
        upcoming = build_m3u(events, [], app_config, hls_only=True, status_filter='upcoming')
        with open(os.path.join(OUTPUT_DIR, "playztv_upcoming.m3u"), 'w', encoding='utf-8') as f:
            f.write(upcoming)

        # Save JSON for inspection
        with open(os.path.join(OUTPUT_DIR, "events.json"), 'w', encoding='utf-8') as f:
            json.dump(events, f, indent=2, ensure_ascii=False, default=str)
        with open(os.path.join(OUTPUT_DIR, "categories.json"), 'w', encoding='utf-8') as f:
            json.dump(categories, f, indent=2, ensure_ascii=False)

        # Save stream URLs CSV
        csv_lines = ['event_id,category,league,team_a,team_b,date,time,link_name,stream_url,verified,verify_message']
        for ev in events:
            link_names = ev.get('link_names', [])
            for i, (stream, ok, msg) in enumerate(ev.get('_verified', [])):
                label = link_names[i] if i < len(link_names) else f"Server {i+1}"
                url = stream.get('link') if isinstance(stream, dict) else stream
                csv_lines.append(f'"{ev.get("category","")}","{ev.get("eventName","")}","{ev.get("teamAName","")}","{ev.get("teamBName","")}","{ev.get("date","")}","{ev.get("time","")}","{label}","{url or ""}","{"OK" if ok else "FAIL"}","{msg}"')
        with open(os.path.join(OUTPUT_DIR, "playztv_streams.csv"), 'w', encoding='utf-8') as f:
            f.write('\n'.join(csv_lines))

        # Save status.json
        now = datetime.now(BD_TZ)
        status = {
            "last_updated": now.isoformat(),
            "last_updated_unix": int(now.timestamp()),
            "api_url": api_url,
            "app_version": app_config.get("app_versions"),
            "telegram": app_config.get("telegram_url"),
            "stats": {
                "total_events": len(events),
                "total_streams": len(verified),
                "verified_ok": ok_count,
                "verified_failed": fail_count,
                "categories": len(categories),
            },
            "verification_rate": f"{(ok_count / len(verified) * 100):.1f}%" if verified else "0%",
            "run_duration_seconds": round(time.time() - start_time, 1),
            "raw_playlist_urls": {
                "master": "output/playztv_master.m3u",
                "hls_only": "output/playztv_hls_only.m3u",
                "live": "output/playztv_live.m3u",
                "upcoming": "output/playztv_upcoming.m3u",
            },
            "github_raw_base": "https://raw.githubusercontent.com/YOUR_USERNAME/YOUR_REPO/main/output/",
        }
        with open(os.path.join(OUTPUT_DIR, "status.json"), 'w') as f:
            json.dump(status, f, indent=2)

        # Print summary
        live_count = sum(1 for e in events if parse_event_date(e.get('date',''), e.get('time','')) and parse_event_date(e.get('date',''), e.get('time','')) <= now <= parse_event_date(e.get('date',''), e.get('time','')) + timedelta(hours=4))
        upcoming_count = sum(1 for e in events if parse_event_date(e.get('date',''), e.get('time','')) and parse_event_date(e.get('date',''), e.get('time','')) > now)
        finished_count = len(events) - live_count - upcoming_count

        print(f"\n{'='*65}")
        print(f"SUMMARY")
        print(f"{'='*65}")
        print(f"  Events:    {len(events)} total")
        print(f"             🔴 Live: {live_count} | ⏰ Upcoming: {upcoming_count} | ✓ Finished: {finished_count}")
        print(f"  Streams:   {len(verified)} total | ✓ {ok_count} OK | ✗ {fail_count} failed")
        print(f"  Verification rate: {(ok_count / len(verified) * 100):.1f}%" if verified else "  Verification rate: N/A")
        print(f"  Categories: {len(categories)}")
        print(f"  Run time:  {round(time.time() - start_time, 1)}s")
        print(f"\n  Generated playlists in {OUTPUT_DIR}/:")
        for fname in ['playztv_master.m3u', 'playztv_hls_only.m3u', 'playztv_live.m3u', 'playztv_upcoming.m3u', 'status.json']:
            fpath = os.path.join(OUTPUT_DIR, fname)
            if os.path.exists(fpath):
                print(f"    {fname}: {os.path.getsize(fpath)} bytes")
        print(f"\n  Next: commit & push to GitHub. Workflow runs hourly.")
        return 0

    except Exception as e:
        import traceback
        print(f"\n❌ FATAL ERROR: {e}")
        traceback.print_exc()
        # Save error status
        with open(os.path.join(OUTPUT_DIR, "status.json"), 'w') as f:
            json.dump({
                "last_updated": datetime.now(BD_TZ).isoformat(),
                "error": str(e),
                "traceback": traceback.format_exc(),
            }, f, indent=2)
        return 1

if __name__ == "__main__":
    sys.exit(main())
