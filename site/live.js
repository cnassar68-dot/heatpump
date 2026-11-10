/*
 * Live mode: this phone reads the heat pumps straight from SPRSUN, every minute.
 *
 * The saved history still comes from api/data (written by the GitHub job). On top of it,
 * when a SPRSUN login is saved on this device, each refresh signs in to SPRSUN's cloud,
 * opens the same live socket the app uses, asks each unit for its current readings
 * (read-only: Authenticate + DeviceAttrStatusQuery, nothing that controls a unit),
 * and adds that reading to the page.
 *
 * The login is kept only in this browser: the username and the md5 of the password
 * (SPRSUN's login takes the md5). Nothing is sent anywhere except SPRSUN.
 */
(function () {
  var BASE = "https://m2m.sprsun.com/JCloud";
  var SOCKET = "wss://m2m.sprsun.com:447";
  var APP_KEY = "6ee9d89011d5405a41292510afa3dc75";
  var APP_SECRET = "1995995f02d056277d88c59e346e86e0";
  var STORE = "hp-live-login";

  // ---- md5 (SPRSUN's login wants md5 of the password) ----
  function md5(str) {
    function rl(n, c) { return (n << c) | (n >>> (32 - c)); }
    function add(a, b) { var l = (a & 0xffff) + (b & 0xffff); return (((a >> 16) + (b >> 16) + (l >> 16)) << 16) | (l & 0xffff); }
    function cmn(q, a, b, x, s, t) { return add(rl(add(add(a, q), add(x, t)), s), b); }
    function ff(a, b, c, d, x, s, t) { return cmn((b & c) | (~b & d), a, b, x, s, t); }
    function gg(a, b, c, d, x, s, t) { return cmn((b & d) | (c & ~d), a, b, x, s, t); }
    function hh(a, b, c, d, x, s, t) { return cmn(b ^ c ^ d, a, b, x, s, t); }
    function ii(a, b, c, d, x, s, t) { return cmn(c ^ (b | ~d), a, b, x, s, t); }
    var bytes = unescape(encodeURIComponent(str)), n = bytes.length, words = [], i;
    for (i = 0; i < n; i++) words[i >> 2] |= bytes.charCodeAt(i) << ((i % 4) * 8);
    words[n >> 2] |= 0x80 << ((n % 4) * 8);
    var total = (((n + 8) >> 6) + 1) * 16;
    for (i = 0; i < total; i++) words[i] = words[i] | 0;
    words[total - 2] = n * 8;
    var a = 1732584193, b = -271733879, c = -1732584194, d = 271733878;
    var S = [7, 12, 17, 22, 5, 9, 14, 20, 4, 11, 16, 23, 6, 10, 15, 21];
    var T = []; for (i = 0; i < 64; i++) T[i] = (Math.abs(Math.sin(i + 1)) * 4294967296) | 0;
    for (i = 0; i < total; i += 16) {
      var oa = a, ob = b, oc = c, od = d, j, g;
      for (j = 0; j < 64; j++) {
        var fn = j < 16 ? ff : j < 32 ? gg : j < 48 ? hh : ii;
        g = j < 16 ? j : j < 32 ? (5 * j + 1) % 16 : j < 48 ? (3 * j + 5) % 16 : (7 * j) % 16;
        var s = S[(j >> 4) * 4 + (j % 4)];
        var na = fn(a, b, c, d, words[i + g], s, T[j]);
        a = d; d = c; c = b; b = na;
      }
      a = add(a, oa); b = add(b, ob); c = add(c, oc); d = add(d, od);
    }
    function hex(x) { var out = ""; for (var k = 0; k < 4; k++) out += ((x >> (k * 8 + 4)) & 15).toString(16) + ((x >> (k * 8)) & 15).toString(16); return out; }
    return hex(a) + hex(b) + hex(c) + hex(d);
  }
  window.hpMd5 = md5;

  // ---- what each code means (same rules as the collector) ----
  var ROLES = {
    inlet: [["0x000E", "back_water_temp"], /inlet temp/i],
    outlet: [["0x0012", "0x00BD"], /outlet temp/i],
    hotwater: [["0x000F", "0x00C3"], /hot ?water temp/i],
    ambient: [["env_temp", "0x00BE"], /ambi|ext\.? ?temp/i],
    suction: [["0x0015", "0x00C0"], /suct\.? ?gas temp/i],
    discharge: [["0x001B", "0x00BF"], /exhaust temp|disch\.? ?gas temp/i],
    coil: [["0x0016"], /coil temp/i],
    evap: [["0x0028"], /evap/i],
    freq: [["0x001E"], /comp\.?\s*frequency/i],
    target_freq: [["0x0024"], /target freq/i],
    current: [["0x0023"], /comp\.?\s*current/i],
    capacity: [["0x00CC"], /actual cap/i],
    comp: [["0x0004_0"], /^\s*compressor\s*$/i],
    pump: [["0x0006_6", "2_0x0008"], /pump/i],
    power: [["power"], /off_on|on\/off/i],
    dcbus: [["0x0021"], /dc bus/i],
    heat_set: [["heat_set_temp"], null],
    cool_set: [["refd_set_temp"], null],
    dhw_set: [["water_heat_set_temp"], null],
    mode: [["coolheat_mode"], null]
  };
  var TEMP = { inlet: 1, outlet: 1, hotwater: 1, ambient: 1, suction: 1, discharge: 1, coil: 1, evap: 1, heat_set: 1, cool_set: 1, dhw_set: 1 };
  var READINGS = [
    ["Water", "Inlet temperature", "inlet", "°C"], ["Water", "Outlet temperature", "outlet", "°C"],
    ["Water", "Hot water temperature", "hotwater", "°C"], ["Water", "Circulation pump", "pump", null],
    ["Compressor", "Status", "comp", null], ["Compressor", "Frequency", "freq", "Hz"],
    ["Compressor", "Capacity", "capacity", "%"], ["Compressor", "Target frequency", "target_freq", "Hz"],
    ["Compressor", "Current", "current", "A"], ["Compressor", "Discharge gas temp", "discharge", "°C"],
    ["Compressor", "Suction gas temp", "suction", "°C"], ["Outdoor unit", "Ambient temperature", "ambient", "°C"],
    ["Outdoor unit", "Coil temperature", "coil", "°C"], ["Outdoor unit", "Evaporator temperature", "evap", "°C"],
    ["Setpoints", "Heating", "heat_set", "°C"], ["Setpoints", "Cooling", "cool_set", "°C"],
    ["Setpoints", "Hot water", "dhw_set", "°C"], ["System", "Power", "power", null], ["System", "DC bus voltage", "dcbus", "V"]
  ];
  var MODE = { "0": "Cooling", "1": "Heating", "2": "Hot water", "3": "Heating + hot water", "4": "Cooling + hot water" };
  var ALARM = /^\s*(Er\s?\d|E\d|AL\d)|fault|failure|protection/i;

  var maps = {};
  async function codeList(pk) {
    if (!pk) return {};
    if (!maps[pk]) {
      maps[pk] = fetch("maps/attrmap_" + pk + ".json").then(function (r) { return r.ok ? r.json() : {}; }).catch(function () { return {}; });
    }
    return maps[pk];
  }
  function isOn(v) { return v === true || v === 1 || v === "1" || v === "true" || v === "True"; }
  function num(v) { var n = parseFloat(v); return isNaN(n) ? null : n; }

  function decode(attrs, amap) {
    var roles = {};
    Object.keys(ROLES).forEach(function (role) {
      var cand = ROLES[role][0], pat = ROLES[role][1];
      for (var i = 0; i < cand.length; i++) {
        var key = cand[i];
        if (!(key in attrs)) continue;
        var label = (amap[key] || {}).label;
        if (pat && Object.keys(amap).length && (!label || !pat.test(label))) continue;
        roles[role] = key; break;
      }
    });
    function value(role) {
      if (!(role in roles)) return null;
      var v = num(attrs[roles[role]]);
      if (v !== null && TEMP[role] && Math.abs(v) > 150) v = v / 10;
      return v;
    }
    function flag(role) { return role in roles ? isOn(attrs[roles[role]]) : null; }
    var inlet = value("inlet"), outlet = value("outlet"), freq = value("freq"), cap = value("capacity");
    var comp = flag("comp");
    if (comp === null && cap !== null) comp = cap > 0;
    if (comp === null && freq !== null) comp = freq > 0;
    var shown = [];
    READINGS.forEach(function (r) {
      var group = r[0], label = r[1], role = r[2], unit = r[3];
      if (role === "comp") { if (comp !== null) shown.push({ group: group, label: label, value: comp ? "On" : "Off" }); return; }
      if (!(role in roles)) return;
      if (unit === null) { shown.push({ group: group, label: label, value: flag(role) ? "On" : "Off" }); return; }
      var n = value(role);
      shown.push({ group: group, label: label, value: n !== null ? n.toFixed(1) + " " + unit : String(attrs[roles[role]]) });
    });
    if (inlet !== null && outlet !== null) shown.splice(2, 0, { group: "Water", label: "Outlet minus inlet", value: (outlet - inlet).toFixed(1) + " °C" });
    if ("mode" in roles) { var m = String(attrs[roles.mode]); shown.push({ group: "System", label: "Mode", value: MODE[m] || "Mode " + m }); }
    var alarms = [];
    Object.keys(attrs).forEach(function (k) { var l = (amap[k] || {}).label || ""; if (isOn(attrs[k]) && l && ALARM.test(l)) alarms.push(l); });
    var running = !!comp || !!freq || !!cap;
    var r1 = function (v) { return v === null ? null : Math.round(v * 10) / 10; };
    return {
      point: { inlet: r1(inlet), outlet: r1(outlet), run: running ? 1 : 0, suction: value("suction"), discharge: value("discharge"), freq: freq },
      shown: shown, alarm: alarms.slice(0, 4).join("; "), running: running
    };
  }

  window.hpDecode = decode;

  // ---- SPRSUN sign-in and one live snapshot ----
  function saved() { try { return JSON.parse(localStorage.getItem(STORE) || "null"); } catch (e) { return null; } }
  function save(v) { try { if (v) localStorage.setItem(STORE, JSON.stringify(v)); else localStorage.removeItem(STORE); } catch (e) { /* not kept */ } }

  async function login(user, pwMd5) {
    var r = await fetch(BASE + "/user/login", {
      method: "POST", headers: { "ApplicationKey": APP_KEY, "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify({ username: user, password: pwMd5 })
    });
    var j = await r.json();
    if (j.code === 200 && j.data && j.data.token) return { uid: j.data.uid, token: j.data.token };
    throw new Error(j.result_code === "sis.api.NotExistUser" ? "User not found" : "Login refused");
  }

  function snapshot(uid, token, waitMs) {
    return new Promise(function (resolve, reject) {
      var ws, devices = {}, readings = {}, queried = {}, answered = {}, done = false;
      var timer = setTimeout(finish, waitMs || 10000);
      function finish() {
        if (done) return; done = true; clearTimeout(timer);
        try { ws.close(); } catch (e) { /* closed */ }
        resolve({ devices: devices, readings: readings });
      }
      function scan(o) {
        if (Array.isArray(o)) { o.forEach(scan); return; }
        if (!o || typeof o !== "object") return;
        if (o.device_key && !devices[o.device_key]) devices[o.device_key] = { device_key: o.device_key, device_mac: o.device_mac, product_key: o.product_key, device_name: o.device_name };
        Object.keys(o).forEach(function (k) { scan(o[k]); });
      }
      try { ws = new WebSocket(SOCKET); } catch (e) { reject(e); return; }
      ws.onopen = function () {
        ws.send(JSON.stringify({ cmd: "Authenticate", sn: 1, app_info: { app_key: APP_KEY, app_secret: APP_SECRET },
          user_info: { uid: uid, token: token }, log_clear: false, lan_disable: false, diff_report: false,
          targeting_push: true, no_devStatus_after_DevList: true }));
      };
      ws.onerror = function () { if (!done) { done = true; clearTimeout(timer); reject(new Error("Live connection failed")); } };
      ws.onclose = function () { finish(); };
      ws.onmessage = function (ev) {
        var msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
        if (msg.cmd && /Authenticate/.test(msg.cmd) && msg.result_code && !/Success/i.test(msg.result_code)) {
          done = true; clearTimeout(timer); try { ws.close(); } catch (e) { /* */ }
          reject(new Error("token")); return;
        }
        scan(msg);
        if (msg.attr_status && msg.device_key) {
          var data = msg.attr_status.data || {};
          readings[msg.device_key] = Object.assign(readings[msg.device_key] || {}, data);
          if (Object.keys(data).length > 20) answered[msg.device_key] = true; // a full answer, not a small push
        }
        Object.keys(devices).forEach(function (k) {
          if (queried[k]) return;
          var d = devices[k];
          ws.send(JSON.stringify({ cmd: "DeviceAttrStatusQuery", sn: 1, device_key: k, device_mac: d.device_mac, product_key: d.product_key }));
          queried[k] = true;
        });
        var keys = Object.keys(queried);
        if (keys.length && keys.every(function (k) { return answered[k]; })) finish();
      };
    });
  }

  var session = null;          // {uid, token}
  var livePoints = {};         // device_key -> points read live on this phone, newest last
  var status = { text: "", ok: false };

  async function liveRead() {
    var cred = saved();
    if (!cred) return null;
    if (!session) session = await login(cred.user, cred.pw);
    try {
      return await snapshot(session.uid, session.token);
    } catch (err) {
      if (err.message !== "token") throw err;
      session = await login(cred.user, cred.pw); // the token expired: sign in again once
      return await snapshot(session.uid, session.token);
    }
  }

  var loadHistory = window.loadHeatPumpData;
  window.loadHeatPumpData = async function () {
    var data = await loadHistory();
    if (!saved()) { setButton(); return data; }
    var snap;
    try {
      snap = await liveRead();
      status = { text: "Live from SPRSUN · " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }), ok: true };
    } catch (err) {
      status = { text: "Live reading failed (" + err.message + "). Showing saved readings.", ok: false };
      if (/User not found|Login refused/.test(err.message)) session = null;
      setButton(); return data;
    }
    var now = Math.floor(Date.now() / 60000) * 60000;
    for (var key in snap.readings) {
      var dev = snap.devices[key] || {};
      var res = decode(snap.readings[key], await codeList(dev.product_key));
      var unit = data.units.find(function (u) { return u.id === key; });
      if (!unit) {
        unit = { id: key, name: dev.device_name || key.slice(0, 8), site: dev.device_mac ? "MAC " + dev.device_mac : "", series: [], readings: [], alarms: [] };
        data.units.push(unit);
      }
      var pts = livePoints[key] = (livePoints[key] || []).filter(function (p) { return p.t !== now; });
      pts.push(Object.assign({ t: now }, res.point));
      var lastSaved = unit.series.length ? unit.series[unit.series.length - 1].t : 0;
      livePoints[key] = pts.filter(function (p) { return p.t > lastSaved; });
      unit.series = unit.series.concat(livePoints[key]);
      unit.readings = res.shown;
      unit.status = res.alarm ? "alarm" : res.running ? "running" : "standby";
    }
    data.live = true;
    setButton();
    return data;
  };

  // ---- the Live button and its sign-in box ----
  var btn, box;
  function setButton() {
    if (!btn) return;
    var on = !!saved();
    btn.textContent = on ? (status.ok ? "● " : "○ ") + (status.text || "Live: starting…") : "Live from phone: off";
    btn.title = on ? "Tap to sign out of live mode on this phone" : "Tap to read the unit directly from SPRSUN on this phone";
  }
  function el(tag, attrs, kids) {
    var e = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (k) { if (k === "text") e.textContent = attrs[k]; else e.setAttribute(k, attrs[k]); });
    (kids || []).forEach(function (k) { e.appendChild(k); });
    return e;
  }
  function openBox() {
    if (box) { box.hidden = false; return; }
    var user = el("input", { type: "text", autocomplete: "username", placeholder: "SPRSUN app username" });
    var pass = el("input", { type: "password", autocomplete: "current-password", placeholder: "SPRSUN app password" });
    var msg = el("p", { "class": "live-msg" });
    var go = el("button", { type: "submit", text: "Start live" });
    var cancel = el("button", { type: "button", "class": "ghost", text: "Cancel" });
    var form = el("form", { "class": "live-box" }, [
      el("h2", { text: "Live readings on this phone" }),
      el("p", { text: "Sign in with the same login as the SPRSUN Heat Pump app. It is kept only on this phone and sent only to SPRSUN. The page will then read the unit itself every minute." }),
      user, pass, msg, el("div", { "class": "live-actions" }, [cancel, go])
    ]);
    box = el("div", { "class": "live-overlay" }, [form]);
    cancel.onclick = function () { box.hidden = true; };
    form.onsubmit = async function (ev) {
      ev.preventDefault();
      msg.textContent = "Signing in…";
      try {
        var pw = md5(pass.value);
        session = await login(user.value.trim(), pw);
        save({ user: user.value.trim(), pw: pw });
        pass.value = ""; msg.textContent = ""; box.hidden = true;
        status = { text: "Live: reading…", ok: true }; setButton();
        if (window.hpRefresh) window.hpRefresh();
      } catch (err) { msg.textContent = "Could not sign in: " + err.message; }
    };
    document.body.appendChild(box);
    user.focus();
  }
  document.addEventListener("DOMContentLoaded", function () {
    var foot = document.querySelector(".side-foot");
    btn = el("button", { type: "button", "class": "ghost live-btn" });
    btn.onclick = function () {
      if (saved()) {
        if (confirm("Stop live readings on this phone and forget the login?")) { save(null); session = null; livePoints = {}; status = { text: "", ok: false }; setButton(); if (window.hpRefresh) window.hpRefresh(); }
      } else openBox();
    };
    if (foot) foot.insertBefore(btn, foot.firstChild); else document.body.appendChild(btn);
    setButton();
  });
})();
