(function () {
  var MIN = 60000, HOUR = 60 * MIN, DAY = 24 * HOUR;
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var SVG = "http://www.w3.org/2000/svg";
  var REFRESH_MS = 60000;

  var VIEWS = {
    minutes: { name: "Minutes", note: "Last 60 minutes, one point per minute.", col: "Time", band: false },
    hours: { name: "Hours", note: "Last 24 hours, one point per hour (average).", col: "Hour", band: true },
    month: { name: "Month", note: "Last 30 days, one point per day (average).", col: "Day", band: true }
  };
  var STATUS = {
    running: { icon: "●", text: "Running" },
    standby: { icon: "○", text: "Standby" },
    alarm: { icon: "▲", text: "Alarm" },
    offline: { icon: "✕", text: "No connection" }
  };

  var state = { data: null, view: "minutes", numbersOpen: false, charts: [] };
  var page = document.getElementById("page");
  var tip = document.getElementById("tip");

  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function hhmm(t) { var d = new Date(t); return pad(d.getHours()) + ":" + pad(d.getMinutes()); }
  function dayName(t) { var d = new Date(t); return d.getDate() + " " + MONTHS[d.getMonth()]; }
  function dateTime(t) { return dayName(t) + ", " + hhmm(t); }
  function deg(v) { return v.toFixed(1) + " °C"; }

  // Small element builder. Data always goes in as text, never as HTML.
  function h(tag, props) {
    var node = document.createElement(tag);
    Object.keys(props || {}).forEach(function (k) {
      if (k === "text") node.textContent = props[k];
      else if (k === "class") node.className = props[k];
      else if (k === "onclick") node.addEventListener("click", props[k]);
      else node.setAttribute(k, props[k]);
    });
    for (var i = 2; i < arguments.length; i++) {
      if (arguments[i]) node.appendChild(arguments[i]);
    }
    return node;
  }
  function svg(name, attrs, parent) {
    var node = document.createElementNS(SVG, name);
    Object.keys(attrs).forEach(function (k) { node.setAttribute(k, attrs[k]); });
    if (parent) parent.appendChild(node);
    return node;
  }
  function pill(status) {
    var s = STATUS[status] || STATUS.standby;
    return h("span", { "class": "pill " + status }, h("i", { text: s.icon, "aria-hidden": "true" }), h("span", { text: s.text }));
  }
  function tile(label, value) {
    return h("div", { "class": "tile" }, h("span", { "class": "tile-label", text: label }), h("span", { "class": "tile-value", text: value }));
  }
  function lastOf(unit) { return unit.series[unit.series.length - 1]; }

  /* ---------- turn raw readings into the points of each view ---------- */

  function summarise(bucket, t) {
    var out = { t: t };
    ["inlet", "outlet", "suction", "discharge", "freq"].forEach(function (key) {
      var vals = bucket.map(function (r) { return r[key]; }).filter(function (v) { return typeof v === "number" && isFinite(v); });
      if (!vals.length) return;
      var sum = 0, lo = Infinity, hi = -Infinity;
      vals.forEach(function (v) { sum += v; lo = Math.min(lo, v); hi = Math.max(hi, v); });
      out[key] = sum / vals.length;
      out[key + "Min"] = lo;
      out[key + "Max"] = hi;
    });
    var runVals = bucket.filter(function (r) { return r.run !== undefined && r.run !== null; });
    if (runVals.length) out.run = runVals.reduce(function (s, r) { return s + (r.run ? 1 : 0); }, 0) / runVals.length;
    return out;
  }
  function group(series, keyOf) {
    var points = [], current = null, bucket = [];
    series.forEach(function (r) {
      var k = keyOf(r.t);
      if (k !== current) {
        if (bucket.length) points.push(summarise(bucket, current));
        current = k;
        bucket = [];
      }
      bucket.push(r);
    });
    if (bucket.length) points.push(summarise(bucket, current));
    return points;
  }
  function pointsFor(view, series) {
    if (!series.length) return [];
    var end = series[series.length - 1].t;
    if (view === "minutes") return series.filter(function (r) { return r.t > end - 60 * MIN; });
    if (view === "hours") {
      var from = Math.floor(end / HOUR) * HOUR - 23 * HOUR;
      return group(series.filter(function (r) { return r.t >= from; }), function (t) { return Math.floor(t / HOUR) * HOUR; });
    }
    var start = new Date(end);
    start.setHours(0, 0, 0, 0);
    start.setDate(start.getDate() - 29);
    return group(series.filter(function (r) { return r.t >= start.getTime(); }), function (t) {
      var d = new Date(t);
      d.setHours(0, 0, 0, 0);
      return d.getTime();
    });
  }
  function timeText(view, t) {
    if (view === "minutes") return dateTime(t);
    if (view === "hours") return dayName(t) + ", " + hhmm(t) + " to " + hhmm(t + HOUR);
    return dayName(t);
  }

  /* ---------- chart ---------- */

  function timeTicks(view, x0, x1, width) {
    var ticks = [];
    if (view === "minutes") {
      var stepM = (width < 480 ? 20 : 10) * MIN;
      for (var t = Math.ceil(x0 / stepM) * stepM; t <= x1; t += stepM) ticks.push({ t: t, label: hhmm(t) });
    } else if (view === "hours") {
      var stepH = width < 480 ? 6 : 3;
      var d = new Date(x0);
      d.setMinutes(0, 0, 0);
      for (; d.getTime() <= x1; d.setHours(d.getHours() + 1)) {
        if (d.getTime() >= x0 && d.getHours() % stepH === 0) ticks.push({ t: d.getTime(), label: pad(d.getHours()) + ":00" });
      }
    } else {
      var every = width < 480 ? 10 : 5;
      for (var i = 0; x1 - i * every * DAY >= x0; i++) {
        var back = new Date(x1);
        back.setDate(back.getDate() - i * every);
        ticks.unshift({ t: back.getTime(), label: dayName(back.getTime()) });
      }
    }
    return ticks;
  }
  function valueTicks(lo, hi) {
    var step = [1, 2, 5, 10, 20].find(function (s) { return (hi - lo) / s <= 5; }) || 50;
    var from = Math.floor(lo / step) * step, to = Math.ceil(hi / step) * step;
    if (to === from) to += step;
    var list = [];
    for (var v = from; v <= to + 1e-9; v += step) list.push(v);
    return list;
  }
  // Split a series where readings are missing, so the line does not bridge a gap.
  function segments(points) {
    if (points.length < 2) return [points];
    var gaps = points.slice(1).map(function (p, i) { return p.t - points[i].t; }).sort(function (a, b) { return a - b; });
    var limit = gaps[Math.floor(gaps.length / 2)] * 2.5;
    var out = [[points[0]]];
    for (var i = 1; i < points.length; i++) {
      if (points[i].t - points[i - 1].t > limit) out.push([]);
      out[out.length - 1].push(points[i]);
    }
    return out;
  }
  // What each chart on the unit page shows. Each chart has one unit (one y axis).
  var CHARTS = [
    { id: "water", title: "Water temperature over time", unit: "°C", diff: true, strip: true,
      series: [{ key: "inlet", name: "Inlet", color: "--inlet" }, { key: "outlet", name: "Outlet", color: "--outlet" }] },
    { id: "gas", title: "Refrigerant gas temperature", unit: "°C",
      series: [{ key: "suction", name: "Suction gas", color: "--suction" }, { key: "discharge", name: "Discharge gas", color: "--discharge" }] },
    { id: "freq", title: "Compressor frequency", unit: "Hz", zero: true,
      series: [{ key: "freq", name: "Frequency", color: "--freq" }] }
  ];
  function isNum(v) { return typeof v === "number" && isFinite(v); }
  function fmt(v, unit) { return unit === "Hz" ? Math.round(v) + " Hz" : v.toFixed(1) + " " + unit; }
  function hasData(spec, series) {
    return spec.series.some(function (s) { return series.some(function (p) { return isNum(p[s.key]); }); });
  }

  function drawOne(c) {
    if (!c.el.isConnected) return;
    var el = c.el, points = c.points, spec = c.spec, view = VIEWS[state.view];
    el.textContent = "";
    c.X = null;
    var series = spec.series.filter(function (s) { return points.some(function (p) { return isNum(p[s.key]); }); });
    if (!points.length || !series.length) { el.appendChild(h("p", { "class": "empty", text: "No readings yet." })); return; }

    var W = el.clientWidth || 800, H = el.clientHeight || 380;
    var M = { top: 12, right: 92, bottom: spec.strip ? 42 : 28, left: 44 };
    var lo = Infinity, hi = -Infinity;
    points.forEach(function (p) {
      series.forEach(function (s) {
        (view.band ? [s.key + "Min", s.key + "Max"] : [s.key]).forEach(function (k) {
          if (isNum(p[k])) { lo = Math.min(lo, p[k]); hi = Math.max(hi, p[k]); }
        });
      });
    });
    if (spec.zero) lo = Math.min(lo, 0);
    var yTicks = valueTicks(lo, hi), y0 = yTicks[0], y1 = yTicks[yTicks.length - 1];
    var x0 = points[0].t, x1 = points[points.length - 1].t;
    if (x1 === x0) x1 = x0 + MIN;
    var plotW = W - M.left - M.right;
    var X = function (t) { return M.left + (t - x0) / (x1 - x0) * plotW; };
    var Y = function (v) { return H - M.bottom - (v - y0) / (y1 - y0) * (H - M.top - M.bottom); };

    var root = svg("svg", { viewBox: "0 0 " + W + " " + H, "aria-hidden": "true" }, el);
    yTicks.forEach(function (v, i) {
      svg("line", { x1: M.left, x2: W - M.right, y1: Y(v), y2: Y(v), stroke: i === 0 ? "var(--axis)" : "var(--grid)", "stroke-width": 1 }, root);
      svg("text", { x: M.left - 8, y: Y(v) + 4, "text-anchor": "end" }, root).textContent = v + (i === yTicks.length - 1 ? " " + spec.unit : "");
    });
    timeTicks(state.view, x0, x1, W).forEach(function (tick) {
      svg("line", { x1: X(tick.t), x2: X(tick.t), y1: H - M.bottom, y2: H - M.bottom + 4, stroke: "var(--axis)" }, root);
      svg("text", { x: X(tick.t), y: H - 8, "text-anchor": "middle" }, root).textContent = tick.label;
    });

    var parts = segments(points);
    // Only join points that have this value, so a missing reading leaves a gap.
    function runsOf(g, key) {
      var out = [[]];
      g.forEach(function (p) {
        if (isNum(p[key])) out[out.length - 1].push(p);
        else if (out[out.length - 1].length) out.push([]);
      });
      return out.filter(function (r) { return r.length; });
    }
    if (spec.diff) {
      // Shade the gap between the two lines: for water, that gap is the temperature difference.
      var a = spec.series[0].key, b = spec.series[1].key;
      parts.forEach(function (g) {
        g = g.filter(function (p) { return isNum(p[a]) && isNum(p[b]); });
        if (g.length < 2) return;
        var top = g.map(function (p) { return X(p.t) + "," + Y(p[b]); });
        var bot = g.slice().reverse().map(function (p) { return X(p.t) + "," + Y(p[a]); });
        svg("polygon", { points: top.concat(bot).join(" "), fill: "var(--ink-2)", opacity: 0.09 }, root);
      });
    }
    if (view.band) {
      series.forEach(function (s) {
        parts.forEach(function (g) {
          runsOf(g, s.key + "Min").forEach(function (r) {
            if (r.length < 2) return;
            var top = r.map(function (p) { return X(p.t) + "," + Y(p[s.key + "Max"]); });
            var bottom = r.slice().reverse().map(function (p) { return X(p.t) + "," + Y(p[s.key + "Min"]); });
            svg("polygon", { points: top.concat(bottom).join(" "), fill: "var(" + s.color + ")", opacity: 0.14 }, root);
          });
        });
      });
    }
    series.forEach(function (s) {
      parts.forEach(function (g) {
        runsOf(g, s.key).forEach(function (r) {
          if (r.length < 2) return;
          svg("polyline", {
            points: r.map(function (p) { return X(p.t) + "," + Y(p[s.key]); }).join(" "),
            fill: "none", stroke: "var(" + s.color + ")", "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round"
          }, root);
        });
      });
    });

    // Name each line at its right end, keeping names from overlapping.
    var ends = series.map(function (s) {
      var lastP = null;
      for (var i = points.length - 1; i >= 0; i--) { if (isNum(points[i][s.key])) { lastP = points[i]; break; } }
      return { s: s, p: lastP };
    }).filter(function (e) { return e.p; });
    ends.sort(function (m, n) { return n.p[n.s.key] - m.p[m.s.key]; });
    var prev = -Infinity;
    ends.forEach(function (e) {
      var cy = Y(e.p[e.s.key]);
      e.ly = Math.max(cy + 4, prev + 18);
      prev = e.ly;
      svg("circle", { cx: X(e.p.t), cy: cy, r: 4, fill: "var(" + e.s.color + ")", stroke: "var(--surface)", "stroke-width": 2 }, root);
      svg("text", { x: X(e.p.t) + 9, y: e.ly, "class": "end-label" }, root).textContent = e.s.name;
    });

    // Compressor on/off strip, just below the plot: filled where the compressor was running.
    if (spec.strip && points.some(function (p) { return isNum(p.run); })) {
      var sy = H - M.bottom + 8, sh = 9;
      svg("rect", { x: M.left, y: sy, width: plotW, height: sh, rx: 2, fill: "var(--wash)" }, root);
      points.forEach(function (p, i) {
        if (!p.run) return;
        var xA = X(p.t);
        var xB = i < points.length - 1 ? X(points[i + 1].t) : Math.min(W - M.right, xA + plotW / Math.max(1, points.length - 1));
        svg("rect", { x: xA, y: sy, width: Math.max(1.5, xB - xA), height: sh, fill: "var(--good)", opacity: 0.3 + 0.6 * p.run }, root);
      });
    }

    c.X = X;
    c.Y = Y;
    c.series = series;
    c.hair = svg("line", { y1: M.top, y2: H - M.bottom, stroke: "var(--muted)", "stroke-width": 1, visibility: "hidden" }, root);
    c.dots = series.map(function (s) {
      return { key: s.key, node: svg("circle", { r: 4, fill: "var(" + s.color + ")", stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden" }, root) };
    });
    var latest = ends.map(function (e) { return e.s.name + " " + fmt(e.p[e.s.key], spec.unit); }).join(", ");
    el.setAttribute("aria-label", spec.title + ". " + view.note + " Latest: " + latest + ". Use the left and right arrow keys to read each point.");
  }
  function drawChart() { state.charts.forEach(drawOne); }

  function tipRow(label, color, value, range) {
    var key = h("i", { "class": "key" });
    key.style.background = "var(" + color + ")";
    return h("div", { "class": "tip-row" }, key, h("strong", { text: value }), h("small", { text: range ? label + " (" + range + ")" : label }));
  }
  function showPoint(c, i) {
    if (!c || !c.X || !c.points[i]) return;
    hidePoint();
    var p = c.points[i], x = c.X(p.t), band = VIEWS[state.view].band, spec = c.spec;
    c.hover = i;
    c.hair.setAttribute("x1", x);
    c.hair.setAttribute("x2", x);
    c.hair.setAttribute("visibility", "visible");
    c.dots.forEach(function (d) {
      if (!isNum(p[d.key])) return;
      d.node.setAttribute("cx", x);
      d.node.setAttribute("cy", c.Y(p[d.key]));
      d.node.setAttribute("visibility", "visible");
    });
    tip.textContent = "";
    tip.appendChild(h("div", { "class": "tip-time", text: timeText(state.view, p.t) }));
    c.series.slice().reverse().forEach(function (s) {
      if (!isNum(p[s.key])) return;
      var range = band && isNum(p[s.key + "Min"]) ? fmt(p[s.key + "Min"], spec.unit) + " to " + fmt(p[s.key + "Max"], spec.unit) : "";
      tip.appendChild(tipRow(s.name, s.color, fmt(p[s.key], spec.unit), range));
    });
    if (spec.diff && isNum(p.inlet) && isNum(p.outlet)) tip.appendChild(tipRow("Difference", "--ink-2", deg(p.outlet - p.inlet), ""));
    if (spec.strip && isNum(p.run)) {
      var runText = state.view === "minutes" ? (p.run ? "On" : "Off") : Math.round(p.run * 100) + "% of the time";
      tip.appendChild(h("div", { "class": "tip-row" }, h("strong", { text: runText }), h("small", { text: "Compressor" })));
    }
    tip.hidden = false;
    var rect = c.el.getBoundingClientRect();
    var left = rect.left + x + 12;
    if (left + tip.offsetWidth > window.innerWidth - 8) left = rect.left + x - tip.offsetWidth - 12;
    tip.style.left = Math.max(8, left) + "px";
    tip.style.top = Math.max(8, rect.top + 8) + "px";
  }
  function hidePoint() {
    tip.hidden = true;
    (state.charts || []).forEach(function (c) {
      if (!c.hair) return;
      c.hover = null;
      c.hair.setAttribute("visibility", "hidden");
      c.dots.forEach(function (d) { d.node.setAttribute("visibility", "hidden"); });
    });
  }
  function nearest(c, clientX) {
    var px = clientX - c.el.getBoundingClientRect().left, best = 0, dist = Infinity;
    c.points.forEach(function (p, i) {
      var d = Math.abs(c.X(p.t) - px);
      if (d < dist) { dist = d; best = i; }
    });
    return best;
  }
  function chartElement(spec, points) {
    var el = h("div", { "class": "chart" + (spec.id === "water" ? "" : " chart-small"), tabindex: "0", role: "img" });
    var c = { el: el, points: points, spec: spec, hover: null };
    state.charts.push(c);
    el.addEventListener("pointermove", function (e) { if (c.X) showPoint(c, nearest(c, e.clientX)); });
    el.addEventListener("pointerdown", function (e) { if (c.X) showPoint(c, nearest(c, e.clientX)); });
    el.addEventListener("pointerleave", hidePoint);
    el.addEventListener("focus", function () { if (c.X) showPoint(c, points.length - 1); });
    el.addEventListener("blur", hidePoint);
    el.addEventListener("keydown", function (e) {
      if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
      e.preventDefault();
      var at = c.hover === null ? points.length - 1 : c.hover;
      showPoint(c, Math.max(0, Math.min(points.length - 1, at + (e.key === "ArrowLeft" ? -1 : 1))));
    });
    return el;
  }
  function sparkline(series) {
    var pts = pointsFor("hours", series), box = svg("svg", { "class": "spark", viewBox: "0 0 300 42", preserveAspectRatio: "none", "aria-hidden": "true" });
    if (pts.length < 2) return box;
    var lo = Infinity, hi = -Infinity;
    pts.forEach(function (p) { lo = Math.min(lo, p.inlet, p.outlet); hi = Math.max(hi, p.inlet, p.outlet); });
    var range = (hi - lo) || 1;
    [["inlet", "--inlet"], ["outlet", "--outlet"]].forEach(function (s) {
      svg("polyline", {
        points: pts.map(function (p, i) { return (i / (pts.length - 1) * 300) + "," + (38 - (p[s[0]] - lo) / range * 34); }).join(" "),
        fill: "none", stroke: "var(" + s[1] + ")", "stroke-width": 2, "stroke-linejoin": "round", "vector-effect": "non-scaling-stroke"
      }, box);
    });
    return box;
  }

  /* ---------- pages ---------- */

  function renderOverview() {
    var units = state.data.units;
    var running = units.filter(function (u) { return u.status === "running"; }).length;
    var withAlarm = units.filter(function (u) { return u.status === "alarm"; }).length;
    page.appendChild(h("div", { "class": "head" }, h("div", null, h("h1", { text: "Overview" }), h("p", { "class": "muted", text: "All heat pumps at a glance." }))));
    var offline = units.filter(function (u) { return u.status === "offline"; }).length;
    page.appendChild(h("div", { "class": "tiles" }, tile("Heat pumps", String(units.length)), tile("Running now", String(running)), tile("With active alarm", String(withAlarm)), tile("No connection", String(offline))));

    var grid = h("div", { "class": "units" });
    units.forEach(function (u) {
      var last = lastOf(u);
      var temps = h("div", { "class": "unit-temps" });
      [["Inlet", last ? deg(last.inlet) : "No data"], ["Outlet", last ? deg(last.outlet) : "No data"],
       ["Difference", last ? (last.outlet - last.inlet).toFixed(1) + " °C" : "No data"]].forEach(function (t) {
        temps.appendChild(h("div", null, h("span", { text: t[0] }), h("strong", { text: t[1] })));
      });
      grid.appendChild(h("a", { "class": "unit", href: "#/unit/" + encodeURIComponent(u.id) },
        h("div", { "class": "unit-top" },
          h("div", null, h("div", { "class": "unit-name", text: u.name }), h("div", { "class": "muted", text: u.site })),
          pill(u.status)),
        temps,
        h("div", null, sparkline(u.series), h("div", { "class": "spark-note", text: u.status === "offline"
          ? (last ? "Last reading " + dateTime(last.t) : "No readings yet")
          : "Last 24 hours: inlet (blue), outlet (orange)" }))));
    });
    page.appendChild(grid);
  }

  function alarmTable(rows, showUnit) {
    if (!rows.length) return h("p", { "class": "empty", text: "No alarms recorded." });
    var table = h("table", { "class": "left" });
    var cols = showUnit ? ["Time", "Heat pump", "Code", "Alarm", "State"] : ["Time", "Code", "Alarm", "State"];
    var head = table.createTHead().insertRow();
    cols.forEach(function (c) { head.appendChild(h("th", { text: c })); });
    var body = table.createTBody();
    rows.forEach(function (a) {
      var row = body.insertRow();
      var cells = [dateTime(a.t)];
      if (showUnit) cells.push(a.unit);
      cells.push(a.code, a.text, a.active ? "▲ Active" : "Cleared");
      cells.forEach(function (c) { row.insertCell().textContent = c; });
    });
    return h("div", { "class": "table-wrap" }, table);
  }
  function renderAlarms() {
    var rows = [];
    state.data.units.forEach(function (u) {
      u.alarms.forEach(function (a) { rows.push({ t: a.t, code: a.code, text: a.text, active: a.active, unit: u.name }); });
    });
    rows.sort(function (a, b) { return b.t - a.t; });
    page.appendChild(h("div", { "class": "head" }, h("div", null, h("h1", { text: "Alarms" }), h("p", { "class": "muted", text: "Active and past alarms for all heat pumps, newest first." }))));
    page.appendChild(h("section", { "class": "card" }, alarmTable(rows, true)));
  }

  // Columns for the table and CSV: each value the unit reports, with its unit.
  var COLUMNS = [
    { key: "inlet", label: "Inlet °C", csv: "inlet_c" }, { key: "outlet", label: "Outlet °C", csv: "outlet_c" },
    { key: "suction", label: "Suction gas °C", csv: "suction_gas_c" }, { key: "discharge", label: "Discharge gas °C", csv: "discharge_gas_c" },
    { key: "freq", label: "Compressor Hz", csv: "compressor_hz" }
  ];
  function columnsFor(points) {
    return COLUMNS.filter(function (col) { return points.some(function (p) { return isNum(p[col.key]); }); });
  }
  function cell(v, key) { return isNum(v) ? (key === "freq" ? String(Math.round(v)) : v.toFixed(1)) : ""; }

  function exportCsv(unit, points) {
    var band = VIEWS[state.view].band, cols = columnsFor(points);
    var head = ["time"];
    cols.forEach(function (col) { head.push(band ? col.csv + "_avg" : col.csv); if (band) head.push(col.csv + "_low", col.csv + "_high"); });
    var lines = [head.join(",")];
    points.forEach(function (p) {
      var row = [new Date(p.t).toISOString()];
      cols.forEach(function (col) {
        row.push(cell(p[col.key], col.key));
        if (band) row.push(cell(p[col.key + "Min"], col.key), cell(p[col.key + "Max"], col.key));
      });
      lines.push(row.join(","));
    });
    var url = URL.createObjectURL(new Blob([lines.join("\r\n")], { type: "text/csv" }));
    var a = h("a", { href: url, download: unit.name.replace(/[^\w-]+/g, "_") + "_" + state.view + ".csv" });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  function numbersTable(points) {
    var view = VIEWS[state.view], table = h("table"), cols = columnsFor(points);
    var head = table.createTHead().insertRow();
    head.appendChild(h("th", { text: view.col }));
    cols.forEach(function (col) { head.appendChild(h("th", { text: view.band ? col.label + " (avg)" : col.label })); });
    var body = table.createTBody();
    points.slice().reverse().forEach(function (p) {
      var row = body.insertRow();
      row.insertCell().textContent = timeText(state.view, p.t);
      cols.forEach(function (col) { row.insertCell().textContent = cell(p[col.key], col.key); });
    });
    return h("div", { "class": "table-wrap" }, table);
  }

  function renderUnit(id) {
    var unit = state.data.units.find(function (u) { return u.id === id; });
    if (!unit) {
      page.appendChild(h("a", { "class": "crumb", href: "#/", text: "← Overview" }));
      page.appendChild(h("h1", { text: "Heat pump not found" }));
      return;
    }
    var last = lastOf(unit), view = VIEWS[state.view], points = pointsFor(state.view, unit.series);
    page.appendChild(h("a", { "class": "crumb", href: "#/", text: "← Overview" }));
    page.appendChild(h("div", { "class": "head" },
      h("div", null,
        h("div", { "class": "title-row" }, h("h1", { text: unit.name }), pill(unit.status)),
        h("p", { "class": "muted", text: unit.site + (last ? " · Last reading " + dateTime(last.t) : "") }))));

    var comp = unit.readings.find(function (r) { return r.group === "Compressor" && r.label === "Status"; });
    page.appendChild(h("div", { "class": "tiles" },
      tile("Water inlet", last ? deg(last.inlet) : "No data"),
      tile("Water outlet", last ? deg(last.outlet) : "No data"),
      tile("Outlet minus inlet", last ? (last.outlet - last.inlet).toFixed(1) + " °C" : "No data"),
      tile("Compressor", comp ? comp.value : "No data")));

    var switcher = h("div", { "class": "views", role: "group", "aria-label": "Time view" });
    Object.keys(VIEWS).forEach(function (key) {
      switcher.appendChild(h("button", {
        type: "button", "aria-pressed": String(key === state.view), text: VIEWS[key].name,
        onclick: function () { state.view = key; render(); }
      }));
    });
    var legend = h("div", { "class": "legend" },
      h("span", null, h("i", { "class": "key key-inlet" }), document.createTextNode("Inlet")),
      h("span", null, h("i", { "class": "key key-outlet" }), document.createTextNode("Outlet")),
      h("span", null, h("i", { "class": "key key-diff" }), document.createTextNode("Difference (shaded)")),
      h("span", null, h("i", { "class": "key key-run" }), document.createTextNode("Compressor on")),
      view.band ? h("span", null, h("i", { "class": "key key-band" }), document.createTextNode("Shaded: lowest to highest")) : null);
    var chart = chartElement(CHARTS[0], points);
    var numbers = h("details", { "class": "numbers" }, h("summary", { text: "Show the numbers" }), numbersTable(points));
    numbers.open = state.numbersOpen;
    numbers.addEventListener("toggle", function () { state.numbersOpen = numbers.open; });

    page.appendChild(h("section", { "class": "card" },
      h("div", { "class": "card-head" },
        h("div", null, h("h2", { text: CHARTS[0].title }), h("p", { "class": "muted", text: view.note })),
        h("div", { "class": "tools" }, switcher, h("button", {
          type: "button", "class": "ghost", text: "Export CSV",
          onclick: function () { exportCsv(unit, points); }
        }))),
      legend, chart, numbers));

    // Gas temperatures and compressor frequency, on their own charts (different units),
    // following the same Minutes / Hours / Month choice.
    CHARTS.slice(1).forEach(function (spec) {
      if (!hasData(spec, unit.series)) return;
      var keys = h("div", { "class": "legend" });
      spec.series.forEach(function (s) {
        var key = h("i", { "class": "key" });
        key.style.background = "var(" + s.color + ")";
        keys.appendChild(h("span", null, key, document.createTextNode(s.name)));
      });
      if (view.band) keys.appendChild(h("span", null, h("i", { "class": "key key-band" }), document.createTextNode("Shaded: lowest to highest")));
      page.appendChild(h("section", { "class": "card" },
        h("div", null, h("h2", { text: spec.title }), h("p", { "class": "muted", text: view.note })),
        keys, chartElement(spec, points)));
    });
    drawChart();

    var groups = h("div", { "class": "groups" }), names = [];
    unit.readings.forEach(function (r) { if (names.indexOf(r.group) < 0) names.push(r.group); });
    names.forEach(function (name) {
      var list = h("dl");
      unit.readings.filter(function (r) { return r.group === name; }).forEach(function (r) {
        list.appendChild(h("div", null, h("dt", { text: r.label }), h("dd", { text: r.value })));
      });
      groups.appendChild(h("div", { "class": "group" }, h("h3", { text: name }), list));
    });
    page.appendChild(h("section", { "class": "card" }, h("h2", { text: "All readings" }), groups));

    var alarms = unit.alarms.slice().sort(function (a, b) { return b.t - a.t; });
    page.appendChild(h("section", { "class": "card" }, h("h2", { text: "Alarm history" }), alarmTable(alarms, false)));
  }

  function route() {
    var hash = location.hash.replace(/^#\/?/, "");
    if (hash === "alarms") return { name: "alarms" };
    if (hash.indexOf("unit/") === 0) return { name: "unit", id: decodeURIComponent(hash.slice(5)) };
    return { name: "overview" };
  }
  function render() {
    if (!state.data) return;
    var r = route(), scroll = window.scrollY;
    hidePoint();
    state.charts = [];
    page.textContent = "";
    if (r.name === "alarms") renderAlarms();
    else if (r.name === "unit") renderUnit(r.id);
    else renderOverview();
    window.scrollTo(0, scroll);

    document.title = (r.name === "alarms" ? "Alarms" : r.name === "unit" ? "Heat pump" : "Overview") + " | Heat Pump Monitor";
    document.querySelectorAll("[data-nav]").forEach(function (a) {
      var current = a.dataset.nav === r.name || (a.dataset.nav === "overview" && r.name === "unit");
      if (current) a.setAttribute("aria-current", "page");
      else a.removeAttribute("aria-current");
    });
    var active = 0;
    state.data.units.forEach(function (u) { active += u.alarms.filter(function (a) { return a.active; }).length; });
    var count = document.getElementById("alarm-count");
    count.hidden = !active;
    count.textContent = active;
  }
  function setSource(data) {
    var source = document.getElementById("source");
    var silent = data.units.filter(function (u) { return u.status === "offline"; }).length;
    source.classList.toggle("live", !!data.live && !silent);
    if (!data.live) source.textContent = "⚠ Sample data. This website is not connected to a heat pump yet.";
    else if (silent) source.textContent = "⚠ Live data, but " + silent + " of " + data.units.length + " heat pumps not answering. Readings shown for those are old.";
    else source.textContent = "✓ Live data";
  }
  async function refresh() {
    var data;
    try {
      data = await window.loadHeatPumpData();
    } catch (err) {
      var source = document.getElementById("source");
      source.classList.remove("live");
      source.textContent = "⚠ Could not get new readings. Showing the last ones received.";
      return;
    }
    state.data = data;
    setSource(data);
    render();
  }

  /* ---------- theme and start ---------- */

  var themeBtn = document.getElementById("theme");
  function isDark() {
    var set = document.documentElement.getAttribute("data-theme");
    if (set) return set === "dark";
    return window.matchMedia("(prefers-color-scheme: dark)").matches;
  }
  function labelTheme() { themeBtn.textContent = isDark() ? "Light mode" : "Dark mode"; }
  try {
    var saved = localStorage.getItem("hp-theme");
    if (saved) document.documentElement.setAttribute("data-theme", saved);
  } catch (e) { /* the page works without saved settings */ }
  labelTheme();
  themeBtn.addEventListener("click", function () {
    var next = isDark() ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("hp-theme", next); } catch (e) { /* not saved */ }
    labelTheme();
  });

  window.addEventListener("hashchange", function () { window.scrollTo(0, 0); render(); });
  if (window.ResizeObserver) new ResizeObserver(function () { drawChart(); }).observe(page);
  refresh();
  setInterval(refresh, REFRESH_MS);
})();
