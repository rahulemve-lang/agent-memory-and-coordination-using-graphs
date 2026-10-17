"""
demo_stale.py - the demo moment, through the HTTP API (stdlib only).

  1. Changes Acme's data-residency fact ("India only" -> "India or EU allowed")
  2. Asks the graph which decisions now rest on outdated evidence
  3. Prints the full trace of the approved decision and writes trace.html

Run AFTER workflow 2 has produced an approved decision.
  PowerShell:  $env:GRAPHMEM_API_KEY = 'your-key' ; python demo_stale.py
Needs viz.py in the same folder for trace.html (the text trace works without it).
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

ap = argparse.ArgumentParser()
ap.add_argument("--url", default=os.environ.get("GRAPHMEM_URL", "http://localhost:8080"))
ap.add_argument("--key", default=os.environ.get("GRAPHMEM_API_KEY", "change-me"))
ap.add_argument("--tenant", default="demo")
ap.add_argument("--user", default="priya")
ap.add_argument("--value", default="India or EU allowed", help="new value for Acme's data residency")
args = ap.parse_args()

args.key = args.key.strip().strip('"').strip("'")
if not args.key.isascii() or any(c in args.key for c in "\r\n\t "):
    sys.exit("API key has characters that can't go in an HTTP header. Retype it in plain single quotes.")


def post(path, body=None):
    body = {"tenant": args.tenant, "user": args.user, **(body or {})}
    req = urllib.request.Request(args.url + path, json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "X-API-Key": args.key})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        sys.exit(f"{path} failed ({e.code}): {e.read().decode()}")
    except urllib.error.URLError as e:
        sys.exit(f"Cannot reach {args.url} ({e.reason}). Is `docker compose up -d` running?")
    except Exception as e:
        sys.exit(f"{path} failed: {type(e).__name__}: {e}")


print("1) Changing the fact: Acme / data residency ->", repr(args.value))
v = post("/fact/set", {"subject": "Acme", "predicate": "data residency",
                       "value": args.value, "agent": "planner"})
print("   new version:", v["version"])

print("\n2) Decisions now resting on outdated evidence:")
stale = post("/stale")["stale"]
if not stale:
    sys.exit("   none found. Has workflow 2 produced an approved decision yet?")

best = None
for s in stale:
    w = post("/why", {"decision_id": s["id"]})
    print(f"   - [{w['status']}] {w['summary']}")
    if w["status"] == "approved" and best is None:
        best = w
best = best or w

print("\n3) Why the agent decided that, and what changed since:\n")
print(best["text"])

try:
    from viz import write_trace
    path = write_trace(best, "trace.html")
    print(f"\nOpen {os.path.abspath(path)} in your browser for the visual trace.")
except ImportError:
    print("\n(viz.py not found next to this script, so no trace.html was written.)")
