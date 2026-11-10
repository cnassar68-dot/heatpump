#!/usr/bin/env python3
"""Heat pump collector: reads each unit once a minute, keeps the history and serves the website.

  python collector.py run              collect and serve the website
  python collector.py check            read every value once and print it
  python collector.py dump UNIT 0 600  save a range of registers to a CSV file

It only reads. See README.txt.
"""
import argparse
import base64
import csv
import gzip
import json
import logging
import logging.handlers
import os
import secrets
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from modbus_read import MAX_REGISTERS, READ_HOLDING, ModbusError, ReadOnlyClient

HERE = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.dirname(HERE)
# Only these files are served, so the database and config can never be downloaded.
SITE_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/data.js": ("data.js", "text/javascript; charset=utf-8"),
}
SERVED_DAYS = 30
MIN_POLL_SECONDS = 10
RETRIES = 2
RETRY_PAUSE = 1.0
API_CACHE_SECONDS = 15
log = logging.getLogger("collector")


class ConfigError(Exception):
    pass


def load_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except OSError as err:
        raise ConfigError("Cannot open %s: %s" % (path, err))
    except ValueError as err:
        raise ConfigError("%s is not valid JSON: %s" % (path, err))


def load_config(path):
    cfg = load_json(path)
    base = os.path.dirname(os.path.abspath(path))
    cfg.setdefault("poll_seconds", 60)
    cfg.setdefault("offline_after_seconds", 300)
    cfg.setdefault("retention_days", 400)
    cfg.setdefault("listen", "127.0.0.1")
    cfg.setdefault("port", 8770)
    cfg.setdefault("web_password", "")
    cfg.setdefault("database", "data/history.db")
    cfg["database"] = os.path.join(base, cfg["database"])
    if cfg["poll_seconds"] < MIN_POLL_SECONDS:
        raise ConfigError("poll_seconds must be at least %d, to keep the unit's bus quiet" % MIN_POLL_SECONDS)
    if not cfg.get("units"):
        raise ConfigError("config has no units")
    if cfg["listen"] not in ("127.0.0.1", "localhost", "::1") and not cfg["web_password"]:
        raise ConfigError("Set web_password before making the website reachable from other devices")
    seen = set()
    for unit in cfg["units"]:
        for field in ("id", "name", "host", "port"):
            if field not in unit:
                raise ConfigError("A unit is missing '%s': %s" % (field, unit))
        if unit["id"] in seen:
            raise ConfigError("Two units use the id '%s'" % unit["id"])
        seen.add(unit["id"])
        unit.setdefault("site", "")
        unit.setdefault("slave", 1)
        unit.setdefault("framing", "mbap")
        unit["map"] = load_map(os.path.join(base, unit.get("register_map", "registers_cgk.json")))
    return cfg


def load_map(path):
    reg_map = load_json(path)
    keys = [r["key"] for r in reg_map["readings"]]
    for need in ("inlet", "outlet", "state", "actual_capacity"):
        if need not in keys:
            raise ConfigError("%s has no '%s' reading" % (path, need))
    reg_map["blocks"] = make_blocks(reg_map["readings"])
    return reg_map


def make_blocks(readings, largest_gap=20):
    """Group nearby addresses, so a poll is a few short reads rather than many."""
    blocks = []
    for reading in sorted(readings, key=lambda r: r["address"]):
        address = reading["address"]
        if blocks and address - blocks[-1]["end"] <= largest_gap and address - blocks[-1]["start"] < MAX_REGISTERS:
            blocks[-1]["end"] = address
        else:
            blocks.append({"start": address, "end": address, "required": False})
        if reading.get("required"):
            blocks[-1]["required"] = True
    return blocks


def signed16(value):
    return value - 0x10000 if value >= 0x8000 else value


def poll_unit(unit, timeout=5.0, pause=RETRY_PAUSE):
    """Read all blocks. Returns {address: raw}. Raises ModbusError if a required block fails."""
    raw = {}
    with ReadOnlyClient(unit["host"], unit["port"], unit["slave"], unit["framing"], timeout) as client:
        for block in unit["map"]["blocks"]:
            count = block["end"] - block["start"] + 1
            for attempt in range(RETRIES + 1):
                try:
                    values = client.read(READ_HOLDING, block["start"], count)
                    raw.update(zip(range(block["start"], block["end"] + 1), values))
                    break
                except ModbusError as err:
                    if attempt == RETRIES:
                        if block["required"]:
                            raise
                        log.debug("%s: optional registers %d-%d unavailable: %s", unit["id"], block["start"], block["end"], err)
                    else:
                        time.sleep(pause)
    return raw


