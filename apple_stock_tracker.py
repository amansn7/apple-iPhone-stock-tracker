#!/usr/bin/env python3
"""
iPhone 18 Pro Max 256GB stock tracker (any colour) for pincodes 95014, 33137.

Uses /shop/retail/pickup-message — the old /shop/fulfillment-messages endpoint
is dead (HTTP 541). Lookup failures report UNKNOWN, never "out of stock".
"""

import json
import os
import random
import signal
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# Homebrew Python ships without certs; fall back to the system bundle.
_CA = "/etc/ssl/cert.pem"
CTX = ssl.create_default_context(cafile=_CA) if os.path.exists(_CA) else None

PARTS = {
    "MJW44LL/A": "Black",
    "MJW54LL/A": "Silver",
    "MJW74LL/A": "Glacier",
    "MJW64LL/A": "Burgundy",
}
ZIPS = ["95014", "33137"]  # Cupertino, Miami
# Apple rate-limits pickup queries per egress IP: ~30 requests triggers HTTP 541,
# then a hard block for 10+ minutes. Randomize pacing; back off hard on 541.
INTERVAL = (120, 180)      # normal pause range, seconds
# Apple's block runs ~14 min, so the first backoff must clear that floor.
# Wake up early and the retry itself re-triggers the block, so consecutive
# 541s double the wait until a request actually gets through.
COOLDOWN = (840, 960)      # first 541 backoff, seconds
MAX_COOLDOWN = 3600        # ceiling for the exponential backoff
URL = "https://www.apple.com/shop/retail/pickup-message"

# Android push via ntfy.sh: install the "ntfy" app, subscribe to your own topic.
# The topic is a capability — anyone who has it can push to your phone — so it
# comes from the NTFY_TOPIC env var instead of the source. Empty = push disabled.
# Set NTFY_TOPIC_<PINCODE> to route one pincode to a different topic.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
_notified = {}  # pincode -> already pushed for this stock streak
_blocked = {"hit": False, "streak": 0}  # 541 hits since the last normal response

# Heartbeat: lets the auto-restarted instance detect and report a dead predecessor.
HB = os.path.expanduser("~/.apple_stock_tracker.hb")


def hb_read():
    try:
        with open(HB) as f:
            return f.read().strip()
    except OSError:
        return None


def hb_write(note="ok"):
    with open(HB, "w") as f:
        f.write(f"{os.getpid()}|{datetime.now().isoformat(timespec='seconds')}|{note}")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36",
    "Accept": "application/json",
}


def summarize(data):
    """Map API response to rows: (store, colour, status, detail)."""
    stores = data.get("body", {}).get("stores")
    if stores is None:
        return [("—", "—", "unknown", f"unexpected shape: head={data.get('head')}")]

    rows = []
    for s in stores:
        addr = s.get("retailStore", {}).get("address", {})
        name = f"{s.get('storeName', '?')} ({addr.get('city', '')} {addr.get('postalCode', '')})"
        for part, colour in PARTS.items():
            info = s.get("partsAvailability", {}).get(part)
            if not info:
                continue  # part not offered at this store
            display = info.get("pickupDisplay", "")
            if display == "available":
                rows.append((name, colour, "IN STOCK", info.get("pickupSearchQuote", "")))
            elif display in ("unavailable", "ineligible"):
                rows.append((name, colour, "out", info.get("pickupSearchQuote", "")))
            else:
                rows.append((name, colour, "unknown", f"pickupDisplay={display!r}"))
    return rows


def build_params(zipcode):
    params = [("pl", "true"), ("mts.0", "regular"), ("location", zipcode)]
    params += [(f"parts.{i}", p) for i, p in enumerate(PARTS)]
    return urllib.parse.urlencode(params)


