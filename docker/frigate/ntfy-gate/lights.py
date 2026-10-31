"""Presence lights: turn a Zigbee switch on when a person stays in view, off again after a timeout.

Polls Frigate for in-progress person events on LIGHT_CAMERAS. Once someone has been present
(across any of those cameras, allowing short gaps) for PRESENCE_SECONDS, the switch is turned
on through Zigbee2MQTT and kept on until LIGHT_ON_SECONDS after the last trigger.

The switch is only turned off if this module turned it on: if it was already on (switched on by
hand), it is left alone. Ownership is persisted so a restart doesn't leave the lights on forever.

Env:
  MQTT_HOST          broker host (e.g. mosquitto)
  LIGHT_DEVICE       Zigbee2MQTT friendly name of the switch
  LIGHT_STATE_KEY    state attribute (default "state"; multi-gang switches use e.g. "state_l1")
  LIGHT_CAMERAS      comma-separated Frigate cameras
  PRESENCE_SECONDS   continuous presence needed before switching on (default 10)
  LIGHT_ON_SECONDS   how long to keep the light on after the last trigger (default 600)
  LIGHT_MIN_SCORE    minimum Frigate top_score to count a person (default 0.7)
  LIGHT_LAT/LIGHT_LON  if set, only switch on while the sun is below LIGHT_SUN_ELEVATION degrees
  LIGHT_SUN_ELEVATION  default 0 (sunset)
  LIGHT_STATE_FILE   where ownership is persisted (default /data/lights.json)
"""

import json
import logging
import math
import os
import threading
import time
import urllib.parse
import urllib.request

import paho.mqtt.client as mqtt

log = logging.getLogger("lights")

FRIGATE_URL = os.environ.get("FRIGATE_URL", "").rstrip("/")
MQTT_HOST = os.environ.get("MQTT_HOST", "mosquitto")
DEVICE = os.environ.get("LIGHT_DEVICE", "")
STATE_KEY = os.environ.get("LIGHT_STATE_KEY", "state")
CAMERAS = [c.strip() for c in os.environ.get("LIGHT_CAMERAS", "").split(",") if c.strip()]
PRESENCE_SECONDS = float(os.environ.get("PRESENCE_SECONDS", "10"))
ON_SECONDS = float(os.environ.get("LIGHT_ON_SECONDS", "600"))
MIN_SCORE = float(os.environ.get("LIGHT_MIN_SCORE", "0.7"))
LAT = os.environ.get("LIGHT_LAT")
LON = os.environ.get("LIGHT_LON")
SUN_ELEVATION = float(os.environ.get("LIGHT_SUN_ELEVATION", "0"))
STATE_FILE = os.environ.get("LIGHT_STATE_FILE", "/data/lights.json")
POLL = 2  # seconds
GAP = 5  # seconds without a person before presence counts as ended (walking between cameras)


def sun_elevation(lat, lon, t):
    """Solar elevation in degrees at unix time t (same approximation as suncalc.js)."""
    rad = math.pi / 180
    d = t / 86400 + 2440587.5 - 2451545.0
    m = rad * (357.5291 + 0.98560028 * d)
    c = rad * (1.9148 * math.sin(m) + 0.02 * math.sin(2 * m) + 0.0003 * math.sin(3 * m))
    lam = m + c + rad * 102.9372 + math.pi
    e = rad * 23.4397
    dec = math.asin(math.sin(e) * math.sin(lam))
    ra = math.atan2(math.sin(lam) * math.cos(e), math.cos(lam))
    hour_angle = rad * (280.16 + 360.9856235 * d) + rad * lon - ra
    phi = rad * lat
    return math.degrees(math.asin(math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.cos(hour_angle)))


def is_dark(t):
    if LAT is None or LON is None:
        return True
    return sun_elevation(float(LAT), float(LON), t) < SUN_ELEVATION


