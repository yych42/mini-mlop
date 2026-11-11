// mini-mlop web UI. No framework, no build step: this file is what the browser runs.
import { initAgent, agentSettings } from "./agent.js";
import { LineChart, drawHistogram, fmt, fmtStep, fmtDuration } from "./chart.js";

const root = document.getElementById("view");
let view = root;  // each navigation renders into its own mount, attached when ready
const crumbs = document.getElementById("crumbs");
const SERIES = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6", "--s7", "--s8"];
const MAX_COMPARE = SERIES.length;
const POLL_MS = 5000;

// -- tiny DOM helper: strings always become text nodes, never markup --------
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v;
    else if (k in el && typeof v !== "string") el[k] = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

// replaceChildren, minus the footgun of null turning into the text "null"
function put(el, ...kids) {
  el.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false));
}

// -- API ---------------------------------------------------------------------
class Unauthorized extends Error {}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: opts.body ? { "Content-Type": "application/json" } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    credentials: "same-origin",
  });
  if (res.status === 401 && !opts.login) throw new Unauthorized();
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `request failed (${res.status})`);
  return data;
}

const q = (params) =>
  params.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join("&");

// -- lifecycle: each view registers cleanup and polling ----------------------
let cleanups = [];
let generation = 0;

function onLeave(fn) {
  cleanups.push(fn);
}

function poll(fn, ms = POLL_MS) {
  const id = setInterval(() => {
    if (!document.hidden) fn().catch(handleError);
  }, ms);
  onLeave(() => clearInterval(id));
}

function handleError(e) {
  if (e instanceof Unauthorized) return showLogin();
  console.error(e);
}

function setCrumbs(...parts) {
  put(crumbs);
  for (const [text, href] of parts) {
    crumbs.append(h("span", { class: "sep" }, "/"), href ? h("a", { href }, text) : h("span", {}, text));
  }
  document.title = parts.length ? `${parts[parts.length - 1][0]} · mini-mlop` : "mini-mlop";
}

// -- formatting --------------------------------------------------------------
function ago(ms) {
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)}d ago`;
  return new Date(ms).toLocaleDateString();
}

function runDuration(run) {
  const end = run.finished_at || (run.status === "RUNNING" ? Date.now() : run.heartbeat_at);
  return fmtDuration(Math.max(0, end - run.created_at));
}

function bytes(n) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) (n /= 1024), i++;
  return `${n < 10 && i ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
}

const STATUS_TEXT = {
  RUNNING: "Running", COMPLETED: "Completed", FAILED: "Failed",
  TERMINATED: "Interrupted", CANCELLED: "Stopped",
};

function statusBadge(run) {
  const label = run.stalled ? "Stalled" : STATUS_TEXT[run.status] || run.status;
  return h("span", { class: `status ${run.status}` }, h("i"), label);
}

function groupOf(name) {
  const i = name.indexOf("/");
  return i > 0 ? name.slice(0, i) : "";
}

function byGroup(names) {
  const groups = new Map();
  for (const n of names) {
    const g = groupOf(n);
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(n);
  }
  return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]));
}

// System metrics arrive as raw byte counts and percentages; say so.
function unitOf(name) {
  if (groupOf(name) !== "sys") return null;
  if (/\.(percentage|pct)(\.|$)/.test(name)) return "percent";
  if (/\/(memory|mem|disk|network|net)\./.test(name)) return "bytes";
  return null;
}

function emptyState(title, ...more) {
  return h("div", { class: "card empty" }, h("div", {}, title), ...more);
}

// Run work only for cards that are on screen: a run with hundreds of series
// should cost what the visible dozen cost.
function lazily(el, load) {
  let visible = false;
  const io = new IntersectionObserver(
    (entries) => {
      visible = entries[entries.length - 1].isIntersecting;
      if (visible) load().catch(handleError);
    },
    { rootMargin: "300px" },
  );
  io.observe(el);
  onLeave(() => io.disconnect());
  return () => (visible ? load() : Promise.resolve());
}

// -- chart controls shared by run and compare views --------------------------
const prefs = { smooth: 0, log: false };

