#!/usr/bin/env python3
"""SPRSUN cloud reader (standard library only).

This signs in to SPRSUN's own cloud (the same one the Heat Pump app uses) with YOUR
app account, and reads your units' live values and temperature history. It never
changes a setting: it only calls the read endpoints below.

It runs on your own always-on machine. Your username and password stay in config.json
on that machine and are sent only to SPRSUN over HTTPS, exactly as the app sends them.

Endpoints (taken from the app, base https://m2m.sprsun.com/JCloud):
  POST /user/login                      header ApplicationKey   body {username, password=md5(pw)}
  POST /device/device-list?pageNo=N     header UserToken        body {}            -> your units
  GET  /device/product-menu             header UserToken
  GET  /statistics/list-curve-name?product_key=K   header UserToken   -> names of each curve
  POST /statistics/list-curve           header UserToken        body {...}         -> history curves
  POST /device/AlarmLog                 header UserToken        body {...}         -> alarms

First run `python collector.py check-cloud`. It logs in and prints the raw replies so the
inlet/outlet curves and the exact request fields can be confirmed against your real account.
"""
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_BASE = "https://m2m.sprsun.com/JCloud"
DEFAULT_APP_KEY = "6ee9d89011d5405a41292510afa3dc75"  # app-level constant shipped in the public app


class CloudError(Exception):
    pass


class SprsunCloud:
    def __init__(self, cfg):
        self.base = cfg.get("base_url", DEFAULT_BASE).rstrip("/")
        self.app_key = cfg.get("app_key", DEFAULT_APP_KEY)
        self.username = cfg.get("username", "")
        self.password = cfg.get("password", "")
        # Store either the plain password (hashed here) or its md5 directly.
        self.password_md5 = cfg.get("password_md5") or (
            hashlib.md5(self.password.encode("utf-8")).hexdigest() if self.password else "")
        self.content_type = cfg.get("content_type", "application/json")
        self.timeout = cfg.get("timeout", 20)
        self.token = None

    # ---- low-level request ----
    def _request(self, method, path, body=None, auth=True):
        url = self.base + path
        data = None
        headers = {"Accept": "application/json"}
        if auth:
            if not self.token:
                raise CloudError("Not logged in")
            headers["UserToken"] = self.token
        else:
            headers["ApplicationKey"] = self.app_key
        if body is not None:
            if self.content_type.startswith("application/json"):
                data = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"
            else:
                data = urllib.parse.urlencode(body).encode("utf-8")
                headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8", "replace")
            raise CloudError("HTTP %s from %s: %s" % (err.code, path, raw[:300]))
        except urllib.error.URLError as err:
            raise CloudError("Cannot reach %s (%s)" % (url, err))
        try:
            return json.loads(raw)
        except ValueError:
            raise CloudError("Reply from %s was not JSON: %s" % (path, raw[:300]))

    # ---- endpoints ----
    def login(self):
        if not self.username or not self.password_md5:
            raise CloudError("Set cloud username and password in config.json first")
        reply = self._request("POST", "/user/login",
                              {"username": self.username, "password": self.password_md5}, auth=False)
        code = reply.get("code")
        data = reply.get("data") or {}
        if code == 200 and data.get("token"):
            self.token = data["token"]
            return data
        rc = reply.get("result_code") or reply.get("msg") or reply
        if rc == "sis.api.NotExistUser":
            raise CloudError("Login refused: user not found. Check the username is your app login.")
        raise CloudError("Login refused (%s): %s" % (code, rc))

    def devices(self, page=1, body=None, extra_query=None):
        path = "/device/device-list?pageNo=%d" % page
        if extra_query:
            path += "&" + extra_query
        return self._request("POST", path, body or {})

    def product_menu(self):
        return self._request("GET", "/device/product-menu")

    def curve_names(self, product_key):
        return self._request("GET", "/statistics/list-curve-name?product_key=%s" %
                             urllib.parse.quote(str(product_key)))

    def curve(self, body):
        return self._request("POST", "/statistics/list-curve", body)

    def alarms(self, body=None):
        return self._request("POST", "/device/AlarmLog", body or {})


def _pretty(obj, limit=2000):
    # ensure_ascii=True so Windows consoles (cp1252) never crash on Chinese labels.
    text = json.dumps(obj, indent=2, ensure_ascii=True)
    return text if len(text) <= limit else text[:limit] + "\n  ... (truncated)"


def _collect(obj, keys):
    """Pull every value stored under any of `keys`, anywhere in a nested reply."""
    found = []
    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in keys and v not in (None, "", []):
                    found.append(v)
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(obj)
    return list(dict.fromkeys(found))


