#!/usr/bin/env python3
"""Turn SPRSUN cloud/socket readings into the website's data, and run the live feed.

Flow: log in (REST) -> open the live socket -> full-snapshot query each device ->
map the attributes to inlet/outlet + readings -> store -> the website shows it.
Read-only throughout.

Different SPRSUN models name their values differently (a Chico controller calls inlet
water "0x000E", a Carel one "back_water_temp"). Each reading below is found by role:
the first candidate code the unit reports whose label in that model's code list
matches. A model's code list is downloaded from SPRSUN the first time it is seen.
"""
import json
import logging
import os
import re
import time
import urllib.request

import sprsun_cloud
import sprsun_socket

HERE = os.path.dirname(os.path.abspath(__file__))
log = logging.getLogger("collector")

# role: (candidate attribute codes, label pattern that confirms it in the model's code list)
ROLES = {
    "inlet": (["0x000E", "back_water_temp"], r"inlet temp"),
    "outlet": (["0x0012", "0x00BD"], r"outlet temp"),
    "hotwater": (["0x000F", "0x00C3"], r"hot ?water temp"),
    "ambient": (["env_temp", "0x00BE"], r"ambi|ext\.? ?temp"),
    "suction": (["0x0015", "0x00C0"], r"suct\.? ?gas temp"),
    "discharge": (["0x001B", "0x00BF"], r"exhaust temp|disch\.? ?gas temp"),
    "coil": (["0x0016"], r"coil temp"),
    "evap": (["0x0028"], r"evap"),
    "freq": (["0x001E"], r"comp\.?\s*frequency"),
    "target_freq": (["0x0024"], r"target freq"),
    "current": (["0x0023"], r"comp\.?\s*current"),
    "capacity": (["0x00CC"], r"actual cap"),
    "comp": (["0x0004_0"], r"^\s*compressor\s*$"),
    "pump": (["0x0006_6", "2_0x0008"], r"pump"),
    "power": (["power"], r"off_on|on/off"),
    "dcbus": (["0x0021"], r"dc bus"),
    "heat_set": (["heat_set_temp"], None),
    "cool_set": (["refd_set_temp"], None),
    "dhw_set": (["water_heat_set_temp"], None),
    "mode": (["coolheat_mode"], None),
}
TEMP_ROLES = {"inlet", "outlet", "hotwater", "ambient", "suction", "discharge", "coil", "evap", "heat_set", "cool_set", "dhw_set"}

# What to show, grouped, with a friendly label. Readings a model does not have are left out.
READINGS = [
    ("Water", "Inlet temperature", "inlet", "°C"),
    ("Water", "Outlet temperature", "outlet", "°C"),
    ("Water", "Hot water temperature", "hotwater", "°C"),
    ("Water", "Circulation pump", "pump", None),
    ("Compressor", "Status", "comp", None),
    ("Compressor", "Frequency", "freq", "Hz"),
    ("Compressor", "Capacity", "capacity", "%"),
    ("Compressor", "Target frequency", "target_freq", "Hz"),
    ("Compressor", "Current", "current", "A"),
    ("Compressor", "Discharge gas temp", "discharge", "°C"),
    ("Compressor", "Suction gas temp", "suction", "°C"),
    ("Outdoor unit", "Ambient temperature", "ambient", "°C"),
    ("Outdoor unit", "Coil temperature", "coil", "°C"),
    ("Outdoor unit", "Evaporator temperature", "evap", "°C"),
    ("Setpoints", "Heating", "heat_set", "°C"),
    ("Setpoints", "Cooling", "cool_set", "°C"),
    ("Setpoints", "Hot water", "dhw_set", "°C"),
    ("System", "Power", "power", None),
    ("System", "DC bus voltage", "dcbus", "V"),
]
MODE_TEXT = {"0": "Cooling", "1": "Heating", "2": "Hot water", "3": "Heating + hot water", "4": "Cooling + hot water"}
ALARM_LABEL = re.compile(r"^\s*(Er\s?\d|E\d|AL\d)|fault|failure|protection", re.I)

CODE_LIST_URLS = [
    "https://m2m.sprsun.com/pk_json/{pk}/{pk}.json",
    "https://fileopen.di-link.com/customer_files/sprsun/config_file/{pk}/{pk}.json",
]
_RETRY_SECONDS = 3600
_last_try = {}


def attr_map_path(product_key):
    return os.path.join(HERE, "attrmap_%s.json" % product_key)


def build_attr_map(pk_json):
    """SPRSUN's code list for a model -> {code: {label, unit, type}}."""
    amap = {}
    for addr in pk_json.get("addrs", []):
        for at in addr.get("attrs", []):
            key = at.get("name")
            if key:
                amap[key] = {"label": at.get("show_en") or at.get("show") or key,
                             "unit": (at.get("unit_en") or at.get("unit") or "").strip(), "type": at.get("type")}
    return amap


def load_attr_map(product_key, download=True):
    """The model's code list, downloaded once and kept next to the collector. {} if unavailable."""
    path = attr_map_path(product_key)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    if not download or not product_key or time.time() - _last_try.get(product_key, 0) < _RETRY_SECONDS:
        return {}
    _last_try[product_key] = time.time()
    for template in CODE_LIST_URLS:
        url = template.format(pk=product_key)
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                amap = build_attr_map(json.loads(resp.read().decode("utf-8")))
        except Exception as err:  # try the next copy
            log.warning("Could not download the code list for model %s from %s: %s", product_key, url, err)
            continue
        if amap:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(amap, fh, ensure_ascii=False)
            log.info("Saved the code list for new model %s (%d codes)", product_key, len(amap))
            return amap
    return {}