function chartToolbar(charts) {
  const apply = () => charts.forEach((c) => c.setOptions({ smooth: prefs.smooth, logY: prefs.log }));
  return h("div", { class: "toolbar" },
    h("label", {}, "Smoothing",
      h("input", { type: "range", min: "0", max: "0.99", step: "0.01", value: String(prefs.smooth),
        oninput: (e) => { prefs.smooth = +e.target.value; apply(); } })),
    h("label", {},
      h("input", { type: "checkbox", checked: prefs.log,
        onchange: (e) => { prefs.log = e.target.checked; apply(); } }),
      "Log scale"),
  );
}

function chartCard(title) {
  const canvas = h("canvas", { tabindex: "0", role: "img", "aria-label": `Line chart of ${title}` });
  const latest = h("span", { class: "latest num" });
  const note = h("div", { class: "note" });
  const el = h("div", { class: "card chart" },
    h("header", {}, h("span", { class: "title", title }, title), latest), canvas, note);
  const chart = new LineChart(canvas, { smooth: prefs.smooth, logY: prefs.log });
  onLeave(() => chart.destroy());
  return { el, chart, latest, note };
}

// -- views -------------------------------------------------------------------
async function showLogin(message) {
  generation++;
  cleanups.splice(0).forEach((fn) => fn());
  setCrumbs();
  view = h("div");
  put(root, view);
  const key = h("input", { type: "password", placeholder: "mini_mlop_…", autocomplete: "current-password",
    "aria-label": "API key", required: true });
  const err = h("div", { class: "error" }, message || "");
  const form = h("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      try {
        await api("/api/login", { method: "POST", body: { key: key.value.trim() }, login: true });
        route();
      } catch (ex) {
        err.textContent = ex.message;
      }
    },
  }, key, h("button", { class: "primary", type: "submit" }, "Sign in"), err);
  put(view, h("div", { class: "card pad login" },
    h("h1", {}, "Sign in"),
    h("p", { class: "muted" }, "Paste an API key. The server printed one the first time it started; " +
      "more can be made with ", h("code", {}, "mini-mlop-server keys create NAME"), "."),
    form));
  key.focus();
}

async function viewProjects() {
  setCrumbs();
  const { projects } = await api("/api/projects");
  if (!projects.length) {
    const me = await api("/api/me");
    put(view, 
      h("div", { class: "head" }, h("h1", {}, "Projects")),
      emptyState("No runs yet. Log your first one:",
        h("pre", {}, `pip install git+https://github.com/yych42/mini-mlop.git\nexport MINI_MLOP_URL=${me.url}\n\n` +
          `import mini_mlop as mlop\nrun = mlop.init(project="hello")\nrun.log({"loss": 0.5})\nrun.finish()`)));
    poll(async () => { if ((await api("/api/projects")).projects.length) route(); });
    return;
  }
  const body = h("tbody");
  const fill = (list) => put(body, ...list.map((p) =>
    h("tr", {},
      h("td", {}, h("a", { href: `#/projects/${encodeURIComponent(p.name)}` }, p.name)),
      h("td", { class: "num" }, p.runs),
      h("td", {}, p.running ? h("span", { class: "status RUNNING" }, h("i"), `${p.running} running`) : ""),
      h("td", { class: "muted" }, ago(p.last_activity)))));
  fill(projects);
  put(view, 
    h("div", { class: "head" }, h("h1", {}, "Projects")),
    h("div", { class: "card tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Project"), h("th", {}, "Runs"), h("th", {}, ""), h("th", {}, "Last activity"))),
      body)));
  poll(async () => fill((await api("/api/projects")).projects));
}

