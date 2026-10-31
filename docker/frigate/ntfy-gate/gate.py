"""ntfy-gate: a pass-through proxy in front of ntfy that throttles alerts per camera group.

frigate-notify sends each alert here instead of to ntfy, tagged with an X-Camera header.
The first alert for a group is forwarded as-is; further alerts for the same group are
dropped until COOLDOWN seconds have passed since the last forwarded one (fixed window).

If FRIGATE_URL is set, a watcher also polls Frigate's /api/stats and sends its own (unthrottled)
alerts when a camera's fps stays at 0, or Frigate is unreachable, for OFFLINE_AFTER seconds,
plus a recovery message when it comes back.

Env:
  UPSTREAM       ntfy base URL, e.g. https://ntfy.example.com
  CAMERA_GROUPS  "backyard=camA,camB;front=camC" (cameras not listed get their own group)
  COOLDOWN       window in seconds (default 180)
  FRIGATE_URL    e.g. http://frigate:5000 (unset = watcher disabled)
  NTFY_TOPIC     topic for watcher alerts
  NTFY_TOKEN     ntfy access token for watcher alerts
  POLL_INTERVAL  seconds between stats polls (default 30)
  OFFLINE_AFTER  seconds at 0 fps before alerting (default 120)
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import lights

UPSTREAM = os.environ["UPSTREAM"].rstrip("/")
COOLDOWN = float(os.environ.get("COOLDOWN", "180"))
CAMERA_HEADER = "X-Camera"
# Hop-by-hop / proxy-only headers that must not be forwarded upstream
DROP_HEADERS = {"host", "connection", "content-length", "transfer-encoding", CAMERA_HEADER.lower()}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ntfy-gate")


def parse_groups(spec):
    groups = {}
    for entry in filter(None, (e.strip() for e in spec.split(";"))):
        name, _, cams = entry.partition("=")
        for cam in filter(None, (c.strip() for c in cams.split(","))):
            groups[cam] = name.strip()
    return groups


CAMERA_GROUPS = parse_groups(os.environ.get("CAMERA_GROUPS", ""))
last_sent = {}  # group -> monotonic time of last forwarded alert
lock = threading.Lock()


def reserve(group):
    """Claim the group's window. Returns (allowed, previous_timestamp, seconds_left)."""
    now = time.monotonic()
    with lock:
        prev = last_sent.get(group)
        if prev is not None and now - prev < COOLDOWN:
            return False, prev, COOLDOWN - (now - prev)
        last_sent[group] = now
        return True, prev, 0


def release(group, prev):
    """Undo a reservation when the upstream send failed, so the next alert isn't swallowed."""
    with lock:
        if prev is None:
            last_sent.pop(group, None)
        else:
            last_sent[group] = prev


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/healthz":
            return self.reply(200, {"ok": True})
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        camera = self.headers.get(CAMERA_HEADER)

        # No camera tag -> fail open, forward unthrottled
        group = CAMERA_GROUPS.get(camera, camera) if camera else None
        prev = None
        if group:
            allowed, prev, left = reserve(group)
            if not allowed:
                log.info("suppressed camera=%s group=%s (%.0fs left)", camera, group, left)
                # Must not contain the word "error": frigate-notify treats that as a failed send
                return self.reply(200, {"suppressed": True, "group": group})

        headers = {k: v for k, v in self.headers.items() if k.lower() not in DROP_HEADERS}
        req = urllib.request.Request(UPSTREAM + self.path, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                status, data = resp.status, resp.read()
        except urllib.error.HTTPError as e:
            status, data = e.code, e.read()
        except Exception as e:  # network failure
            if group:
                release(group, prev)
            log.warning("upstream failed camera=%s: %s", camera, e)
            return self.reply(502, {"error": f"upstream: {e}"})

        if status >= 300 and group:
            release(group, prev)
        log.info("forwarded camera=%s group=%s status=%s", camera, group, status)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def reply(self, status, obj):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass  # replaced by our own logging


FRIGATE_URL = os.environ.get("FRIGATE_URL", "").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "30"))
OFFLINE_AFTER = float(os.environ.get("OFFLINE_AFTER", "120"))