def check_zip(zipcode):
    req = urllib.request.Request(f"{URL}?{build_params(zipcode)}", headers=HEADERS)

    try:
        with urllib.request.urlopen(req, timeout=15, context=CTX) as r:
            data = json.loads(r.read().decode())
        _blocked["streak"] = 0  # Apple answered normally: block is over
    except urllib.error.HTTPError as e:
        if e.code == 541:
            _blocked["hit"] = True
        return [("—", "—", "unknown", f"HTTP {e.code}: {e.reason}")]
    except (urllib.error.URLError, TimeoutError) as e:
        return [("—", "—", "unknown", f"network error: {e}")]
    except json.JSONDecodeError:
        return [("—", "—", "unknown", "bad response (blocked or API changed)")]
    return summarize(data)


def backoff(streak):
    """Seconds to wait after `streak` consecutive 541s (1-based)."""
    return min(int(random.uniform(*COOLDOWN) * (2 ** (streak - 1))), MAX_COOLDOWN)


def alert():
    # macOS only; ignore failure
    os.system("afplay /System/Library/Sounds/Glass.aiff >/dev/null 2>&1")


def topic_for(zipcode):
    """Per-pincode override (NTFY_TOPIC_33137) falling back to NTFY_TOPIC."""
    return os.environ.get(f"NTFY_TOPIC_{zipcode}") or NTFY_TOPIC


def publish(body, title, priority="default", zipcode=None):
    """Send an ntfy push. Returns True only if a push actually went out."""
    topic = topic_for(zipcode)
    if not topic:
        return False
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=body.encode(),
        headers={"Title": title, "Priority": priority},
    )
    urllib.request.urlopen(req, timeout=10, context=CTX).read()
    return True


def run_once():
    found = unknown = out = 0
    hits = {}  # pincode -> ["colour — store (detail)", ...]
    ts = datetime.now().strftime("%H:%M:%S")
    for zipcode in ZIPS:
        rows = check_zip(zipcode)
        print(f"\n=== {zipcode} @ {ts} ===")
        for name, colour, status, detail in rows:
            if status == "IN STOCK":
                found += 1
                hits.setdefault(zipcode, []).append(f"{colour} — {name} ({detail})")
                print(f"  *** IN STOCK *** {colour:9} {name} — {detail}")
            elif status == "unknown":
                unknown += 1
                print(f"  ?? UNKNOWN     {colour:9} {name} — {detail}")
            else:
                out += 1
    if found:
        print(f"\nSTOCK FOUND: {found} pickup option(s)")
        alert()
    for zipcode in ZIPS:
        lines = hits.get(zipcode)
        if not lines:
            _notified.pop(zipcode, None)  # re-arm so a fresh restock notifies again
            continue
        if _notified.get(zipcode):
            continue
        try:
            sent = publish("iPhone 18 Pro Max 256GB AVAILABLE\n" + "\n".join(lines[:10]),
                           "iPhone 18 Pro Max IN STOCK", priority="urgent", zipcode=zipcode)
            _notified[zipcode] = sent
            print(f"pushed {zipcode} -> ntfy/{topic_for(zipcode)}" if sent
                  else f"{zipcode}: ntfy push disabled — set NTFY_TOPIC[_{zipcode}] to enable")
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"!! ntfy push failed for {zipcode}: {e}")
    if not found:
        if unknown:
            print(f"\n{out} store/colour checks: out of stock; {unknown} lookup(s) FAILED "
                  f"— not a confirmed no-stock")
        else:
            print(f"\nChecked {out} store/colour checks: out of stock everywhere")
    return found