async function viewProject(project) {
  setCrumbs([project]);
  const base = `/api/projects/${encodeURIComponent(project)}`;
  let { runs, total } = await api(`${base}/runs`);
  // Colour belongs to a run for as long as it stays selected, so ticking
  // another run never repaints the ones already on the charts.
  const slots = new Map();
  const take = (id) => {
    for (let i = 0; i < MAX_COMPARE; i++) {
      if (![...slots.values()].includes(i)) return slots.set(id, i), true;
    }
    return false;
  };
  runs.slice(0, 5).forEach((r) => take(r.id));

  const configCols = (() => {
    const seen = new Map();
    for (const r of runs) {
      for (const [k, v] of Object.entries(r.config || {})) {
        if (v !== null && typeof v === "object") continue;
        if (!seen.has(k)) seen.set(k, new Set());
        seen.get(k).add(JSON.stringify(v));
      }
    }
    // Columns that differ between runs are the ones worth the space.
    return [...seen.entries()].sort((a, b) => b[1].size - a[1].size).slice(0, 5).map(([k]) => k);
  })();

  const tbody = h("tbody");
  const chartsEl = h("div");
  const hint = h("span", { class: "sub" });

  function renderTable() {
    put(tbody, ...runs.map((r) => {
      const slot = slots.get(r.id);
      const box = h("input", { type: "checkbox", checked: slot != null, "aria-label": `Compare ${r.name}`,
        onchange: () => {
          if (box.checked) {
            if (!take(r.id)) {
              box.checked = false;
              hint.textContent = `Up to ${MAX_COMPARE} runs can be compared at once.`;
              return;
            }
          } else slots.delete(r.id);
          hint.textContent = "";
          renderTable();
          renderCharts();
        } });
      return h("tr", {},
        h("td", {}, box),
        h("td", {}, h("span", { class: "swatch", style: slot != null ? `background:var(${SERIES[slot]})` : "visibility:hidden" }),
          h("a", { href: `#/runs/${r.id}` }, r.name)),
        h("td", {}, statusBadge(r)),
        h("td", { class: "muted" }, ago(r.created_at)),
        h("td", { class: "num" }, runDuration(r)),
        configCols.map((k) => {
          const v = (r.config || {})[k];
          return h("td", { class: "clip num" }, v === undefined ? "" : typeof v === "number" ? fmt(v) : String(v));
        }));
    }));
  }

  let refreshers = [];
  function renderCharts() {
    refreshers = [];
    const chosen = runs.filter((r) => slots.has(r.id));
    const names = [...new Set(chosen.flatMap((r) => r.metrics))].filter((n) => groupOf(n) !== "sys").sort();
    if (!chosen.length || !names.length) {
      put(chartsEl, emptyState(chosen.length ? "The selected runs have not logged any metrics yet." : "Tick runs above to compare their metrics."));
      return;
    }
    const charts = [];
    const sections = byGroup(names).map(([group, list]) => {
      const grid = h("div", { class: "grid" });
      for (const name of list) {
        const card = chartCard(name);
        charts.push(card.chart);
        const having = chosen.filter((r) => r.metrics.includes(name));
        const load = async () => {
          const results = await Promise.all(having.map((r) =>
            api(`/api/runs/${r.id}/metrics?${q([["name", name], ["points", 400]])}`)));
          card.chart.setData(having.map((r, i) => ({
            label: r.name, color: SERIES[slots.get(r.id)], ...(results[i][name] || { x: [], y: [] }),
          })));
        };
        refreshers.push(lazily(card.el, load));
        grid.append(card.el);
      }
      return [h("h2", {}, group || "Metrics"), grid];
    });
    put(chartsEl, chartToolbar(charts), ...sections.flat());
  }

  renderTable();
  renderCharts();
  put(view, 
    h("div", { class: "head" }, h("h1", {}, project),
      h("span", { class: "sub" }, `${total} run${total === 1 ? "" : "s"}`), hint, h("span", { class: "grow" }),
      h("button", { class: "danger", onclick: async () => {
        if (!confirm(`Delete project "${project}" and all ${total} of its runs? This cannot be undone.`)) return;
        await api(base, { method: "DELETE" });
        location.hash = "#/";
      } }, "Delete project")),
    h("div", { class: "card tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", {}, "Run"), h("th", {}, "Status"), h("th", {}, "Started"),
        h("th", {}, "Duration"), configCols.map((k) => h("th", {}, k)))),
      tbody)),
    total > runs.length ? h("p", { class: "muted" }, `Showing the ${runs.length} most recent runs.`) : null,
    chartsEl);

  poll(async () => {
    const before = JSON.stringify(runs.map((r) => [r.id, r.metrics.length]));
    ({ runs, total } = await api(`${base}/runs`));
    renderTable();
    if (before !== JSON.stringify(runs.map((r) => [r.id, r.metrics.length]))) renderCharts();
    else if (runs.some((r) => r.status === "RUNNING" && slots.has(r.id))) await Promise.all(refreshers.map((f) => f()));
  });
}

const MEDIA = ["IMAGE", "AUDIO", "VIDEO", "TEXT", "ARTIFACT", "FILE"];
const DATA = ["HISTOGRAM", "TABLE"];