def decode(reg_map, raw):
    """Turn raw registers into values. A value is None if missing or out of its valid range."""
    values, shown = {}, []
    for reading in reg_map["readings"]:
        rawvalue = raw.get(reading["address"])
        value = None
        fault = False
        if rawvalue is not None:
            value = round(signed16(rawvalue) * reading["scale"], 3)
            low, high = reading.get("min"), reading.get("max")
            if (low is not None and value < low) or (high is not None and value > high):
                value, fault = None, True
        values[reading["key"]] = value
        if value is None:
            text = "Sensor fault" if fault else "Not available"
        elif "text" in reading:
            text = reading["text"].get(str(int(value)), "Code %d" % value)
        else:
            text = ("%.*f %s" % (reading["decimals"], value, reading["unit"])).strip()
        shown.append({"group": reading["group"], "label": reading["label"], "value": text})
    return values, shown


def add_compressor_status(values, shown):
    capacity = values.get("actual_capacity")
    text = "Not available" if capacity is None else ("On" if capacity > 0 else "Off")
    shown.append({"group": "Compressor", "label": "Status", "value": text})


# Extra values kept per minute besides inlet/outlet: suction gas, discharge gas (°C), compressor frequency (Hz).
EXTRA_KEYS = ("suction", "discharge", "freq")


def series_point(row):
    """One stored row -> the website's point. Missing values are left out."""
    t, inlet, outlet, run = row[:4]
    point = {"t": t, "inlet": inlet, "outlet": outlet}
    if run is not None:
        point["run"] = run
    for key, value in zip(EXTRA_KEYS, row[4:]):
        if value is not None:
            point[key] = value
    return point


class Store:
    def __init__(self, path):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock, self.db:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("CREATE TABLE IF NOT EXISTS samples(unit TEXT, t INTEGER, inlet REAL, outlet REAL, run INTEGER, PRIMARY KEY(unit, t)) WITHOUT ROWID")
            self.db.execute("CREATE TABLE IF NOT EXISTS latest(unit TEXT PRIMARY KEY, contact INTEGER, state_code INTEGER, capacity REAL, readings TEXT)")
            self.db.execute("CREATE TABLE IF NOT EXISTS alarms(id INTEGER PRIMARY KEY, unit TEXT, t INTEGER, code TEXT, text TEXT, active INTEGER)")
            self.db.execute("CREATE TABLE IF NOT EXISTS units(unit TEXT PRIMARY KEY, name TEXT, site TEXT, product_key TEXT, seen INTEGER)")
            # Add columns to a database made before they existed.
            have = {row[1] for row in self.db.execute("PRAGMA table_info(samples)")}
            for column, kind in (("run", "INTEGER"),) + tuple((k, "REAL") for k in EXTRA_KEYS):
                if column not in have:
                    self.db.execute("ALTER TABLE samples ADD COLUMN %s %s" % (column, kind))

    def save(self, unit_id, t, values, shown, alarm_text):
        state = values.get("state")
        with self.lock, self.db:
            if values.get("inlet") is not None and values.get("outlet") is not None:
                run = values.get("run")
                run = None if run is None else int(bool(run))
                extras = [values.get(k) for k in EXTRA_KEYS]
                self.db.execute("INSERT OR REPLACE INTO samples(unit, t, inlet, outlet, run, %s) VALUES(?,?,?,?,?,%s)"
                                % (", ".join(EXTRA_KEYS), ",".join("?" * len(EXTRA_KEYS))),
                                [unit_id, t, values["inlet"], values["outlet"], run] + extras)
            self.db.execute("INSERT OR REPLACE INTO latest VALUES(?,?,?,?,?)",
                            (unit_id, t, None if state is None else int(state), values.get("actual_capacity"), json.dumps(shown)))
            open_alarm = self.db.execute("SELECT id FROM alarms WHERE unit=? AND active=1", (unit_id,)).fetchone()
            if alarm_text and not open_alarm:
                self.db.execute("INSERT INTO alarms(unit, t, code, text, active) VALUES(?,?,?,?,1)", (unit_id, t, "State", alarm_text))
            elif open_alarm and not alarm_text:
                self.db.execute("UPDATE alarms SET active=0 WHERE id=?", open_alarm)

    def save_units(self, units):
        """Remember each unit's name and model, so exports and restarts show names, not codes."""
        now = int(time.time() * 1000)
        with self.lock, self.db:
            for u in units:
                self.db.execute("INSERT OR REPLACE INTO units VALUES(?,?,?,?,?)",
                                (u["id"], u.get("name"), u.get("site"), u.get("product_key"), now))

    def known_units(self):
        with self.lock:
            rows = self.db.execute("SELECT unit, name, site, product_key FROM units ORDER BY lower(name)").fetchall()
        return [{"id": r[0], "name": r[1] or r[0][:8], "site": r[2] or "", "product_key": r[3] or "",
                 "map": {"alarm_state_codes": [2]}} for r in rows]

    def prune(self, before):
        with self.lock, self.db:
            self.db.execute("DELETE FROM samples WHERE t<?", (before,))
            self.db.execute("DELETE FROM alarms WHERE t<? AND active=0", (before,))

    def unit_data(self, unit_id, since):
        with self.lock:
            series = self.db.execute("SELECT t, inlet, outlet, run, %s FROM samples WHERE unit=? AND t>=? ORDER BY t"
                                     % ", ".join(EXTRA_KEYS), (unit_id, since)).fetchall()
            latest = self.db.execute("SELECT contact, state_code, capacity, readings FROM latest WHERE unit=?", (unit_id,)).fetchone()
            alarms = self.db.execute("SELECT t, code, text, active FROM alarms WHERE unit=? ORDER BY t DESC LIMIT 200", (unit_id,)).fetchall()
        return series, latest, alarms


