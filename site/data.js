/*
 * Where the website gets its data.
 *
 * loadHeatPumpData() is the only link between the page and the data source.
 * It asks the collector (collector/collector.py) at /api/data. When the page is not
 * served by the collector it makes clearly labelled SAMPLE data instead.
 * Once real data has been received, a failed request is an error and never falls
 * back to sample data.
 * The shape:
 *
 * {
 *   live: true,                  // false shows the "sample data" notice
 *   units: [{
 *     id: "abc", name: "Unit name", site: "Site name",
 *     status: "running" | "standby" | "alarm" | "offline",
 *     series: [{ t: <ms>, inlet: 41.2, outlet: 46.1 }, ...],   // oldest first, up to 30 days
 *     readings: [{ group: "Water", label: "Outdoor air", value: "12.5 °C" }, ...],
 *     alarms: [{ t: <ms>, code: "E01", text: "Description", active: true }, ...]
 *   }]
 * }
 */
(function () {
  var DAYS = 30;

  // Repeatable pseudo-random number from 0 to 1, so sample data stays the same between refreshes.
  function noise(n) {
    var x = Math.sin(n * 12.9898) * 43758.5453;
    return x - Math.floor(x);
  }
  function round1(v) { return Math.round(v * 10) / 10; }

  function makeUnit(cfg, nowMin) {
    var total = DAYS * 1440, set = cfg.setpoint, low = set - 5;
    var tank = set - 3, on = false, outlet = tank, series = [], last = null;

    for (var i = 0; i < total; i++) {
      var m = nowMin - total + 1 + i;
      var d = new Date(m * 60000);
      var hour = d.getHours() + d.getMinutes() / 60;
      var drawing = (hour >= 6.5 && hour < 8.5) || (hour >= 18 && hour < 21);
      var dayShift = noise(Math.floor(m / 1440) + cfg.seed) * 4 - 2;
      var air = 14 + dayShift + 6 * Math.sin((hour - 9) / 24 * 2 * Math.PI);
      var stopped = cfg.stopLastMinutes && i >= total - cfg.stopLastMinutes;

      tank -= 0.012 + (drawing ? cfg.use : 0);
      if (tank <= low) on = true;
      if (tank >= set) on = false;
      if (stopped) on = false;
      if (on) tank += 0.13;

      var inlet = tank - 1.5 + (noise(m + cfg.seed) - 0.5) * 0.15;
      var target = on ? inlet + 4.5 + 0.8 * noise(m + cfg.seed + 0.5) : inlet + 0.2;
      outlet += (target - outlet) * 0.3;
      series.push({ t: m * 60000, inlet: round1(inlet), outlet: round1(outlet) });
      last = { air: air, on: on, tank: tank, freq: on ? 35 + (set - tank) * 7 : 0, inlet: inlet, outlet: outlet };
    }

    var power = last.on ? 1.2 + last.freq * 0.02 : 0.05;
    var alarms = cfg.alarms.map(function (a) {
      return { t: (nowMin - a.minutesAgo) * 60000, code: a.code, text: a.text, active: a.active };
    });
    var active = alarms.some(function (a) { return a.active; });

    return {
      id: cfg.id, name: cfg.name, site: cfg.site,
      status: active ? "alarm" : (last.on ? "running" : "standby"),
      series: series,
      alarms: alarms,
      readings: [
        { group: "Water", label: "Inlet temperature", value: last.inlet.toFixed(1) + " °C" },
        { group: "Water", label: "Outlet temperature", value: last.outlet.toFixed(1) + " °C" },
        { group: "Water", label: "Tank temperature", value: last.tank.toFixed(1) + " °C" },
        { group: "Water", label: "Setpoint", value: set.toFixed(1) + " °C" },
        { group: "Compressor", label: "Status", value: last.on ? "On" : "Off" },
        { group: "Compressor", label: "Frequency", value: Math.round(last.freq) + " Hz" },
        { group: "Outdoor unit", label: "Outdoor air", value: last.air.toFixed(1) + " °C" },
        { group: "Outdoor unit", label: "Fan", value: last.on ? "On" : "Off" },
        { group: "Outdoor unit", label: "Defrost", value: "Off" },
        { group: "Electrical", label: "Power", value: power.toFixed(2) + " kW" },
        { group: "Electrical", label: "Voltage", value: Math.round(229 + noise(nowMin + cfg.seed) * 4) + " V" }
      ]
    };
  }

  var SAMPLE_UNITS = [
    { id: "unit-1", name: "Sample unit 1", site: "Sample site A", seed: 1, setpoint: 50, use: 0.05, alarms: [] },
    { id: "unit-2", name: "Sample unit 2", site: "Sample site A", seed: 2.7, setpoint: 48, use: 0.035,
      alarms: [{ minutesAgo: 4210, code: "Sample", text: "Example alarm, cleared", active: false }] },
    { id: "unit-3", name: "Sample unit 3", site: "Sample site B", seed: 5.3, setpoint: 55, use: 0.07, stopLastMinutes: 95,
      alarms: [
        { minutesAgo: 95, code: "Sample", text: "Example active alarm", active: true },
        { minutesAgo: 11630, code: "Sample", text: "Example alarm, cleared", active: false }
      ] }
  ];

  var gotRealData = false;

  window.loadHeatPumpData = async function () {
    var response = null;
    try {
      response = await fetch("api/data", { cache: "no-store" });
    } catch (err) {
      if (gotRealData) throw err;
    }
    if (response && response.ok) {
      gotRealData = true;
      return response.json();
    }
    if (gotRealData || (response && response.status !== 404)) throw new Error("Collector did not answer");

    var nowMin = Math.floor(Date.now() / 60000);
    return {
      live: false,
      units: SAMPLE_UNITS.map(function (cfg) { return makeUnit(cfg, nowMin); })
    };
  };
})();