async function viewRun(id, tab) {
  let run = await api(`/api/runs/${id}`);
  setCrumbs([run.project, `#/projects/${encodeURIComponent(run.project)}`], [run.name]);

  const kinds = (list) => run.series.filter((s) => list.includes(s.kind));
  const metricNames = () => run.series.filter((s) => s.kind === "METRIC").map((s) => s.name);
  const tabs = [
    ["charts", "Charts", () => true],
    ["system", "System", () => metricNames().some((n) => groupOf(n) === "sys")],
    ["media", "Media", () => kinds(MEDIA).length],
    ["logs", "Logs", () => run.log_lines],
    ["model", "Model", () => run.has_graph],
    ["overview", "Overview", () => true],
  ].filter(([, , show]) => show());
  if (!tabs.some(([k]) => k === tab)) tab = "charts";

  const status = h("span", {});
  const stop = h("button", { onclick: async () => {
    if (!confirm("Stop this run? The training script is interrupted the next time it checks in.")) return;
    await api(`/api/runs/${id}/stop`, { method: "POST" });
    route();
  } }, "Stop run");
  const paintHead = () => {
    put(status, statusBadge(run), " ", h("span", { class: "sub" }, `· started ${ago(run.created_at)} · ${runDuration(run)}`));
    stop.hidden = run.status !== "RUNNING";
  };
  paintHead();

  const body = h("div");
  put(view, 
    h("div", { class: "head" }, h("h1", {}, run.name), status, h("span", { class: "grow" }), stop,
      h("button", { class: "danger", onclick: async () => {
        if (!confirm(`Delete run "${run.name}" and everything it logged? This cannot be undone.`)) return;
        await api(`/api/runs/${id}`, { method: "DELETE" });
        location.hash = `#/projects/${encodeURIComponent(run.project)}`;
      } }, "Delete")),
    h("div", { class: "tabs", role: "tablist" }, tabs.map(([k, label]) =>
      h("button", { role: "tab", "aria-selected": String(k === tab), onclick: () => (location.hash = `#/runs/${id}/${k}`) }, label))),
    body);

  let refresh = async () => {};
  if (tab === "charts") refresh = runCharts(run, body, (n) => groupOf(n) !== "sys", "step");
  else if (tab === "system") refresh = runCharts(run, body, (n) => groupOf(n) === "sys", "time");
  else if (tab === "media") refresh = runMedia(run, body);
  else if (tab === "logs") refresh = await runLogs(run, body);
  else if (tab === "model") await runModel(run, body);
  else runOverview(run, body);

  const shape = () => `${run.series.length}:${run.has_graph}:${!!run.log_lines}`;
  poll(async () => {
    const live = run.status === "RUNNING";
    const before = shape();
    const next = await api(`/api/runs/${id}`);
    const structural = (run = next, shape() !== before);
    paintHead();
    if (structural && tab !== "logs") return route();  // new series appeared: rebuild
    if (live) await refresh(run);
  });
}

function runCharts(run, body, want, axis) {
  const names = run.series.filter((s) => s.kind === "METRIC" && want(s.name)).map((s) => s.name);
  const data = axis === "step" ? run.series.filter((s) => DATA.includes(s.kind)) : [];
  if (!names.length && !data.length) {
    put(body, emptyState(run.status === "RUNNING" ? "Waiting for the first metrics…" : "This run did not log any metrics."));
    return async () => {};
  }
  const charts = [];
  const refreshers = [];
  const sections = byGroup([...names, ...data.map((s) => s.name)]).map(([group, list]) => {
    const grid = h("div", { class: "grid" });
    for (const name of list) {
      const series = data.find((s) => s.name === name);
      if (series) {
        const card = dataCard(run, series);
        refreshers.push(lazily(card.el, card.load));
        grid.append(card.el);
        continue;
      }
      const card = chartCard(name);
      card.chart.setOptions({ axis, origin: run.created_at, unit: unitOf(name) });
      charts.push(card.chart);
      const load = async () => {
        const r = await api(`/api/runs/${run.id}/metrics?${q([["name", name], ["points", 600], ["x", axis]])}`);
        const s = r[name] || { x: [], y: [] };
        card.chart.setData([{ label: name, color: SERIES[0], ...s }]);
        card.latest.textContent = s.y.length ? card.chart.value(s.y[s.y.length - 1]) : "";
        card.note.textContent = s.min ? `${s.count.toLocaleString()} points, averaged into ${s.x.length} buckets; band shows min–max` : "";
      };
      refreshers.push(lazily(card.el, load));
      grid.append(card.el);
    }
    return [h("h2", {}, group || "Metrics"), grid];
  });
  put(body, charts.length ? chartToolbar(charts) : null, ...sections.flat());
  return () => Promise.all(refreshers.map((f) => f()));
}