def selftest():
    def store(name, part, display):
        return {"storeName": name, "retailStore": {"address": {"city": "X", "postalCode": "1"}},
                "partsAvailability": {part: {"pickupDisplay": display, "pickupSearchQuote": display}}}

    data = {"body": {"stores": [
        store("A", "MJW44LL/A", "available"),
        store("B", "MJW54LL/A", "unavailable"),
        store("C", "MJW74LL/A", "weird-value"),
    ]}}
    rows = summarize(data)
    assert [r[2] for r in rows] == ["IN STOCK", "out", "unknown"], rows
    assert summarize({})[0][2] == "unknown"          # missing body -> unknown, not "out"
    q = urllib.parse.unquote(build_params("33304"))
    assert q.count("parts.") == len(PARTS) and all(p in q for p in PARTS), q  # all colours queried
    assert COOLDOWN[0] <= backoff(1) <= COOLDOWN[1], backoff(1)   # clears Apple's ~14 min block
    assert backoff(2) >= 2 * COOLDOWN[0], backoff(2)              # consecutive hits double
    assert backoff(5) == MAX_COOLDOWN, backoff(5)                 # and then just cap
    saved = dict(os.environ)
    try:
        os.environ["NTFY_TOPIC_33137"] = "miami"
        assert topic_for("33137") == "miami", topic_for("33137")  # pincode override wins
        del os.environ["NTFY_TOPIC_33137"]
        assert topic_for("95014") == NTFY_TOPIC, topic_for("95014")  # falls back
        assert topic_for(None) == NTFY_TOPIC, topic_for(None)        # no pincode -> base
    finally:
        os.environ.clear()
        os.environ.update(saved)
    print("selftest ok")


def main():
    if "--selftest" in sys.argv:
        return selftest()
    if "--notify-test" in sys.argv:
        i = sys.argv.index("--notify-test")
        zipcode = (sys.argv[i + 1] if i + 1 < len(sys.argv)
                   and not sys.argv[i + 1].startswith("--") else None)
        if publish("Test alert — setup works.", "apple_stock_tracker test",
                   priority="high", zipcode=zipcode):
            print(f"test push sent — topic: {topic_for(zipcode)}")
        else:
            print("ntfy push disabled — set the NTFY_TOPIC env var to enable")
        return
    print(f"Tracking iPhone 18 Pro Max 256GB — {', '.join(PARTS.values())}")
    print(f"Pincodes: {', '.join(ZIPS)} | interval {INTERVAL[0]}-{INTERVAL[1]}s | Ctrl+C to stop")
    for zipcode in ZIPS:
        topic = topic_for(zipcode)
        print(f"Android push: {zipcode} -> ntfy/{topic}" if topic
              else f"Android push: {zipcode} disabled (NTFY_TOPIC[_{zipcode}] not set)")

    if "--once" in sys.argv:
        run_once()
        return

    prev = hb_read()
    if prev:  # predecessor died without clearing the file -> report it
        pid, when, note = (prev.split("|", 2) + ["", "", ""])[:3]
        reason = note if note not in ("ok", "running") else "killed (no error recorded)"
        print(f"crash detected: PID {pid} died at {when} ({reason}) — notifying")
        try:
            publish(f"Tracker run PID {pid} died at {when}.\nReason: {reason}\n"
                    "Auto-restarted by launchd and still watching stock.",
                    "apple_stock_tracker CRASHED", priority="urgent")
        except Exception as e:
            print(f"!! crash notification failed: {e}")
    hb_write()
    while True:
        _blocked["hit"] = False
        try:
            run_once()
            hb_write()
        except Exception as e:  # survive anything: only an explicit stop may end this
            print(f"!! cycle failed: {e!r} — retrying next interval")
            hb_write(f"last cycle failed: {e!r}")
        if _blocked["hit"]:
            _blocked["streak"] += 1
            wait = backoff(_blocked["streak"])
            print(f"HTTP 541 rate limit hit — consecutive #{_blocked['streak']}, "
                  f"cooling down {wait}s (no requests until then)")
            time.sleep(wait)
        else:
            time.sleep(random.randint(*INTERVAL))


def _sigterm(_sig, _frm):  # launchd's stop signal = explicit stop, leave quietly
    try:
        os.remove(HB)
    except OSError:
        pass
    sys.exit(0)


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _sigterm)
    try:
        main()
    except KeyboardInterrupt:
        try:
            os.remove(HB)
        except OSError:
            pass
        print("\nstopped")
    except SystemExit:
        pass  # clean exit (SIGTERM handler already cleared the heartbeat)
    except BaseException as e:  # record why we're dying; restarted instance reports it
        try:
            hb_write(note=repr(e))
        except Exception:
            pass
        raise