def check_cloud(cfg):
    """Log in and print the raw replies, so the data can be mapped to the website."""
    cloud = SprsunCloud(cfg)
    print("Logging in to %s as %s ..." % (cloud.base, cloud.username))
    info = cloud.login()
    print("  OK. uid=%s  token=%s...\n" % (info.get("uid"), str(cloud.token)[:12]))

    print("PRODUCT MENU (/device/product-menu):")
    try:
        menu = cloud.product_menu()
        print(_pretty(menu), "\n")
    except CloudError as err:
        menu = None
        print("  ", err, "\n")

    # device-list is denied without scope; it needs a product key. Try each product the account knows.
    product_keys = _collect(menu or {}, {"productKey", "product_key", "pk"})
    print("Trying device-list scoped to each of your %d product keys:" % len(product_keys))
    found_devices = []
    working_body_shape = None
    for key in product_keys:
        shapes = [
            ("body product_key", {"product_key": key, "pageNo": 1, "size": 50}),
            ("body productKey", {"productKey": key, "pageNo": 1, "size": 50}),
            ("query product_key", {"__query__": "product_key=%s" % key, "pageNo": 1, "size": 50}),
        ]
        for label, body in shapes:
            query = body.pop("__query__", None)
            try:
                reply = cloud.devices(page=1, body=body, extra_query=query)
            except CloudError as err:
                print("  %s (%s) -> %s" % (key[:16], label, err))
                continue
            code, rc = reply.get("code"), reply.get("result_code")
            rows = reply.get("data")
            n = len(rows) if isinstance(rows, list) else ("data" if rows else 0)
            print("  %s (%s) -> code=%s rc=%s rows=%s" % (key[:16], label, code, rc, n))
            if code == 200 and rows:
                found_devices.append((key, reply))
                working_body_shape = label
                break  # this product's devices found; next product
    print()

    if found_devices:
        print("YOUR DEVICES (working request: %s):" % working_body_shape)
        for key, reply in found_devices:
            print("--- product %s" % key)
            print(_pretty(reply, 1500))
        print()
        # Show the curve names for the first product that has devices.
        first_key = found_devices[0][0]
        print("CURVE NAMES for %s (/statistics/list-curve-name):" % first_key)
        try:
            print(_pretty(cloud.curve_names(first_key)), "\n")
        except CloudError as err:
            print("  ", err, "\n")
    else:
        print("No devices returned yet. The product scope still was not accepted;")
        print("send this output back and another request shape will be tried.\n")

    print("Done. Send this output back so your unit and its curves can be wired to the website.")


def discover_socket(cfg):
    """Log in, then listen on SPRSUN's websocket for your device list and live readings."""
    import sprsun_socket
    cloud = SprsunCloud(cfg)
    print("Logging in to %s as %s ..." % (cloud.base, cloud.username))
    info = cloud.login()
    uid = info.get("uid")
    print("  OK. uid=%s\n" % uid)

    url = cfg.get("socket_url", "wss://m2m.sprsun.com:447")
    app_secret = cfg.get("app_secret", sprsun_socket.APP_SECRET_DEFAULT)
    print("Connecting to %s and listening for 15 seconds ..." % url)
    try:
        messages, devices, readings = sprsun_socket.collect(
            url, cloud.app_key, app_secret, uid, cloud.token, seconds=15,
            verify=cfg.get("verify_tls", True))
    except sprsun_socket.SocketError as err:
        print("  Socket problem: %s" % err)
        if "certificate" in str(err).lower():
            print('  (If this is a certificate error, add  "verify_tls": false  to the cloud section and retry.)')
        return

    print("\nReceived %d messages. Commands seen: %s\n" %
          (len(messages), sorted({m.get("cmd") for m in messages if m.get("cmd")})))

    print("YOUR DEVICES (from the socket):")
    if devices:
        print(_pretty(list(devices.values())), "\n")
    else:
        print("  none seen yet\n")

    print("LIVE READINGS (attr_status.data per device):")
    if readings:
        for key, data in readings.items():
            print("--- device_key=%s  (%d values)" % (key, len(data)))
            print(_pretty(data, 2500))
    else:
        print("  none seen yet\n")

    # Save the full result next to the script so it can be read back in detail.
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "discover_result.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"devices": list(devices.values()), "readings": readings, "messages": messages},
                  fh, ensure_ascii=False, indent=1)
    print("\nFull result saved to: %s" % out_path)
    print("Done. Send this output back so the live readings can be wired to the website and graphs.")


if __name__ == "__main__":
    import sys
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "config.json"
    with open(cfg_path, encoding="utf-8") as fh:
        full = json.load(fh)
    check_cloud(full.get("cloud", {}))