// A stepped series (histograms, tables, media): one frame per logged step,
// with a slider to move through them. Follows the newest step until moved.
function stepped(render) {
  let items = [];
  let pinned = false;
  const range = h("input", { type: "range", min: "0", max: "0", value: "0", "aria-label": "Step" });
  const label = h("span", { class: "num" });
  const el = h("div", { class: "slider" }, range, label);
  const show = () => {
    const item = items[+range.value];
    el.hidden = items.length < 2;
    if (!item) return;
    label.textContent = `step ${fmtStep(item.step)}`;
    render(item);
  };
  range.addEventListener("input", () => { pinned = +range.value < items.length - 1; show(); });
  return {
    el,
    set(next) {
      const grew = next.length !== items.length;
      items = next;
      range.max = String(Math.max(0, items.length - 1));
      if (!pinned) range.value = range.max;
      if (grew || !pinned) show();
    },
  };
}

function dataCard(run, series) {
  const frame = h("div", {});
  const note = h("div", { class: "note" });
  let canvas;
  const steps = stepped((entry) => {
    const d = entry.data || {};
    if (series.kind === "HISTOGRAM") {
      if (!canvas) {
        canvas = h("canvas", { role: "img", "aria-label": `Histogram of ${series.name}` });
        put(frame, canvas);
      }
      drawHistogram(canvas, d);
    } else {
      put(frame, dataTable(d));
    }
  });
  const el = h("div", { class: "card chart" }, h("header", {}, h("span", { class: "title", title: series.name }, series.name),
    h("span", { class: "latest" }, series.kind === "HISTOGRAM" ? "histogram" : "table")), frame, steps.el, note);
  const load = async () => {
    const r = await api(`/api/runs/${run.id}/data?${q([["name", series.name], ["limit", 64]])}`);
    steps.set(r.entries);
    note.textContent = r.total > r.entries.length ? `showing ${r.entries.length} of ${r.total} logged steps` : "";
  };
  return { el, load };
}

function dataTable(d) {
  const rows = Array.isArray(d.table) ? d.table : [];
  const cols = d.col || null;
  const rowNames = d.row || null;
  return h("div", { class: "tablewrap", style: "max-height:240px;overflow:auto" }, h("table", {},
    cols ? h("thead", {}, h("tr", {}, rowNames ? h("th", {}) : null, cols.map((c) => h("th", {}, c.name)))) : null,
    h("tbody", {}, rows.slice(0, 200).map((r, i) => h("tr", {},
      rowNames ? h("th", {}, rowNames[i] ? rowNames[i].name : "") : null,
      (Array.isArray(r) ? r : [r]).map((c) => h("td", { class: "num" }, typeof c === "number" ? fmt(c) : String(c))))))));
}

function runMedia(run, body) {
  const list = run.series.filter((s) => MEDIA.includes(s.kind));
  const refreshers = [];
  const grid = h("div", { class: "grid" });
  for (const series of list) {
    const frame = h("div", { class: "frame" });
    const caption = h("div", { class: "note" });
    let shown = null;
    const steps = stepped(async (f) => {
      if (shown === f.id) return;
      shown = f.id;
      const url = `/api/files/${f.id}`;
      caption.textContent = `${f.name} · ${bytes(f.size)}`;
      if (series.kind === "IMAGE") put(frame, h("img", { src: url, alt: `${series.name} at step ${f.step}`, loading: "lazy" }));
      else if (series.kind === "AUDIO") put(frame, h("audio", { src: url, controls: true, preload: "metadata" }));
      else if (series.kind === "VIDEO") put(frame, h("video", { src: url, controls: true, preload: "metadata" }));
      else if (series.kind === "TEXT" && f.size < 262144) {
        const pre = h("pre", {}, "…");
        put(frame, pre);
        const res = await fetch(url, { credentials: "same-origin" });
        if (shown === f.id) pre.textContent = await res.text();
      } else put(frame, h("a", { href: url, download: f.name }, `Download ${f.name}`));
    });
    const el = h("div", { class: "card media" }, h("div", { class: "title" }, series.name), frame, steps.el, caption);
    refreshers.push(lazily(el, async () => {
      const r = await api(`/api/runs/${run.id}/files?${q([["name", series.name]])}`);
      steps.set(r.files);
      if (!r.files.length) caption.textContent = "upload in progress…";
    }));
    grid.append(el);
  }
  put(body, grid);
  return () => Promise.all(refreshers.map((f) => f()));
}

