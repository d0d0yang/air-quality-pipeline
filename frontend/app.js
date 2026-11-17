// app.js
// everything the three pages do. each page sets <body data-page="..."> and gets its own small init function.

const REPO = "https://github.com/d0d0yang/air-quality-pipeline";
const AQHI_URL = "https://www.canada.ca/en/environment-climate-change/services/air-quality-health-index.html";
// official aqhi colours for 1..10 and 10+ (sampled from eccc's scale image, see pipeline/aqhi.py)
const AQHI_COLOURS = ["#00ccff", "#0099cc", "#006699", "#ffff00", "#ffcc00", "#ff9933",
                      "#ff6666", "#ff0000", "#cc0000", "#990000", "#660000"];
const GREY = "#c8ccc2";
const ACCENT = "#306f6e";

// --- helpers ---

const $ = (id) => document.getElementById(id);
const fmt = (v, d = 1) => (v === null || v === undefined ? "–" : Number(v).toFixed(d));
const pct = (v) => (v === null || v === undefined ? "–" : `${Math.round(v * 100)}%`);
const time = (iso) => (iso ? new Date(iso).toLocaleString("en-CA", {
  timeZone: "America/Toronto", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "–");
const minutes = (s) => (s === null || s === undefined ? "–" : s < 90 ? `${s.toFixed(0)} s` : `${(s / 60).toFixed(1)} min`);

function aqhiIndex(v) {
  return v === null || v === undefined ? null : Math.min(Math.max(Math.floor(v + 0.5), 1), 11);
}
const aqhiColour = (v) => (aqhiIndex(v) ? AQHI_COLOURS[aqhiIndex(v) - 1] : GREY);
const aqhiLabel = (v) => (aqhiIndex(v) === null ? "–" : aqhiIndex(v) === 11 ? "10+" : String(aqhiIndex(v)));

// small dom builder: el("td", { class: "num" }, "text", child, ...)
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  node.append(...children.filter((c) => c !== null && c !== undefined));
  return node;
}

function aqhiTag(v) {
  const colour = aqhiColour(v);
  const dark = ["#00ccff", "#ffff00", "#ffcc00", "#ff9933", "#ff6666", GREY].includes(colour);
  return el("span", { class: "aqhi", style: `background:${colour};color:${dark ? "#151a1d" : "#fff"}` }, aqhiLabel(v));
}

function row(cells, attrs = {}) {
  return el("tr", attrs, ...cells.map((c) => (c instanceof Node && c.tagName === "TD" ? c : el("td", {}, c))));
}

async function load(name) {
  const resp = await fetch(`data/${name}`, { cache: "no-cache" });
  if (!resp.ok) throw new Error(`${name}: ${resp.status}`);
  return resp.json();
}

function lineChart(canvas, labels, series, yTitle) {
  Chart.defaults.font.family = "Inter, system-ui, sans-serif";
  if (canvas.chart) canvas.chart.destroy();
  canvas.chart = new Chart(canvas, {
    type: "line",
    data: { labels, datasets: series.map((s) => ({ pointRadius: 0, borderWidth: 1.5, ...s })) },
    options: {
      maintainAspectRatio: false, animation: false, interaction: { mode: "index", intersect: false },
      plugins: { legend: { labels: { boxWidth: 12, filter: (i) => !i.text.startsWith("_") } } },
      scales: { x: { ticks: { maxTicksLimit: 7, maxRotation: 0 }, grid: { display: false } },
                y: { beginAtZero: true, title: { display: true, text: yTitle } } },
    },
  });
}

// --- shared header line and footer ---

async function frame() {
  const summary = await load("summary.json").catch(() => null);
  const run = summary && summary.last_run;
  $("refreshed").textContent = run ? `Last refreshed ${time(run.ended_utc)}` : "";
  const providers = summary ? summary.attributions.map((a) => `${a.provider} (${a.licence || "licence not stated"})`) : [];
  document.querySelector(".footer").append(
    el("p", {}, "Not health advice: forecasts are experimental. Use the official ",
       el("a", { href: AQHI_URL }, "Air Quality Health Index"), "."),
    el("p", {}, "Data: Environment and Climate Change Canada (Open Government Licence – Canada) · OpenAQ · weather by " +
       "Open-Meteo.com (CC BY 4.0) · map © OpenStreetMap contributors."),
    providers.length ? el("p", {}, `Station providers: ${providers.join(" · ")}.`) : null,
    el("p", {}, el("a", { href: REPO }, "Source on GitHub"), " · a personal learning project."),
  );
  return summary;
}

// --- map page ---

async function initMap(summary) {
  const data = await load("stations.json");
  if (summary) {
    const r = summary.regional;
    $("summary").textContent = `${summary.sites.monitors} monitors and ${summary.sites.low_cost} low-cost sensors. ` +
      `PM2.5 right now averages ${fmt(r.pm25_latest_mean)} µg/m³; tomorrow's forecast averages ` +
      `${fmt(r.forecast_pm25_mean)} µg/m³.`;
  }
  const map = L.map("map", { scrollWheelZoom: false });
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 13, attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  map.fitBounds(data.stations.map((s) => [s.lat, s.lon]), { padding: [20, 20] });

  let layer;
  const draw = (tomorrow) => {
    $("show-now").setAttribute("aria-pressed", String(!tomorrow));
    $("show-tomorrow").setAttribute("aria-pressed", String(tomorrow));
    if (layer) layer.remove();
    layer = L.layerGroup(data.stations.map((s) => {
      const v = tomorrow ? s.forecast && s.forecast.aqhi : s.aqhi && s.aqhi.value;
      return L.circleMarker([s.lat, s.lon], {
        radius: s.low_cost ? 5 : 8, color: "#151a1d", weight: 1, fillColor: aqhiColour(v), fillOpacity: 0.9,
      }).bindTooltip(s.name).on("click", () => show(s));
    })).addTo(map);
  };
  $("show-now").onclick = () => draw(false);
  $("show-tomorrow").onclick = () => draw(true);
  draw(false);

  const show = (s) => {
    const f = s.forecast;
    $("station").replaceChildren(
      el("h2", {}, s.name, " ", s.low_cost ? el("span", { class: "badge" }, "low-cost") : null),
      el("p", { class: "note" }, s.aqhi ? aqhiTag(s.aqhi.value) : aqhiTag(null),
         s.aqhi ? ` AQHI ${s.aqhi.category} risk (${s.aqhi.kind}, ${time(s.aqhi.time_utc)})` : " no AQHI right now"),
      el("div", { class: "chart" }, el("canvas", { id: "station-chart", role: "img",
         "aria-label": `PM2.5 at ${s.name} over the last 7 days` })),
      el("p", { class: "note" }, f ? `Tomorrow: ${fmt(f.pm25)} µg/m³ (80% range ${fmt(f.p10)}–${fmt(f.p90)})` : "No forecast",
         s.weather_station ? `. Weather from ${s.weather_station.name}, ${fmt(s.weather_station.distance_km)} km away.` : "."),
    );
    const start = new Date(data.hours_start_utc).getTime();
    const labels = (s.pm25_hourly || []).map((_, i) => new Date(start + i * 3600e3).toLocaleString("en-CA",
      { timeZone: "America/Toronto", weekday: "short", hour: "2-digit" }));
    lineChart($("station-chart"), labels, [{ label: "PM2.5 hourly (µg/m³)", data: s.pm25_hourly, borderColor: ACCENT }], "µg/m³");
  };

  // the same stations as a list, for keyboard and screen-reader users
  const sorted = [...data.stations].sort((a, b) => a.low_cost - b.low_cost || a.name.localeCompare(b.name));
  $("list").replaceChildren(...sorted.map((s) => row([
    el("td", {}, el("button", { class: "link", onclick: () => show(s) }, s.name)),
    s.low_cost ? "low-cost" : "monitor",
    el("td", {}, s.aqhi ? aqhiTag(s.aqhi.value) : "–"),
    el("td", { class: "num" }, fmt(s.pm25_latest)),
    el("td", { class: "num" }, s.forecast ? fmt(s.forecast.pm25) : "–"),
  ])));
}

// --- forecast page ---

async function initForecast() {
  const f = await load("forecast.json");
  const m = f.metrics;
  const high = f.holdout.high_pollution_days;
  const rows = [[`Model (${f.chosen_model.replaceAll("_", " ")})`, m.model, high.model],
                ["Persistence (tomorrow = today)", m.persistence, high.persistence],
                ["Climatology (station × month)", m.climatology, high.climatology]];
  $("metrics").replaceChildren(...rows.map(([name, s, h], i) => row([name,
    el("td", { class: "num" }, fmt(s.mae, 2)), el("td", { class: "num" }, fmt(s.rmse, 2)),
    el("td", { class: "num" }, i === 0 ? pct(s.skill_vs_persistence) : "–"),
    el("td", { class: "num" }, fmt(h.mae, 2))], i === 0 ? { class: "best" } : {})));

  const a = f.holdout.aqhi_category;
  const sp = f.spatial;
  $("findings").replaceChildren(...[
    `Scored once on ${m.model.n.toLocaleString()} station-days (${f.data.holdout_targets.join(" to ")}) that were never used for training or model choice.`,
    `On stations it never saw (${sp.folds}-fold by station) the MAE is ${fmt(sp.mae, 2)} µg/m³, ${pct(sp.skill_vs_persistence)} better than persistence.`,
    `The 80% range held the actual value on ${pct(f.holdout.interval_80.coverage)} of days, so it's too narrow.`,
    `AQHI category right on ${pct(a.model.accuracy)} of days (persistence ${pct(a.persistence.accuracy)}). ECCC's own forecast overlaps only ${a.eccc_overlap_days} day so far.`,
    `On the worst 10% of days (≥ ${fmt(high.threshold_ug_m3)} µg/m³) its MAE is ${fmt(high.model.mae, 1)} vs ${fmt(high.persistence.mae, 1)} for persistence. Smoke from distant fires is its blind spot.`,
  ].map((t) => el("li", {}, t)));

  f.sites.forEach((s, i) => $("site").append(el("option", { value: i }, s.name)));
  const draw = () => {
    const days = f.sites[$("site").value].days;
    lineChart($("holdout-chart"), days.map((d) => d[0]), [
      { label: "Actual", data: days.map((d) => d[1]), borderColor: "#151a1d", borderWidth: 1 },
      { label: "Forecast", data: days.map((d) => d[2]), borderColor: ACCENT },
    ], "daily mean PM2.5 (µg/m³)");
  };
  $("site").onchange = draw;
  draw();
}

// --- pipeline page ---

async function initPipeline() {
  const p = await load("pipeline.json");
  const bench = await load("benchmark.json").catch(() => null);
  const median = (sc, name) => minutes(sc[name] && sc[name].median_s);
  $("bench").textContent = !bench || !bench.targets ? "Benchmark not run yet (TBD)." :
    Object.entries(bench.targets).map(([target, b]) =>
      `${target}: full reload ${median(b.scenarios, "full")}, incremental ${median(b.scenarios, "incremental_daily")}` +
      (b.reduction_pct !== null ? ` (${b.reduction_pct.toFixed(0)}% faster, medians of repeated runs).` : ".")).join(" ");

  const chip = (status) => el("span", { class: `chip ${status === "fail" ? "chip--fail" : status === "warn" ? "chip--warn" : ""}` }, status);
  $("sources").replaceChildren(...p.freshness.map((f) => row([f.source, time(f.latest_utc), el("td", { class: "num" }, f.rows.toLocaleString())])));
  $("checks").replaceChildren(...p.dq.map((d) => row([d.check.replaceAll("_", " "), chip(d.severity === "info" ? "info" : d.status)])));
  $("runs").replaceChildren(...p.runs.map((r) => row([
    el("td", { class: "num" }, r.run_id), time(r.started_utc), r.mode, r.target,
    el("td", { title: r.error || "" }, chip(r.status === "succeeded" ? "pass" : r.status === "running" ? "info" : "fail")),
    el("td", { class: "num" }, minutes(r.duration_s)),
    el("td", { class: "num" }, r.rows_inserted === null ? "–" : r.rows_inserted.toLocaleString()),
  ])));
}

// --- start ---

(async () => {
  const pages = { map: initMap, forecast: initForecast, pipeline: initPipeline };
  try {
    const summary = await frame();
    await pages[document.body.dataset.page](summary);
  } catch (err) {
    document.querySelector("main").prepend(el("p", { class: "notice", role: "alert" }, `Couldn't load the data (${err.message}).`));
  }
})();