def resolve_roles(attrs, amap):
    """{role: attribute code} for the codes this unit reports, confirmed against its model's labels."""
    found = {}
    for role, (candidates, pattern) in ROLES.items():
        for key in candidates:
            if key not in attrs:
                continue
            label = (amap.get(key) or {}).get("label")
            if pattern and amap and (not label or not re.search(pattern, label, re.I)):
                continue  # same code, different meaning on this model
            found[role] = key
            break
    return found


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_on(value):
    return value in (True, 1, "1", "true", "True")


def decode(attrs, amap):
    """attrs: {attr_key: value} from the socket. Returns (values, shown, alarm_text)."""
    roles = resolve_roles(attrs, amap)

    def value(role):
        key = roles.get(role)
        if key is None:
            return None
        v = _num(attrs.get(key))
        if v is not None and role in TEMP_ROLES and abs(v) > 150:
            v = v / 10.0  # a model that sends tenths of a degree
        return v

    def flag(role):
        key = roles.get(role)
        return None if key is None else _is_on(attrs.get(key))

    inlet, outlet = value("inlet"), value("outlet")
    freq, capacity = value("freq"), value("capacity")
    comp_on = flag("comp")
    if comp_on is None and capacity is not None:
        comp_on = capacity > 0
    if comp_on is None and freq is not None:
        comp_on = freq > 0
    power_on = flag("power")

    shown = []
    for group, label, role, unit in READINGS:
        if role == "comp":
            if comp_on is None:
                continue
            shown.append({"group": group, "label": label, "value": "On" if comp_on else "Off"})
            continue
        if role not in roles:
            continue
        if unit is None:
            shown.append({"group": group, "label": label, "value": "On" if flag(role) else "Off"})
        else:
            num = value(role)
            text = ("%.1f %s" % (num, unit)).strip() if num is not None else str(attrs.get(roles[role]))
            shown.append({"group": group, "label": label, "value": text})
    if inlet is not None and outlet is not None:
        shown.insert(2, {"group": "Water", "label": "Outlet minus inlet", "value": "%.1f °C" % (outlet - inlet)})
    if "mode" in roles:
        mode = attrs.get(roles["mode"])
        shown.append({"group": "System", "label": "Mode", "value": MODE_TEXT.get(str(mode), "Mode %s" % mode)})

    # Alarms: any on/off value whose label marks it an error, fault or protection, and is on.
    active = []
    for key, val in attrs.items():
        if not _is_on(val):
            continue
        label = (amap.get(key) or {}).get("label", "")
        if label and ALARM_LABEL.search(label):
            active.append(label)
    alarm_text = "; ".join(active[:4]) if active else None

    running = bool(comp_on) or bool(freq) or bool(capacity)
    values = {
        "inlet": round(inlet, 1) if inlet is not None else None,
        "outlet": round(outlet, 1) if outlet is not None else None,
        "state": 2 if alarm_text else (1 if power_on or power_on is None else 0),
        "actual_capacity": (freq or capacity or 100) if running else 0,
        "run": 1 if running else 0,
        "suction": value("suction"),
        "discharge": value("discharge"),
        "freq": freq,
    }
    return values, shown, alarm_text


def snapshot(cfg, seconds=12):
    """Log in and get one full reading snapshot per device over the socket.
    Returns (devices, readings): devices {key: info}, readings {key: {attr: value}}."""
    cloud = sprsun_cloud.SprsunCloud(cfg)
    info = cloud.login()
    url = cfg.get("socket_url", "wss://m2m.sprsun.com:447")
    app_secret = cfg.get("app_secret", sprsun_socket.APP_SECRET_DEFAULT)
    _, devices, readings = sprsun_socket.collect(
        url, cloud.app_key, app_secret, info["uid"], cloud.token,
        seconds=seconds, verify=cfg.get("verify_tls", True))
    return devices, readings


def units_from_devices(devices):
    units = []
    for key, dev in devices.items():
        mac = dev.get("device_mac") or ""
        units.append({
            "id": key,
            "name": dev.get("device_name") or key[:8],
            "site": ("MAC " + mac) if mac else "",
            "product_key": dev.get("product_key", ""),
            "map": {"alarm_state_codes": [2]},
        })
    units.sort(key=lambda u: u["name"].lower())
    return units


def poll_once(cfg, store, amaps):
    devices, readings = snapshot(cfg, seconds=cfg.get("snapshot_seconds", 12))
    if devices and hasattr(store, "save_units"):
        store.save_units(units_from_devices(devices))
    step = cfg.get("poll_seconds", 60) * 1000
    t = int(time.time() * 1000) // step * step
    n = 0
    for key, attrs in readings.items():
        pk = devices.get(key, {}).get("product_key", "")
        amap = amaps.get(pk)
        if not amap:
            amap = load_attr_map(pk)
            if amap:
                amaps[pk] = amap
        values, shown, alarm_text = decode(attrs, amap or {})
        store.save(key, t, values, shown, alarm_text)
        n += 1
    return devices, n