async function runLogs(run, body) {
  const box = h("div", { class: "logs", tabindex: "0", "aria-label": "Console output" });
  let last = 0;
  const add = (lines) => {
    const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
    for (const l of lines) {
      last = l.id;
      box.append(h("div", { class: l.level },
        h("time", {}, new Date(l.time).toLocaleTimeString([], { hour12: false })), l.message));
    }
    while (box.childElementCount > 5000) box.firstElementChild.remove();
    if (stick) box.scrollTop = box.scrollHeight;
  };
  const first = (await api(`/api/runs/${run.id}/logs?limit=1000`)).lines;
  put(body, 
    run.log_lines > first.length ? h("p", { class: "muted" }, `Showing the last ${first.length} of ${run.log_lines.toLocaleString()} lines.`) : null,
    box);
  add(first);
  box.scrollTop = box.scrollHeight;
  return async () => add((await api(`/api/runs/${run.id}/logs?${q([["after", last], ["limit", 2000]])}`)).lines);
}

async function runModel(run, body) {
  const { graph } = await api(`/api/runs/${run.id}/graph`);
  const nodes = Object.entries((graph && graph.nodes) || {});
  put(body, h("div", { class: "card pad tree" }, nodes.map(([name, n]) => {
    const params = Object.values(n.params || {}).reduce((sum, shape) =>
      sum + (Array.isArray(shape) ? shape.reduce((a, b) => a * b, 1) : 0), 0);
    return h("div", { style: `padding-left:${Math.max(0, (n.depth || 0)) * 18}px` },
      name === "." ? "(model)" : name.split(".").pop(),
      h("span", { class: "t" }, [n.type, params ? `${params.toLocaleString()} params` : null].filter(Boolean).join(" · ")));
  })));
}

function kv(obj) {
  return h("dl", { class: "kv" }, Object.entries(obj).filter(([, v]) => v != null && v !== "").map(([k, v]) => [
    h("dt", {}, k), h("dd", { class: typeof v === "object" ? "mono" : "" }, typeof v === "object" ? JSON.stringify(v) : String(v))]));
}

function runOverview(run, body) {
  const sys = run.system || {};
  const git = sys.git || {};
  const gpus = ((sys.gpu || {}).nvidia || {}).devices || [];
  const trace = run.status_meta && (run.status_meta.trace || run.status_meta.message);
  put(body, 
    run.alerts.length ? h("div", {}, h("h2", { style: "margin-top:0" }, "Alerts"), h("div", { class: "card tablewrap" }, h("table", {}, h("tbody", {},
      run.alerts.map((a) => h("tr", {}, h("td", { class: "muted" }, new Date(a.time).toLocaleString()),
        h("td", {}, h("b", {}, a.level)), h("td", {}, a.title), h("td", {}, a.body))))))) : null,
    trace ? h("div", {}, h("h2", {}, "Exit"), h("div", { class: "card pad" }, h("pre", {}, trace))) : null,
    h("div", { class: "cols", style: "margin-top:14px" },
      h("div", { class: "card pad" }, h("h2", { style: "margin-top:0" }, "Run"), kv({
        id: run.id, project: run.project, status: run.stalled ? "FAILED (no heartbeat; resumes if the client returns)" : run.status,
        started: new Date(run.created_at).toLocaleString(),
        finished: run.finished_at ? new Date(run.finished_at).toLocaleString() : null,
        duration: runDuration(run), series: run.series.length, "log lines": run.log_lines,
      })),
      h("div", { class: "card pad" }, h("h2", { style: "margin-top:0" }, "Config"),
        run.config && Object.keys(run.config).length ? kv(run.config) : h("span", { class: "muted" }, "No config was passed to mlop.init().")),
      h("div", { class: "card pad" }, h("h2", { style: "margin-top:0" }, "Machine"), kv({
        host: (sys.platform || {}).node, os: [(sys.platform || {}).system, (sys.platform || {}).release].filter(Boolean).join(" "),
        arch: (sys.platform || {}).machine,
        cpu: sys.cpu ? `${sys.cpu.physical || "?"} cores / ${sys.cpu.virtual || "?"} threads` : null,
        memory: sys.memory ? bytes(sys.memory.virt) : null,
        gpu: gpus.length ? gpus.map((g) => g.name).join(", ") : null,
        command: ((sys.process || {}).cmdline || []).join(" "),
      })),
      git.commit ? h("div", { class: "card pad" }, h("h2", { style: "margin-top:0" }, "Git"), kv({
        branch: git.branch, commit: git.commit, dirty: git.dirty ? "yes, uncommitted changes" : "no", remote: git.url,
      })) : null),
    git.diff && git.diff.head ? h("div", {}, h("h2", {}, "Uncommitted changes"), h("div", { class: "logs" }, h("pre", {}, git.diff.head))) : null,
    sys.requirements ? h("details", { style: "margin-top:20px" }, h("summary", { class: "muted" }, `${sys.requirements.length} Python packages`),
      h("div", { class: "logs", style: "margin-top:8px" }, h("pre", {}, sys.requirements.slice().sort().join("\n")))) : null);
}