def collect_once(cfg, unit, store, timeout=5.0, pause=RETRY_PAUSE):
    """One poll of one unit. Nothing is stored if the unit did not answer."""
    raw = poll_unit(unit, timeout, pause)
    values, shown = decode(unit["map"], raw)
    add_compressor_status(values, shown)
    state = values.get("state")
    alarm_text = None
    if state is not None and int(state) in unit["map"].get("alarm_state_codes", []):
        alarm_text = next(r["value"] for r in shown if r["label"] == "State")
    step = cfg["poll_seconds"] * 1000
    t = int(time.time() * 1000) // step * step
    store.save(unit["id"], t, values, shown, alarm_text)
    return values


def build_api(cfg, store, now_ms=None):
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    since = now - SERVED_DAYS * 86400000
    units = []
    for unit in cfg["units"]:
        series, latest, alarms = store.unit_data(unit["id"], since)
        status = "offline"
        readings = []
        if latest:
            contact, state_code, capacity, stored = latest
            readings = json.loads(stored)
            if now - contact <= cfg["offline_after_seconds"] * 1000:
                if state_code in unit["map"].get("alarm_state_codes", []):
                    status = "alarm"
                elif capacity:
                    status = "running"
                else:
                    status = "standby"
        units.append({
            "id": unit["id"], "name": unit["name"], "site": unit["site"], "status": status,
            "series": [series_point(row) for row in series],
            "readings": readings,
            "alarms": [{"t": t, "code": c, "text": x, "active": bool(a)} for t, c, x, a in alarms],
        })
    return {"live": True, "units": units}


class ApiCache:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self.lock = threading.Lock()
        self.body, self.made = None, 0.0

    def get(self):
        with self.lock:
            if self.body is None or time.monotonic() - self.made > API_CACHE_SECONDS:
                text = json.dumps(build_api(self.cfg, self.store), separators=(",", ":"))
                self.body = gzip.compress(text.encode("utf-8"), 6)
                self.made = time.monotonic()
            return self.body


def make_handler(cfg, cache):
    password = cfg["web_password"]

    class Handler(BaseHTTPRequestHandler):
        server_version = "HeatPumpMonitor"
        sys_version = ""

        def log_message(self, *_):
            pass

        def _authorised(self):
            if not password:
                return True
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return False
            try:
                _, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            except ValueError:
                return False
            return secrets.compare_digest(given.encode("utf-8"), password.encode("utf-8"))

        def _send(self, code, body, kind, gzipped=False):
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            if gzipped:
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if not self._authorised():
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="Heat Pump Monitor"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            path = self.path.split("?", 1)[0]
            if path == "/api/data":
                try:
                    body = cache.get()
                except Exception:
                    log.exception("Could not build the data")
                    self._send(500, b"Error", "text/plain")
                    return
                if "gzip" in self.headers.get("Accept-Encoding", ""):
                    self._send(200, body, "application/json", gzipped=True)
                else:
                    self._send(200, gzip.decompress(body), "application/json")
                return
            entry = SITE_FILES.get(path)
            if not entry:
                self._send(404, b"Not found", "text/plain")
                return
            with open(os.path.join(SITE_DIR, entry[0]), "rb") as handle:
                self._send(200, handle.read(), entry[1])

    return Handler


