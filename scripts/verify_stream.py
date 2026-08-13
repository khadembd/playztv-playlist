#!/usr/bin/env python3
"""
Verify a single stream URL (for testing).
Usage: python3 verify_stream.py <url>
"""
import sys
sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
from playztv_auto_update import verify_stream, parse_stream_url

if len(sys.argv) < 2:
    print("Usage: python3 verify_stream.py <url>")
    sys.exit(1)

url = sys.argv[1]
print(f"Verifying: {url}")
print()
base, headers = parse_stream_url(url)
print(f"Base URL: {base}")
print(f"Headers: {headers}")
print()

ok, msg, parsed_url, parsed_headers = verify_stream(url)
print(f"Result: {'✓ OK' if ok else '✗ FAIL'}")
print(f"Message: {msg}")