async function viewSettings() {
  setCrumbs(["Settings"]);
  const [me, keys, stats] = await Promise.all([api("/api/me"), api("/api/keys"), api("/api/stats")]);
  const assistant = await agentSettings(me);
  const fresh = h("div");
  const name = h("input", { type: "text", placeholder: "Key name, e.g. laptop", "aria-label": "Key name", maxlength: "64" });
  put(view, 
    h("div", { class: "head" }, h("h1", {}, "Settings"), h("span", { class: "grow" }),
      me.auth ? h("button", { onclick: async () => { await api("/api/logout", { method: "POST" }); showLogin(); } }, "Sign out") : null),
    h("h2", { style: "margin-top:0" }, "Connect a training script"),
    h("div", { class: "card pad" }, h("pre", {},
      `pip install git+https://github.com/yych42/mini-mlop.git\nexport MINI_MLOP_URL=${me.url}\n${me.auth ? "mini-mlop login   # paste an API key once per machine\n" : ""}`)),
    h("h2", {}, "API keys"),
    me.auth ? null : h("p", { class: "error" }, "Authentication is disabled (--no-auth): anyone who can reach this server can read and write. Keys are not checked."),
    h("div", { class: "card tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Key"), h("th", {}, "Created"), h("th", {}, "Last used"), h("th", {}))),
      h("tbody", {}, keys.keys.map((k) => h("tr", {},
        h("td", {}, k.name, k.id === keys.current ? h("span", { class: "muted" }, "  (this session)") : null),
        h("td", { class: "mono" }, k.hint),
        h("td", { class: "muted" }, new Date(k.created_at).toLocaleDateString()),
        h("td", { class: "muted" }, k.last_used_at ? ago(k.last_used_at) : "never"),
        h("td", {}, k.id === keys.current ? null : h("button", { class: "danger", onclick: async () => {
          if (!confirm(`Revoke key "${k.name}"? Scripts using it will stop being able to log.`)) return;
          await api(`/api/keys/${k.id}`, { method: "DELETE" });
          route();
        } }, "Revoke"))))))),
    h("form", { class: "row", style: "margin-top:12px", onsubmit: async (e) => {
      e.preventDefault();
      const k = await api("/api/keys", { method: "POST", body: { name: name.value.trim() || "unnamed" } });
      put(fresh, h("div", { class: "card pad", style: "margin-top:12px" },
        h("b", {}, `Key "${k.name}" created.`), " Copy it now; it is not shown again.",
        h("div", { class: "keybox" }, k.key)));
      name.value = "";
    } }, name, h("button", { class: "primary", type: "submit" }, "Create key")),
    fresh,
    assistant,
    h("h2", {}, "Storage"),
    h("div", { class: "card pad" }, kv({
      projects: stats.projects, runs: stats.runs, database: bytes(stats.db_bytes),
      "uploaded files": bytes(stats.file_bytes), version: me.version,
    })));
}

// -- SQL console: the same open-ended interface agents use ---------------------
let lastSql = "";