def poll_forever(cfg, unit, store, host_lock, stop):
    interval = cfg["poll_seconds"]
    failing = False
    while not stop.is_set():
        started = time.time()
        try:
            with host_lock:
                collect_once(cfg, unit, store)
            if failing:
                log.info("%s: answering again", unit["id"])
            failing = False
        except ModbusError as err:
            if not failing:
                log.warning("%s: %s", unit["id"], err)
            failing = True
        except Exception:
            log.exception("%s: unexpected error", unit["id"])
            failing = True
        stop.wait(max(1.0, interval - (time.time() - started)))


def prune_forever(cfg, store, stop):
    while not stop.is_set():
        try:
            store.prune(int(time.time() * 1000) - cfg["retention_days"] * 86400000)
        except sqlite3.Error:
            log.exception("Could not remove old readings")
        stop.wait(86400)


def start(cfg):
    """Start polling and the website. Returns (server, stop_event)."""
    store = Store(cfg["database"])
    stop = threading.Event()
    host_locks = {}
    for unit in cfg["units"]:
        # Units on one adapter share a bus, so they take turns.
        lock = host_locks.setdefault((unit["host"], unit["port"]), threading.Lock())
        threading.Thread(target=poll_forever, args=(cfg, unit, store, lock, stop), daemon=True).start()
    threading.Thread(target=prune_forever, args=(cfg, store, stop), daemon=True).start()
    server = ThreadingHTTPServer((cfg["listen"], cfg["port"]), make_handler(cfg, ApiCache(cfg, store)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, stop


def cmd_run(cfg):
    server, stop = start(cfg)
    log.info("Website: http://%s:%d  Reading %d unit(s) every %d s", cfg["listen"], server.server_address[1], len(cfg["units"]), cfg["poll_seconds"])
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop.set()
        server.shutdown()
    return 0


def cloud_config(path):
    """Build a run config from the cloud section plus top-level server settings."""
    raw = load_json(path)
    cloud = dict(raw.get("cloud", {}))
    base = os.path.dirname(os.path.abspath(path))
    cloud.setdefault("poll_seconds", raw.get("poll_seconds", 60))
    cloud.setdefault("snapshot_seconds", raw.get("snapshot_seconds", 12))
    cloud["offline_after_seconds"] = raw.get("offline_after_seconds", 300)
    cloud["retention_days"] = raw.get("retention_days", 400)
    cloud["listen"] = raw.get("listen", "127.0.0.1")
    cloud["port"] = raw.get("port", 8770)
    cloud["web_password"] = raw.get("web_password", "")
    cloud["database"] = os.path.join(base, raw.get("database", "data/history.db"))
    cloud.setdefault("units", [])
    if not cloud.get("username"):
        raise ConfigError("Set cloud username and password in config.json first")
    if cloud["listen"] not in ("127.0.0.1", "localhost", "::1") and not cloud["web_password"]:
        raise ConfigError("Set web_password before making the website reachable from other devices")
    return cloud


def cmd_cloud_run(config_path):
    """Serve the website from SPRSUN's cloud: poll each unit's live readings and store them."""
    import sprsun_source
    try:
        cfg = cloud_config(config_path)
    except ConfigError as err:
        print("Config problem: %s" % err, file=sys.stderr)
        return 2

    store = Store(cfg["database"])
    amaps = {}
    stop = threading.Event()
    cfg["units"] = store.known_units()  # units seen before, until the first poll answers

    # Serve immediately so the page is reachable; the first poll fills it in a few seconds.
    server = ThreadingHTTPServer((cfg["listen"], cfg["port"]), make_handler(cfg, ApiCache(cfg, store)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Website: http://%s:%d", cfg["listen"], cfg["port"])

    def poll_loop():
        failing = False
        while not stop.is_set():
            started = time.time()
            try:
                devices, n = sprsun_source.poll_once(cfg, store, amaps)
                # Every unit ever seen stays listed; one that stops answering shows "No connection".
                if devices:
                    cfg["units"] = store.known_units()
                if failing:
                    log.info("Cloud responding again")
                failing = False
                log.info("Updated %d unit(s)", n)
            except Exception as err:
                if not failing:
                    log.warning("Cloud poll failed: %s", err)
                failing = True
            stop.wait(max(5.0, cfg["poll_seconds"] - (time.time() - started)))

    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=prune_forever, args=(cfg, store, stop), daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        stop.set()
        server.shutdown()
    return 0


def cmd_check(cfg):
    result = 0
    for unit in cfg["units"]:
        print("\n%s (%s:%s, %s, device %s)" % (unit["name"], unit["host"], unit["port"], unit["framing"], unit["slave"]))
        try:
            raw = poll_unit(unit)
        except ModbusError as err:
            print("  NO ANSWER: %s" % err)
            result = 1
            continue
        values, shown = decode(unit["map"], raw)
        for reading, line in zip(unit["map"]["readings"], shown):
            print("  %-4d %-26s %-18s raw=%s" % (reading["address"], line["group"] + " / " + line["label"], line["value"], raw.get(reading["address"], "-")))
    print("\nCompare these with the unit's display. Do not trust the graph until they match.")
    return result


def cmd_dump(cfg, unit_id, first, last):
    unit = next((u for u in cfg["units"] if u["id"] == unit_id), None)
    if not unit:
        print("No unit with id '%s'" % unit_id)
        return 1
    out = os.path.join(HERE, "dump_%s_%d.csv" % (unit_id, int(time.time())))
    rows = 0
    with open(out, "w", newline="") as handle, ReadOnlyClient(unit["host"], unit["port"], unit["slave"], unit["framing"]) as client:
        writer = csv.writer(handle)
        writer.writerow(["address", "raw", "signed", "divided_by_10"])
        for start_at in range(first, last + 1, MAX_REGISTERS):
            count = min(MAX_REGISTERS, last - start_at + 1)
            try:
                for offset, value in enumerate(client.read(READ_HOLDING, start_at, count)):
                    writer.writerow([start_at + offset, value, signed16(value), signed16(value) / 10])
                    rows += 1
            except ModbusError as err:
                print("  %d-%d skipped: %s" % (start_at, start_at + count - 1, err))
            time.sleep(1)
    print("Saved %d registers to %s" % (rows, out))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only heat pump collector")
    parser.add_argument("--config", default=os.path.join(HERE, "config.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run")
    commands.add_parser("check")
    commands.add_parser("check-cloud")
    commands.add_parser("discover-socket")
    commands.add_parser("cloud-run")
    dump = commands.add_parser("dump")
    dump.add_argument("unit")
    dump.add_argument("first", type=int)
    dump.add_argument("last", type=int)
    args = parser.parse_args(argv)

    fmt = "%(asctime)s %(levelname)s %(message)s"
    if sys.stderr is not None:
        logging.basicConfig(level=logging.INFO, format=fmt)
    else:
        logging.getLogger().setLevel(logging.INFO)  # started without a window (pythonw)
    if args.command in ("run", "cloud-run"):
        # Also keep a log file, so a background run can still be checked.
        log_dir = os.path.join(HERE, "data")
        os.makedirs(log_dir, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(os.path.join(log_dir, "collector.log"),
                                                       maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter(fmt))
        logging.getLogger().addHandler(handler)

    if args.command == "cloud-run":
        return cmd_cloud_run(args.config)

    if args.command in ("check-cloud", "discover-socket"):
        # Reads SPRSUN's cloud with your app login. Runs on your machine, sends nothing to anyone else.
        import sprsun_cloud
        try:
            raw = load_json(args.config)
        except ConfigError as err:
            print("Config problem: %s" % err, file=sys.stderr)
            return 2
        try:
            if args.command == "check-cloud":
                sprsun_cloud.check_cloud(raw.get("cloud", {}))
            else:
                sprsun_cloud.discover_socket(raw.get("cloud", {}))
            return 0
        except sprsun_cloud.CloudError as err:
            print("Cloud problem: %s" % err, file=sys.stderr)
            return 1

    try:
        cfg = load_config(args.config)
    except ConfigError as err:
        print("Config problem: %s" % err, file=sys.stderr)
        return 2
    if args.command == "run":
        return cmd_run(cfg)
    if args.command == "check":
        return cmd_check(cfg)
    try:
        return cmd_dump(cfg, args.unit, args.first, args.last)
    except ModbusError as err:
        print("NO ANSWER: %s" % err)
        return 1


if __name__ == "__main__":
    sys.exit(main())