def notify(title, message, tags, priority):
    headers = {"X-Title": title, "X-Tags": tags, "X-Priority": str(priority)}
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    req = urllib.request.Request(f"{UPSTREAM}/{NTFY_TOPIC}", data=message.encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20):
            pass
        log.info("watcher sent: %s - %s", title, message)
        return True
    except Exception as e:
        log.warning("watcher failed to send '%s': %s", title, e)
        return False


def fmt_duration(seconds):
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{max(minutes, 1)} min"
    return f"{minutes // 60}h {minutes % 60}m"


class Watcher:
    """Tracks one thing that can go down (a camera or Frigate itself)."""

    def __init__(self):
        self.bad_since = None  # monotonic time it was first seen down
        self.alerted = False

    def update(self, ok, now):
        """Returns 'offline', 'online' or None, depending on what needs announcing."""
        if ok:
            if self.alerted:
                return "online"
            self.bad_since = None
            return None
        if self.bad_since is None:
            self.bad_since = now
        if not self.alerted and now - self.bad_since >= OFFLINE_AFTER:
            return "offline"
        return None

    def mark(self, event):
        if event == "offline":
            self.alerted = True
        else:
            self.alerted, self.bad_since = False, None


def watch():
    frigate, cameras = Watcher(), {}
    log.info("watcher polling %s every %ss, offline after %ss", FRIGATE_URL, POLL_INTERVAL, OFFLINE_AFTER)
    while True:
        try:
            poll(frigate, cameras)
        except Exception:
            log.exception("watcher poll crashed")
        time.sleep(POLL_INTERVAL)


def poll(frigate, cameras):
    now = time.monotonic()
    try:
        with urllib.request.urlopen(f"{FRIGATE_URL}/api/stats", timeout=10) as resp:
            stats = json.load(resp)["cameras"]
    except Exception as e:
        stats = None
        log.debug("stats poll failed: %s", e)

    event = frigate.update(stats is not None, now)
    if event == "offline":
        if notify("Frigate unreachable", f"No response from Frigate for {fmt_duration(now - frigate.bad_since)}. "
                  "Detection and alerts are not working.", "rotating_light", 5):
            frigate.mark(event)
    elif event == "online":
        if notify("Frigate is back", f"Frigate is responding again (was down {fmt_duration(now - frigate.bad_since)}).",
                  "white_check_mark", 3):
            frigate.mark(event)

    # Camera states are only meaningful when Frigate answered
    if stats is not None:
        changes = {"offline": [], "online": []}
        for name, cam in stats.items():
            w = cameras.setdefault(name, Watcher())
            event = w.update((cam.get("camera_fps") or 0) > 0, now)
            if event:
                changes[event].append((name, w))

        down = changes["offline"]
        if down:
            names = ", ".join(n for n, _ in down)
            if len(down) == len(stats) > 1:
                title, msg = f"All {len(down)} cameras offline", f"No video from {names}. Is the NVR down?"
            else:
                title = f"Camera offline: {names}" if len(down) == 1 else f"{len(down)} cameras offline"
                msg = f"No video from {names} for {fmt_duration(now - min(w.bad_since for _, w in down))}."
            if notify(title, msg, "warning,video_camera", 4):
                for _, w in down:
                    w.mark("offline")

        up = changes["online"]
        if up:
            names = ", ".join(n for n, _ in up)
            title = f"Camera back online: {names}" if len(up) == 1 else f"{len(up)} cameras back online"
            msg = "; ".join(f"{n} (was down {fmt_duration(now - w.bad_since)})" for n, w in up)
            if notify(title, msg, "white_check_mark", 3):
                for _, w in up:
                    w.mark("online")


if __name__ == "__main__":
    log.info("upstream=%s cooldown=%ss groups=%s", UPSTREAM, COOLDOWN, CAMERA_GROUPS)
    if FRIGATE_URL:
        threading.Thread(target=watch, daemon=True).start()
    lights.start()
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
