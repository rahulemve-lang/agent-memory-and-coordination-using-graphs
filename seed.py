"""
seed.py - load demo evidence into the graph through the HTTP API (no dependencies).

    python seed.py                 # add the Acme/Postgres facts
    python seed.py --tasks         # ...and also create a fresh researcher + coder task

Auth: set GRAPHMEM_API_KEY in your environment, or pass --key. Defaults to 'change-me'.
  PowerShell:  $env:GRAPHMEM_API_KEY = "your-key" ; python seed.py
  Mac/Linux :  GRAPHMEM_API_KEY=your-key python seed.py
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
ap.add_argument("--tasks", action="store_true", help="also create a fresh researcher+coder task pair")
args = ap.parse_args()

args.key = args.key.strip().strip('"').strip("'")
if not args.key.isascii() or any(c in args.key for c in "\r\n\t "):
    sys.exit("The API key has characters that can't go in an HTTP header (non-English "
             "symbols such as curly quotes or dashes, spaces, or line breaks). Retype it "
             "by hand in plain single quotes: $env:GRAPHMEM_API_KEY = 'your-key'")


def post(path, body):
    body = {"tenant": args.tenant, "user": args.user, **body}
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


print("health:", urllib.request.urlopen(args.url + "/health", timeout=10).read().decode())

ep = post("/remember", {
    "agent": "planner",
    "text": "Acme wants a Q3 revenue dashboard on Postgres. Acme's compliance team requires "
            "all customer data to stay in India. Postgres can be hosted in the Mumbai region "
            "(India) or the Frankfurt region (Germany).",
    "importance": 0.9,
    "entities": [["Acme", "org"], ["Q3 Dashboard", "project"], ["Postgres", "tool"],
                 ["Mumbai Region", "place"], ["Frankfurt Region", "place"]],
    "relations": [["Q3 Dashboard", "requested_by", "Acme"],
                  ["Q3 Dashboard", "can_use", "Postgres"],
                  ["Postgres", "hosted_in", "Mumbai Region"],
                  ["Postgres", "hosted_in", "Frankfurt Region"]],
    "facts": [["Acme", "data residency", "India only"],
              ["Postgres", "available regions", "Mumbai, Frankfurt"],
              ["Mumbai Region", "country", "India"],
              ["Frankfurt Region", "country", "Germany"]],
})
print("seeded evidence, episode:", ep["episode_id"])

if args.tasks:
    t = post("/task/create_many", {"from_agent": "planner", "tasks": [
        {"title": "Choose database and hosting region for the Q3 Dashboard", "target": "researcher"},
        {"title": "Build the Q3 Dashboard backend using the approved decision",
         "target": "coder", "after": [0]},
    ]})
    print("created tasks:", t["task_ids"])

check = post("/recall", {"question": "Choose database and hosting region for the Q3 Dashboard", "hops": 2})
print("\nWhat the researcher will now see:\n" + check["context"])
