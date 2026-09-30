# iPhone stock tracker

Watches Apple's retail pickup API for iPhone 18 Pro Max 256GB and tells you the
moment any colour is available for pickup near a pincode.

Pure Python standard library — nothing to `pip install`.

```
=== 33137 @ 13:05:40 ===
  *** IN STOCK *** Burgundy  The Galleria (Fort Lauderdale 33304) — Available Today
```

On a hit it plays a sound, and (optionally) pushes to your Android phone via
[ntfy.sh](https://ntfy.sh).

## Configure

Everything is at the top of `apple_stock_tracker.py`:

```python
PARTS = {                    # part number -> colour
    "MJW44LL/A": "Black",
    "MJW54LL/A": "Silver",
    "MJW74LL/A": "Glacier",
    "MJW64LL/A": "Burgundy",
}
ZIPS = ["33137"]             # one or more pincodes
INTERVAL = (120, 180)        # seconds between checks (randomized)
```

`PARTS` keys are the Apple part numbers for the model/size you want — open the
product's pickup page in a browser, look at the `parts.` params on the
`pickup-message` request, and copy them across.

## Run

```bash
python3 apple_stock_tracker.py --once        # single check, then exit
python3 apple_stock_tracker.py --selftest    # offline sanity check (no requests)
python3 apple_stock_tracker.py               # watch forever, Ctrl+C to stop
```

The log is written to stdout, so redirect it wherever you like.

## Phone notifications (optional)

1. Install the **ntfy** app on Android and subscribe to any topic name you make up.
2. Export the same topic before starting:

```bash
export NTFY_TOPIC="your-long-random-topic"
python3 apple_stock_tracker.py --notify-test   # confirm the app buzzes
```

The topic is a capability — anyone who knows it can push to you — so it is read
from the environment and never committed. Leave it unset and push is simply off;
the stock check still works.

## Run at login (macOS launchd)

Copy the plist and fix the two paths for your machine:

```bash
cp com.apple-stock-tracker.plist ~/Library/LaunchAgents/
#   /Users/YOUR_USERNAME/... in ProgramArguments and StandardOutPath
#   python3 path: /usr/local/bin/python3 (Intel) or /opt/homebrew/bin/python3 (Apple Silicon)
```

Then load it:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.apple-stock-tracker.plist
launchctl list | grep apple-stock          # should show a PID, not -
tail -f ~/apple_stock_tracker.log
```

To stop and remove:

```bash
launchctl bootout gui/$(id -u)/com.apple-stock-tracker
rm ~/Library/LaunchAgents/com.apple-stock-tracker.plist
```

`caffeinate -i` wraps the script so the Mac won't idle-sleep through a
cooldown. `KeepAlive` restarts the process if it dies, and a heartbeat file lets
the next instance report *why* the previous one died.

## Rate limiting

Apple rate-limits pickup queries per egress IP: roughly 30 requests triggers
`HTTP 541` and a hard block for ~14 minutes.

The tracker handles this by randomizing its interval and backing off hard on a
541 — the first cooldown already clears the block, and consecutive failures
double the wait (capped at an hour) until a request gets through again. Failed
lookups are reported as `UNKNOWN`, never as "out of stock", so a blocked IP
never produces a false all-clear.

If you keep getting 541s, raise `INTERVAL` (e.g. `(240, 360)`) to stay under
the request threshold.

## Notes

- Lookup failures, network errors and API shape changes are printed as
  `?? UNKNOWN` and are explicitly *not* treated as out-of-stock.
- Reports are for personal use. Automated access to apple.com may be restricted
  by their terms — keep the interval polite.
- Not affiliated with Apple.