class Switch:
    def __init__(self):
        self.topic = f"zigbee2mqtt/{DEVICE}"
        self.state = None  # last reported "ON"/"OFF", None until the device reports
        self.owned, self.off_at = False, 0.0
        self.lock = threading.Lock()
        try:
            with open(STATE_FILE) as f:
                saved = json.load(f)
            self.owned, self.off_at = saved["owned"], saved["off_at"]
            log.info("restored owned=%s off_at=%s", self.owned, time.ctime(self.off_at))
        except (OSError, ValueError, KeyError):
            pass

        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ntfy-gate-lights")
        self.client.on_connect = self.on_connect
        self.client.on_message = self.on_message
        self.client.connect_async(MQTT_HOST)
        self.client.loop_start()  # reconnects automatically

    def on_connect(self, client, userdata, flags, reason, props):
        log.info("mqtt connected (%s), watching %s", reason, self.topic)
        client.subscribe(self.topic)
        client.publish(f"{self.topic}/get", json.dumps({STATE_KEY: ""}))  # ask for the current state

    def on_message(self, client, userdata, msg):
        try:
            state = json.loads(msg.payload).get(STATE_KEY)
        except ValueError:
            return
        if state not in ("ON", "OFF"):
            return
        with self.lock:
            self.state = state
            if state == "OFF" and self.owned:
                log.info("switched off externally, releasing")
                self.owned = False
                self.save()

    def set(self, state):
        self.client.publish(f"{self.topic}/set", json.dumps({STATE_KEY: state}), qos=1)

    def save(self):
        try:
            with open(STATE_FILE, "w") as f:
                json.dump({"owned": self.owned, "off_at": self.off_at}, f)
        except OSError as e:
            log.warning("could not persist state: %s", e)

    def trigger(self, now):
        with self.lock:
            if self.state == "ON" and not self.owned:
                return  # turned on by hand, not ours to manage
            if not self.owned:
                log.info("person present, turning %s on", DEVICE)
                self.set("ON")
                self.owned = True
            self.off_at = now + ON_SECONDS
            self.save()

    def tick(self, now):
        with self.lock:
            if self.owned and now >= self.off_at:
                log.info("timeout, turning %s off", DEVICE)
                self.set("OFF")
                self.owned = False
                self.save()


def active_people():
    """Start times of in-progress, confident person events on the light cameras."""
    query = urllib.parse.urlencode({"in_progress": 1, "labels": "person", "cameras": ",".join(CAMERAS), "limit": 50})
    with urllib.request.urlopen(f"{FRIGATE_URL}/api/events?{query}", timeout=5) as resp:
        events = json.load(resp)
    return [e["start_time"] for e in events
            if e.get("end_time") is None and (e.get("data") or {}).get("top_score", 1) >= MIN_SCORE]


def run():
    switch = Switch()
    since = last_seen = None
    skipped_daylight = False
    log.info("presence lights: device=%s cameras=%s after %ss, on for %ss, dark-only=%s",
             DEVICE, CAMERAS, PRESENCE_SECONDS, ON_SECONDS, LAT is not None)
    while True:
        now = time.time()
        try:
            starts = active_people()
        except Exception as e:
            log.debug("frigate poll failed: %s", e)
            starts = None

        if starts:
            if since is None:
                since = min(min(starts), now)
            last_seen = now
        elif starts is not None and last_seen is not None and now - last_seen > GAP:
            since = last_seen = None
            skipped_daylight = False

        if since is not None and last_seen == now and now - since >= PRESENCE_SECONDS:
            if is_dark(now):
                switch.trigger(now)
            elif not skipped_daylight:
                log.info("person present but it's daylight, not switching on")
                skipped_daylight = True

        switch.tick(now)
        time.sleep(POLL)


def start():
    if not (FRIGATE_URL and DEVICE and CAMERAS):
        log.info("presence lights disabled (FRIGATE_URL, LIGHT_DEVICE and LIGHT_CAMERAS required)")
        return
    threading.Thread(target=run, daemon=True).start()