async function viewQuery() {
  setCrumbs(["Query"]);
  const schema = await api("/api/query/schema");
  const input = h("textarea", { class: "sql mono", rows: 7, spellcheck: "false", "aria-label": "SQL query",
    placeholder: "SELECT id, project, name, status FROM runs ORDER BY id DESC" });
  input.value = lastSql || schema.examples[0].sql;
  const status = h("span", { class: "muted" });
  const out = h("div");
  const runButton = h("button", { class: "primary", type: "submit" }, "Run");

  async function run() {
    lastSql = input.value;
    runButton.disabled = true;
    status.textContent = "Running…";
    try {
      const r = await api("/api/query", { method: "POST", body: { sql: input.value, limit: 1000 } });
      status.textContent = `${r.row_count} row${r.row_count === 1 ? "" : "s"}` +
        (r.truncated ? " (truncated; add LIMIT or aggregate)" : "") + ` · ${r.elapsed_ms} ms`;
      put(out, r.rows.length
        ? h("div", { class: "card tablewrap results" }, h("table", {},
            h("thead", {}, h("tr", {}, r.columns.map((c) => h("th", {}, c)))),
            h("tbody", {}, r.rows.map((row) => h("tr", {}, row.map((v) =>
              h("td", { class: typeof v === "number" ? "num mono" : "mono" }, v === null ? h("span", { class: "muted" }, "null") : String(v))))))))
        : h("p", { class: "muted" }, "No rows."));
    } catch (e) {
      if (e instanceof Unauthorized) return showLogin();
      status.textContent = "";
      put(out, h("div", { class: "error" }, e.message));
    } finally {
      runButton.disabled = false;
    }
  }

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); run(); }
  });
  put(view,
    h("div", { class: "head" }, h("h1", {}, "Query")),
    h("p", { class: "muted" }, "Read-only SQL over everything this server stores. Agents and scripts use the same interface at ",
      h("code", {}, "POST /api/query"), "; ", h("code", {}, "GET /api/query/schema"), " describes it."),
    h("div", { class: "querygrid" },
      h("div", {},
        h("form", { onsubmit: (e) => { e.preventDefault(); run(); } },
          input,
          h("div", { class: "queryrow" }, runButton, h("span", { class: "muted" }, "Ctrl/⌘ + Enter"), h("span", { class: "grow" }), status)),
        out),
      h("aside", {},
        h("h2", {}, "Tables"),
        schema.tables.map((t) => h("details", {},
          h("summary", {}, h("code", {}, t.name), " ", h("span", { class: "muted" }, t.description)),
          h("ul", { class: "cols" }, t.columns.map(([name, type, note]) =>
            h("li", {}, h("code", {}, name), " ", h("span", { class: "muted" }, type.toLowerCase() + (note ? ` · ${note}` : ""))))))),
        h("h2", {}, "Examples"),
        h("ul", { class: "examples" }, schema.examples.map((ex) =>
          h("li", {}, h("button", { type: "button", onclick: () => { input.value = ex.sql; input.focus(); } }, ex.question)))),
        h("h2", {}, "Notes"),
        h("ul", { class: "cols" }, schema.notes.map((n) => h("li", { class: "muted" }, n))))));
}

// -- router ------------------------------------------------------------------
async function route() {
  const mine = ++generation;
  cleanups.splice(0).forEach((fn) => fn());
  document.getElementById("tooltip").hidden = true;
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  const mount = (view = h("div"));
  // Attach as soon as the view has drawn something, unless the user has
  // already navigated elsewhere.
  const attach = new MutationObserver(() => {
    attach.disconnect();
    if (mine === generation) put(root, mount);
  });
  attach.observe(mount, { childList: true });
  try {
    if (parts[0] === "projects" && parts[1]) await viewProject(parts[1]);
    else if (parts[0] === "runs" && /^\d+$/.test(parts[1] || "")) await viewRun(+parts[1], parts[2]);
    else if (parts[0] === "settings") await viewSettings();
    else if (parts[0] === "query") await viewQuery();
    else await viewProjects();
  } catch (e) {
    attach.disconnect();
    if (mine !== generation) return;
    if (e instanceof Unauthorized) return showLogin();
    setCrumbs();
    put(root, mount);
    put(view, emptyState(e.message, h("p", {}, h("a", { href: "#/" }, "Back to projects"))));
  }
}

initAgent({ h, put, api, Unauthorized, onUnauthorized: () => showLogin() });
window.addEventListener("hashchange", route);
route();
