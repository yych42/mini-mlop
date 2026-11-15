// mini-mlop web UI. No framework, no build step: this file is what the browser runs.
import { initAgent, agentSettings } from "./agent.js";
import { LineChart, drawHistogram, fmt, fmtStep, fmtDuration } from "./chart.js";
import * as loading from "./loading.js";

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
    if (k === "onclick" || k === "onsubmit") el.addEventListener(k.slice(2), busyWhileRunning(v));
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
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

// A click or submit handler that returns a promise is an action that talks to
// the server: its button is disabled and the title shows the loading sweep
// until it settles.
function busyWhileRunning(fn) {
  return (e) => {
    const result = fn(e);
    if (!result || typeof result.then !== "function") return result;
    const button = e.type === "submit" ? e.submitter : e.currentTarget;
    const isButton = button instanceof HTMLButtonElement;
    if (isButton) { button.disabled = true; button.setAttribute("aria-busy", "true"); }
    loading.begin();
    return result.finally(() => {
      loading.end();
      if (isButton) { button.disabled = false; button.removeAttribute("aria-busy"); }
    });
  };
}

// replaceChildren, minus the footgun of null turning into the text "null"
function put(el, ...kids) {
  el.replaceChildren(...kids.flat(Infinity).filter((k) => k != null && k !== false));
}

// Live views redraw every few seconds, and the reader is in the middle of
// using them: scrolled into a table, hovering a cell, holding a slider. So
// polls never rebuild what is already on screen. Instead, a view is a set of
// parts that are made once and then updated in place.
//
// reconcile keeps `parent`'s children in step with `items`, one part per key.
// `make(item)` returns {el, update?(item), destroy?()}; update also runs right
// after make, so each part has a single drawing path. Parts whose key stays
// are updated, not replaced; new keys are made, gone keys destroyed, and an
// element is only moved when it is out of order.
function reconcile(parent, items, key, make) {
  const before = parent._parts || new Map();
  const after = new Map();
  for (const item of items) {
    const k = key(item);
    if (after.has(k)) continue;
    const part = before.get(k) || make(item);
    if (part.update) part.update(item);
    after.set(k, part);
  }
  for (const [k, part] of before) {
    if (after.has(k)) continue;
    part.el.remove();
    if (part.destroy) part.destroy();
  }
  let at = parent.firstChild;
  for (const part of after.values()) {
    if (part.el === at) at = at.nextSibling;
    else parent.insertBefore(part.el, at);
  }
  parent._parts = after;
}

// Writes only when the text changed, so an unchanged poll leaves text
// selections and the DOM alone.
function setText(el, text) {
  if (el.textContent !== text) el.textContent = text;
}

function icon(d) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  const path = document.createElementNS(NS, "path");
  for (const [k, v] of Object.entries({ viewBox: "0 0 16 16", width: "14", height: "14", "aria-hidden": "true" })) svg.setAttribute(k, v);
  for (const [k, v] of Object.entries({ d, fill: "none", stroke: "currentColor", "stroke-width": "1.5",
    "stroke-linecap": "round", "stroke-linejoin": "round" })) path.setAttribute(k, v);
  svg.append(path);
  return svg;
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

// Who is signed in: {auth, key, access: "read" | "write" | "admin", project}.
// The request itself is cached, so every caller shares one fetch.
let session = null;
const who = () => (session ??= api("/api/me").catch((e) => { session = null; throw e; }));

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
  const label = document.createTextNode("");
  const el = h("span", {}, h("i"), label);
  const update = (run) => {
    const cls = `status ${run.status}`;
    if (el.className !== cls) el.className = cls;
    const text = run.stalled ? "Stalled" : STATUS_TEXT[run.status] || run.status;
    if (label.data !== text) label.data = text;
  };
  update(run);
  return { el, update };
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

// Shows the loading sweep in the top bar until `promise` settles. For loads
// the reader is waiting on (a card's first data, a step they picked), not for
// background refreshes, which would keep it sweeping forever on a live run.
function track(promise) {
  loading.begin();
  return Promise.resolve(promise).finally(loading.end);
}

// Requests made in the same turn of the event loop go out as one call:
// `fetchMany(keys)` gets every key asked for (in chunks of `max`) and
// returns an object by key. Cards that come into view together, and every
// visible card on a poll, then cost one round trip instead of one each,
// which is what matters behind a tunnel where a round trip is most of the
// cost.
function batched(fetchMany, max = 40) {
  let queue = null;
  const flush = (waiting) => {
    const keys = [...waiting.keys()];
    for (let i = 0; i < keys.length; i += max) {
      const chunk = keys.slice(i, i + max);
      fetchMany(chunk).then(
        (out) => chunk.forEach((k) => waiting.get(k).forEach((p) => p.resolve(out[k]))),
        (e) => chunk.forEach((k) => waiting.get(k).forEach((p) => p.reject(e))));
    }
  };
  return (key) => new Promise((resolve, reject) => {
    if (!queue) {
      const waiting = (queue = new Map());
      setTimeout(() => { queue = null; flush(waiting); });
    }
    if (!queue.has(key)) queue.set(key, []);
    queue.get(key).push({ resolve, reject });
  });
}

// Run work only for cards that are on screen: a run with hundreds of series
// should cost what the visible dozen cost. A card's first load is tracked.
function lazily(el, load) {
  let visible = false, loaded = false;
  const io = new IntersectionObserver(
    (entries) => {
      visible = entries[entries.length - 1].isIntersecting;
      if (!visible) return;
      const p = load();
      if (!loaded) track(p);
      loaded = true;
      p.catch(handleError);
    },
    { rootMargin: "300px" },
  );
  io.observe(el);
  onLeave(() => io.disconnect());
  const refresh = () => (visible ? load() : Promise.resolve());
  refresh.stop = () => io.disconnect();
  return refresh;
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

// Which cards span the full width of their grid, by series name. Kept in this
// browser only; a name that is wide in one run is wide in every run.
const wide = (() => {
  const KEY = "mini-mlop.wide";
  let names;
  try { names = new Set(JSON.parse(localStorage.getItem(KEY) || "[]")); } catch { names = new Set(); }
  return {
    has: (name) => names.has(name),
    toggle(name) {
      if (!names.delete(name)) names.add(name);
      try { localStorage.setItem(KEY, JSON.stringify([...names])); } catch { /* private mode: this page only */ }
      return names.has(name);
    },
  };
})();

const EXPAND = "M9.5 2.5h4v4M13.5 2.5 9 7M6.5 13.5h-4v-4M2.5 13.5 7 9";
const SHRINK = "M13 7H9V3M9 7l4.5-4.5M3 9h4v4M7 9l-4.5 4.5";

// A card in a chart grid: title, an optional readout, and a button that lets
// the card take the whole row (and grow taller) for a closer look.
function gridCard(name, cls, readout, ...body) {
  const el = h("div", { class: cls });
  const button = h("button", { type: "button", class: "icon", onclick: () => paint(wide.toggle(name)) });
  const paint = (on) => {
    el.classList.toggle("wide", on);
    button.setAttribute("aria-pressed", String(on));
    button.setAttribute("aria-label", `${on ? "Shrink" : "Expand"} ${name}`);
    button.title = on ? "Back to grid size" : "Full width";
    put(button, icon(on ? SHRINK : EXPAND));
  };
  paint(wide.has(name));
  el.append(h("header", { class: "cardhead" }, h("span", { class: "title", title: name }, name),
    h("span", { class: "grow" }), readout, button), ...body);
  return el;
}

function chartCard(title) {
  const canvas = h("canvas", { tabindex: "0", role: "img", "aria-label": `Line chart of ${title}` });
  const latest = h("span", { class: "latest num" });
  const note = h("div", { class: "note" });
  const el = gridCard(title, "card chart", latest, canvas, note);
  const chart = new LineChart(canvas, { smooth: prefs.smooth, logY: prefs.log });
  onLeave(() => chart.destroy());
  return { el, chart, latest, note };
}

// -- views -------------------------------------------------------------------
async function showLogin(message) {
  session = null;
  generation++;
  cleanups.splice(0).forEach((fn) => fn());
  setCrumbs();
  view = h("div");
  put(root, view);
  root.removeAttribute("aria-busy");
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
  const fill = (list) => reconcile(body, list, (p) => p.name, (p) => {
    const runs = h("td", { class: "num" });
    const running = h("span", { class: "status RUNNING" }, h("i"), "");
    const last = h("td", { class: "muted" });
    return {
      el: h("tr", {}, h("td", {}, h("a", { href: `/projects/${encodeURIComponent(p.name)}` }, p.name)),
        runs, h("td", {}, running), last),
      update(p) {
        setText(runs, String(p.runs));
        running.hidden = !p.running;
        running.lastChild.data = `${p.running} running`;
        setText(last, ago(p.last_activity));
      },
    };
  });
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
  let [{ runs, total }, me] = await Promise.all([api(`${base}/runs`), who()]);
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

  const hint = h("span", { class: "sub" });
  const count = h("span", { class: "sub" });
  const more = h("p", { class: "muted" });
  const paintCounts = () => {
    setText(count, `${total} run${total === 1 ? "" : "s"}`);
    more.hidden = total <= runs.length;
    setText(more, `Showing the ${runs.length} most recent runs.`);
  };

  // -- the runs table: one row per run, updated in place --------------------
  const tbody = h("tbody");
  const toggle = (id, box) => {
    if (box.checked) {
      if (!take(id)) {
        box.checked = false;
        hint.textContent = `Up to ${MAX_COMPARE} runs can be compared at once.`;
        return;
      }
    } else slots.delete(id);
    hint.textContent = "";
    paintRows();
    track(paintCharts(true)).catch(handleError);
  };
  const makeRow = (r) => {
    const box = h("input", { type: "checkbox", "aria-label": `Compare ${r.name}`, onchange: () => toggle(r.id, box) });
    const swatch = h("span", { class: "swatch" });
    const link = h("a", { href: `/runs/${r.id}` });
    const badge = statusBadge(r);
    const started = h("td", { class: "muted" });
    const duration = h("td", { class: "num" });
    const config = configCols.map(() => h("td", { class: "clip num" }));
    return {
      el: h("tr", {}, h("td", {}, box), h("td", {}, swatch, link), h("td", {}, badge.el), started, duration, config),
      update(r) {
        const slot = slots.get(r.id);
        box.checked = slot != null;
        swatch.style.background = slot != null ? `var(${SERIES[slot]})` : "";
        swatch.style.visibility = slot != null ? "" : "hidden";
        setText(link, r.name);
        badge.update(r);
        setText(started, ago(r.created_at));
        setText(duration, runDuration(r));
        configCols.forEach((k, i) => {
          const v = (r.config || {})[k];
          setText(config[i], v === undefined ? "" : typeof v === "number" ? fmt(v) : String(v));
        });
      },
    };
  };
  const paintRows = () => reconcile(tbody, runs, (r) => r.id, makeRow);

  // -- compare charts: one card per metric any selected run has logged ------
  const charts = [];
  const toolbar = chartToolbar(charts);
  const sections = h("div");
  const empty = h("div");
  const chartsEl = h("div", {}, toolbar, sections, empty);
  const cards = new Set();
  let chosen = [];
  // One batched reader per run: all of a run's charts arrive in one request.
  const readers = new Map();
  const metric = (runId, name) => {
    if (!readers.has(runId)) readers.set(runId, batched((names) =>
      api(`/api/runs/${runId}/metrics?${q([...names.map((n) => ["name", n]), ["points", 400]])}`)));
    return readers.get(runId)(name);
  };
  const makeCard = (name) => {
    const card = chartCard(name);
    charts.push(card.chart);
    let latest = 0;  // a reply that arrives after a newer request was sent is dropped
    const refresh = lazily(card.el, async () => {
      const mine = ++latest;
      const having = chosen.filter((r) => r.metrics.includes(name));
      const results = await Promise.all(having.map((r) => metric(r.id, name)));
      if (mine !== latest) return;
      card.chart.setData(having.map((r, i) => ({
        label: r.name, color: SERIES[slots.get(r.id)], ...(results[i] || { x: [], y: [] }),
      })));
    });
    const part = { el: card.el, refresh, destroy() {
      refresh.stop(); card.chart.destroy(); charts.splice(charts.indexOf(card.chart), 1); cards.delete(part);
    } };
    cards.add(part);
    return part;
  };
  const makeSection = ([group]) => {
    const grid = h("div", { class: "grid" });
    return { el: h("div", {}, h("h2", {}, group || "Metrics"), grid),
      update: ([, names]) => reconcile(grid, names, (n) => n, makeCard) };
  };
  function paintCharts(reload) {
    chosen = runs.filter((r) => slots.has(r.id));
    const names = [...new Set(chosen.flatMap((r) => r.metrics))].filter((n) => groupOf(n) !== "sys").sort();
    const groups = chosen.length ? byGroup(names) : [];
    toolbar.hidden = !groups.length;
    if (groups.length) put(empty);
    else put(empty, emptyState(chosen.length ? "The selected runs have not logged any metrics yet." : "Tick runs above to compare their metrics."));
    reconcile(sections, groups, ([g]) => g, makeSection);
    return reload ? Promise.all([...cards].map((c) => c.refresh())) : null;
  }

  paintCounts();
  paintRows();
  paintCharts(false);
  put(view,
    h("div", { class: "head" }, h("h1", {}, project), count, hint, h("span", { class: "grow" }),
      me.access !== "admin" ? null : h("button", { class: "danger", onclick: async () => {
        if (!confirm(`Delete project "${project}" and all ${total} of its runs? This cannot be undone.`)) return;
        await api(base, { method: "DELETE" });
        navigate("/");
      } }, "Delete project")),
    h("div", { class: "card tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, ""), h("th", {}, "Run"), h("th", {}, "Status"), h("th", {}, "Started"),
        h("th", {}, "Duration"), configCols.map((k) => h("th", {}, k)))),
      tbody)),
    more,
    chartsEl);

  poll(async () => {
    // Judged before the fetch, so a run that just finished gets its last points drawn.
    const live = chosen.some((r) => r.status === "RUNNING");
    ({ runs, total } = await api(`${base}/runs`));
    paintCounts();
    paintRows();
    await paintCharts(live);
  });
}

const MEDIA = ["IMAGE", "AUDIO", "VIDEO", "TEXT", "ARTIFACT", "FILE"];
const DATA = ["HISTOGRAM", "TABLE"];

async function viewRun(id, requested) {
  let [run, me] = await Promise.all([api(`/api/runs/${id}`), who()]);
  setCrumbs([run.project, `/projects/${encodeURIComponent(run.project)}`], [run.name]);

  const kinds = (list) => run.series.filter((s) => list.includes(s.kind));
  const metricNames = () => run.series.filter((s) => s.kind === "METRIC").map((s) => s.name);
  const TABS = [
    ["charts", "Charts", () => true],
    ["system", "System", () => metricNames().some((n) => groupOf(n) === "sys")],
    ["media", "Media", () => kinds(MEDIA).length],
    ["logs", "Logs", () => run.log_lines],
    ["model", "Model", () => run.has_graph],
    ["overview", "Overview", () => true],
  ];
  const available = () => TABS.filter(([, , show]) => show());
  const tab = available().some(([k]) => k === requested) ? requested : "charts";

  const badge = statusBadge(run);
  const since = h("span", { class: "sub" });
  const stop = h("button", { onclick: async () => {
    if (!confirm("Stop this run? The training script is interrupted the next time it checks in.")) return;
    await api(`/api/runs/${id}/stop`, { method: "POST" });
    route();
  } }, "Stop run");
  const writable = me.access !== "read";
  const paintHead = () => {
    badge.update(run);
    setText(since, `· started ${ago(run.created_at)} · ${runDuration(run)}`);
    stop.hidden = !writable || run.status !== "RUNNING";
  };
  // Tabs appear as the run starts logging the things they show.
  const tabbar = h("div", { class: "tabs", role: "tablist" });
  const paintTabs = () => reconcile(tabbar, available(), ([k]) => k, ([k, label]) => ({
    el: h("button", { role: "tab", "aria-selected": String(k === tab), onclick: () => navigate(`/runs/${id}/${k}`) }, label),
  }));
  paintHead();
  paintTabs();

  const body = h("div");
  put(view,
    h("div", { class: "head" }, h("h1", {}, run.name), badge.el, " ", since, h("span", { class: "grow" }), stop,
      !writable ? null : h("button", { class: "danger", onclick: async () => {
        if (!confirm(`Delete run "${run.name}" and everything it logged? This cannot be undone.`)) return;
        await api(`/api/runs/${id}`, { method: "DELETE" });
        navigate(`/projects/${encodeURIComponent(run.project)}`);
      } }, "Delete")),
    tabbar, body);

  // Each tab is a component: built once, then update(run, live) on every poll.
  // Updates add what is new and redraw in place; nothing is torn down, so
  // scroll positions, hover and steps picked by hand stay where they were.
  let content = { update: async () => {} };
  if (tab === "charts") content = runCharts(run, body, (n) => groupOf(n) !== "sys", "step");
  else if (tab === "system") content = runCharts(run, body, (n) => groupOf(n) === "sys", "time");
  else if (tab === "media") content = runMedia(run, body);
  else if (tab === "logs") {
    const more = await runLogs(run, body);
    content = { update: (r, live) => (live ? more() : null) };
  } else if (tab === "model") await runModel(run, body);
  else runOverview(run, body);

  poll(async () => {
    const live = run.status === "RUNNING";  // as of the last poll, so a run's final points still load
    run = await api(`/api/runs/${id}`);
    paintHead();
    paintTabs();
    await content.update(run, live);
  });
}

// The Charts and System tabs: a card per series, grouped by name prefix. A
// series that shows up mid-run gets a card added; existing cards are kept.
function runCharts(run, body, want, axis) {
  const charts = [];
  const toolbar = chartToolbar(charts);
  const sections = h("div");
  const empty = h("div");
  put(body, toolbar, sections, empty);
  const cards = new Set();
  const metric = batched((names) =>
    api(`/api/runs/${run.id}/metrics?${q([...names.map((n) => ["name", n]), ["points", 600], ["x", axis]])}`));

  const makeCard = (series) => {
    let part;
    if (series.kind === "METRIC") {
      const name = series.name;
      const card = chartCard(name);
      card.chart.setOptions({ axis, origin: run.created_at, unit: unitOf(name) });
      charts.push(card.chart);
      const refresh = lazily(card.el, async () => {
        const s = (await metric(name)) || { x: [], y: [] };
        card.chart.setData([{ label: name, color: SERIES[0], ...s }]);
        setText(card.latest, s.y.length ? card.chart.value(s.y[s.y.length - 1]) : "");
        setText(card.note, s.min ? `${s.count.toLocaleString()} points, averaged into ${s.x.length} buckets; band shows min–max` : "");
      });
      part = { el: card.el, refresh, destroy() { refresh.stop(); card.chart.destroy(); charts.splice(charts.indexOf(card.chart), 1); } };
    } else {
      const card = dataCard(run, series);
      const refresh = lazily(card.el, card.load);
      part = { el: card.el, refresh, destroy() { refresh.stop(); card.destroy(); } };
    }
    cards.add(part);
    const destroy = part.destroy;
    part.destroy = () => { destroy(); cards.delete(part); };
    return part;
  };
  const makeSection = ([group]) => {
    const grid = h("div", { class: "grid" });
    return { el: h("div", {}, h("h2", {}, group || "Metrics"), grid),
      update: ([, list]) => reconcile(grid, list, (s) => s.name, makeCard) };
  };

  const paint = (run) => {
    const metrics = run.series.filter((s) => s.kind === "METRIC" && want(s.name));
    const data = axis === "step" ? run.series.filter((s) => DATA.includes(s.kind)) : [];
    const all = [...metrics, ...data];
    const byName = new Map(all.map((s) => [s.name, s]));
    const groups = byGroup(all.map((s) => s.name)).map(([g, names]) => [g, names.map((n) => byName.get(n))]);
    toolbar.hidden = !metrics.length;
    if (all.length) put(empty);
    else put(empty, emptyState(run.status === "RUNNING" ? "Waiting for the first metrics…" : "This run did not log any metrics."));
    reconcile(sections, groups, ([g]) => g, makeSection);
  };
  paint(run);
  return {
    update(run, live) {
      paint(run);
      return live ? Promise.all([...cards].map((c) => c.refresh())) : null;
    },
  };
}

// A stepped series (histograms, tables, media): one frame per logged step,
// with a slider to move through them. It follows the newest step until the
// slider is moved, then holds that step. A frame is drawn only when the step
// on screen changes, never because a poll brought the same data back.
function stepped(render) {
  let items = [];
  let follow = true;
  let shown;
  const id = (item) => item.id ?? `${item.step}:${item.time}`;
  const range = h("input", { type: "range", min: "0", max: "0", value: "0", "aria-label": "Step" });
  const label = h("span", { class: "num" });
  const el = h("div", { class: "slider" }, range, label);
  const show = () => {
    const item = items[+range.value];
    el.hidden = items.length < 2;
    if (!item) return;
    setText(label, `step ${fmtStep(item.step)}`);
    if (id(item) === shown) return;
    shown = id(item);
    render(item);
  };
  range.addEventListener("input", () => { follow = +range.value === items.length - 1; show(); });
  return {
    el,
    set(next) {
      const held = items[+range.value];
      items = next;
      range.max = String(Math.max(0, items.length - 1));
      if (follow || !held) range.value = range.max;
      else {
        // Long series are thinned on read, so the held step can drop out of
        // the window; stay on the closest one that is still there.
        let at = items.findIndex((it) => id(it) === id(held));
        if (at < 0) at = items.reduce((best, it, i) =>
          Math.abs(it.step - held.step) < Math.abs(items[best].step - held.step) ? i : best, 0);
        range.value = String(at);
      }
      show();
    },
  };
}

function dataCard(run, series) {
  const note = h("div", { class: "note" });
  let frame, redraw = () => {}, ro;
  if (series.kind === "HISTOGRAM") {
    frame = h("canvas", { role: "img", "aria-label": `Histogram of ${series.name}` });
    let last = null;
    redraw = (d) => { last = d; drawHistogram(frame, d); };
    ro = new ResizeObserver(() => last && drawHistogram(frame, last));
    ro.observe(frame);
  } else {
    const table = dataTable();
    frame = table.el;
    redraw = table.update;
  }
  // Bodies can be large (a table with long text is easily 100 KB a step), so
  // a load asks only for the ones it lacks, newest first within a budget;
  // a step picked on the slider that is not here yet is fetched on its own.
  const bodies = new Map();
  let newest = 0, wanted = null;
  const steps = stepped((entry) => {
    wanted = entry.id;
    if (bodies.has(entry.id)) return redraw(bodies.get(entry.id) || {});
    frame.setAttribute("aria-busy", "true");
    track(api(`/api/runs/${run.id}/data?${q([["name", series.name], ["entry", entry.id]])}`))
      .then((r) => {
        bodies.set(entry.id, r.entry.data);
        if (wanted === entry.id) redraw(r.entry.data || {});
      })
      .catch(handleError)
      .finally(() => { if (wanted === entry.id) frame.removeAttribute("aria-busy"); });
  });
  const el = gridCard(series.name, "card chart",
    h("span", { class: "latest" }, series.kind === "HISTOGRAM" ? "histogram" : "table"), frame, steps.el, note);
  const load = async () => {
    const r = await api(`/api/runs/${run.id}/data?${q([["name", series.name], ["limit", 64],
      ["budget", 262144], ["after", newest]])}`);
    const ids = new Set(r.entries.map((e) => e.id));
    for (const id of bodies.keys()) if (!ids.has(id)) bodies.delete(id);
    for (const e of r.entries) {
      if ("data" in e) bodies.set(e.id, e.data);
      newest = Math.max(newest, e.id);
    }
    steps.set(r.entries);
    setText(note, r.total > r.entries.length ? `showing ${r.entries.length} of ${r.total} logged steps` : "");
  };
  return { el, load, destroy: () => ro && ro.disconnect() };
}

// A logged mlop.Table. Numbers are right-aligned in tabular figures, text is
// left-aligned and cut to one line; hovering a cut cell shows all of it (see
// peek). The scroll box persists across steps, so moving the slider keeps the
// rows and columns you were looking at in view.
const TABLE_ROWS = 500;

function dataTable() {
  const scroller = h("div", { class: "datatable", tabindex: "0", role: "region", "aria-label": "Table" });
  const note = h("div", { class: "note" });
  const el = h("div", {}, scroller, note);
  const update = (d) => {
    const all = Array.isArray(d.table) ? d.table : [];
    const rows = all.slice(0, TABLE_ROWS).map((r) => (Array.isArray(r) ? r : [r]));
    const cols = Array.isArray(d.col) ? d.col : null;
    const names = Array.isArray(d.row) ? d.row : null;
    const width = Math.max(cols ? cols.length : 0, 0, ...rows.map((r) => r.length));
    const numeric = Array.from({ length: width }, (_, j) =>
      rows.some((r) => typeof r[j] === "number") && rows.every((r) => r[j] == null || typeof r[j] === "number"));
    const span = Array.from({ length: width }, (_, j) => j);
    const table = h("table", {},
      cols ? h("thead", {}, h("tr", {},
        names ? h("th", { class: "corner" }) : null,
        span.map((j) => h("th", { class: numeric[j] ? "n clip" : "clip", scope: "col" }, cols[j] ? String(cols[j].name) : "")),
        h("th", { class: "fill" }))) : null,
      h("tbody", {}, rows.map((r, i) => h("tr", {},
        names ? h("th", { class: "clip", scope: "row" }, names[i] ? String(names[i].name) : "") : null,
        span.map((j) => tableCell(r[j], numeric[j])),
        h("td", { class: "fill" })))));
    const { scrollTop, scrollLeft } = scroller;
    put(scroller, table);
    scroller.scrollTop = scrollTop;
    scroller.scrollLeft = scrollLeft;
    setText(note, all.length > TABLE_ROWS ? `showing the first ${TABLE_ROWS} of ${all.length.toLocaleString()} rows` : "");
  };
  return { el, update };
}

function tableCell(v, numeric) {
  const align = numeric ? "n" : "";
  if (v == null) return h("td", { class: `${align} muted` }, "–");
  if (typeof v === "number") {
    const text = Number.isInteger(v) ? String(v) : fmt(v);
    return h("td", { class: align || "num", "data-full": text === String(v) ? null : String(v) }, text);
  }
  const text = typeof v === "object" ? JSON.stringify(v) : String(v);
  // Cells long enough to be cut get a tab stop, so the full text is reachable by keyboard too.
  return h("td", { class: "clip", tabindex: text.length > 40 ? "0" : null }, text);
}

function runMedia(run, body) {
  const cards = new Set();
  const grid = h("div", { class: "grid" });
  const makeCard = (series) => {
    const frame = h("div", { class: "frame" });
    const caption = h("div", { class: "note" });
    let shown = null;
    const steps = stepped(async (f) => {
      shown = f.id;
      const url = `/api/files/${f.id}`;
      setText(caption, `${f.name} · ${bytes(f.size)}`);
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
    const el = gridCard(series.name, "card media", null, frame, steps.el, caption);
    const refresh = lazily(el, async () => {
      const r = await api(`/api/runs/${run.id}/files?${q([["name", series.name]])}`);
      steps.set(r.files);
      if (!r.files.length) setText(caption, "upload in progress…");
    });
    const part = { el, refresh, destroy() { refresh.stop(); cards.delete(part); } };
    cards.add(part);
    return part;
  };
  const paint = (run) => reconcile(grid, run.series.filter((s) => MEDIA.includes(s.kind)), (s) => s.name, makeCard);
  put(body, grid);
  paint(run);
  return {
    update(run, live) {
      paint(run);
      return live ? Promise.all([...cards].map((c) => c.refresh())) : null;
    },
  };
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

let revealed = null;  // the secret of a key just created or rotated

async function viewSettings() {
  setCrumbs(["Settings"]);
  session = null;
  const me = await who();
  const head = [
    h("div", { class: "head" }, h("h1", {}, "Settings"), h("span", { class: "grow" }),
      me.auth ? h("button", { onclick: async () => { await api("/api/logout", { method: "POST" }); showLogin(); } }, "Sign out") : null),
    h("h2", { style: "margin-top:0" }, "Connect a training script"),
    h("div", { class: "card pad" }, h("pre", {},
      `pip install git+https://github.com/yych42/mini-mlop.git\nexport MINI_MLOP_URL=${me.url}\n${me.auth ? "mini-mlop login   # paste an API key once per machine\n" : ""}`)),
  ];
  if (me.access !== "admin") {
    // Keys, the assistant's provider and storage are for admin keys.
    put(view, head, h("h2", {}, "This key"),
      h("div", { class: "card pad" }, kv({ name: me.key, access: me.access, project: me.project || "all projects" }),
        h("p", { class: "muted" }, "Server settings and API keys are managed with an admin key.")));
    return;
  }
  const [keys, stats, projects] = await Promise.all([api("/api/keys"), api("/api/stats"), api("/api/projects")]);
  const assistant = await agentSettings(me);
  // A key just created or rotated: shown once, above the list, then forgotten.
  const fresh = h("div"), failed = h("div");
  if (revealed) {
    const k = revealed;
    revealed = null;
    put(fresh, h("div", { class: "card pad", style: "margin-bottom:12px" },
      h("b", {}, `Key "${k.name}" ${k.rotated ? "rotated" : "created"}`),
      ` (${k.access}, ${k.project || "all projects"}). Copy it now; it is not shown again.`,
      k.rotated ? " The old secret no longer works: update every script and machine that used it." : null,
      k.current ? " This browser is now signed in with the new secret." : null,
      h("div", { class: "keybox" }, k.key)));
  }
  const name = h("input", { type: "text", placeholder: "Key name, e.g. laptop", "aria-label": "Key name", maxlength: "64" });
  const access = h("select", { "aria-label": "Access" },
    h("option", { value: "admin" }, "Admin: everything"),
    h("option", { value: "write" }, "Write: log, stop and delete runs"),
    h("option", { value: "read" }, "Read: view and query only"));
  const scope = h("input", { type: "text", placeholder: "All projects", "aria-label": "Project", list: "key-projects", maxlength: "200" });
  const syncAccess = () => {
    const bound = !!scope.value.trim();
    access.options[0].disabled = bound;  // admin keys are never tied to a project
    if (bound && access.value === "admin") access.value = "write";
  };
  scope.addEventListener("input", syncAccess);
  put(view, 
    head,
    h("h2", {}, "API keys"),
    me.auth ? null : h("p", { class: "error" }, "Authentication is disabled (--no-auth): anyone who can reach this server can read and write. Keys are not checked."),
    fresh,
    h("div", { class: "card tablewrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Key"), h("th", {}, "Access"), h("th", {}, "Project"), h("th", {}, "Created"), h("th", {}, "Last used"), h("th", {}))),
      h("tbody", {}, keys.keys.map((k) => h("tr", {},
        h("td", {}, k.name, k.id === keys.current ? h("span", { class: "muted" }, "  (this session)") : null),
        h("td", { class: "mono" }, k.hint),
        h("td", {}, k.access),
        h("td", { class: k.project ? "" : "muted" }, k.project || "all"),
        h("td", { class: "muted" }, new Date(k.created_at).toLocaleDateString()),
        h("td", { class: "muted" }, k.last_used_at ? ago(k.last_used_at) : "never"),
        h("td", { class: "actions" },
          h("button", { onclick: async () => {
            const own = k.id === keys.current ? " This browser will switch to the new secret." : "";
            if (!confirm(`Rotate key "${k.name}"? It gets a new secret and the old one stops working at once; ` +
              `scripts and machines using it must be given the new one.${own}`)) return;
            revealed = { ...(await api(`/api/keys/${k.id}/rotate`, { method: "POST" })), rotated: true };
            route();
          } }, "Rotate"),
          k.id === keys.current ? null : h("button", { class: "danger", onclick: async () => {
            if (!confirm(`Revoke key "${k.name}"? Scripts using it will stop being able to log.`)) return;
            await api(`/api/keys/${k.id}`, { method: "DELETE" });
            route();
          } }, "Revoke"))))))),
    h("form", { class: "row", style: "margin-top:12px", onsubmit: async (e) => {
      e.preventDefault();
      try {
        revealed = await api("/api/keys", { method: "POST", body: { name: name.value.trim() || "unnamed", access: access.value, project: scope.value.trim() } });
        route();
      } catch (ex) {
        put(failed, h("p", { class: "error" }, ex.message));
      }
    } }, name, scope, h("datalist", { id: "key-projects" }, projects.projects.map((p) => h("option", { value: p.name }))),
      access, h("button", { class: "primary", type: "submit" }, "Create key")),
    h("p", { class: "muted" }, "Name a project to limit a key to it: the key can then log to, read and ask about that project only. " +
      "Give teammates and training jobs their own keys, and agents a read key. " +
      "Rotate a key to replace its secret, for example the first key, whose secret was printed in the server log."),
    failed,
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

// -- peek: the whole of a cut-off cell ----------------------------------------
// Any element with class "clip" that is cut off, or that carries data-full
// (e.g. a number shown rounded), opens a popover with its full text on hover
// or keyboard focus. The popover stays while the pointer is over it, so long
// text can be scrolled, selected and copied. Wired once, by delegation, so
// cells drawn later need nothing of their own.
const peek = (() => {
  const box = document.getElementById("peek");
  let owner = null, showing = 0, hiding = 0;
  const wants = (el) => el && (el.dataset.full != null || el.scrollWidth > el.clientWidth + 1);
  const cancel = () => { clearTimeout(showing); clearTimeout(hiding); };
  function show(el) {
    cancel();
    owner = el;
    box.textContent = el.dataset.full ?? el.textContent;
    box.hidden = false;
    box.scrollTop = 0;
    const r = el.getBoundingClientRect();
    const w = box.offsetWidth, ht = box.offsetHeight;
    const below = r.bottom + 4, above = r.top - ht - 4;
    box.style.left = `${Math.max(8, Math.min(r.left, innerWidth - w - 8))}px`;
    box.style.top = `${below + ht <= innerHeight - 8 || above < 8 ? Math.max(8, Math.min(below, innerHeight - ht - 8)) : above}px`;
  }
  function hide() {
    cancel();
    owner = null;
    box.hidden = true;
  }
  const later = () => { clearTimeout(hiding); hiding = setTimeout(hide, 180); };
  document.addEventListener("pointerover", (e) => {
    if (box.contains(e.target)) return clearTimeout(hiding);
    const el = e.target.closest && e.target.closest(".clip, [data-full]");
    if (el && el === owner) return clearTimeout(hiding);
    clearTimeout(showing);
    if (wants(el)) showing = setTimeout(() => show(el), owner ? 0 : 280);
    else if (owner) later();
  });
  document.addEventListener("pointerleave", () => owner && later());
  document.addEventListener("focusin", (e) => {
    const el = e.target.closest && e.target.closest(".clip, [data-full]");
    if (wants(el)) show(el);
    else if (owner && !box.contains(e.target)) hide();
  });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && owner) hide(); });
  // The popover is pinned to where the cell was; once the page moves, it is wrong.
  document.addEventListener("scroll", (e) => { if (owner && !box.contains(e.target)) hide(); }, true);
  return { hide };
})();

// -- router ------------------------------------------------------------------
async function route() {
  const mine = ++generation;
  cleanups.splice(0).forEach((fn) => fn());
  document.getElementById("tooltip").hidden = true;
  peek.hide();
  window.dispatchEvent(new Event("routechange"));
  const parts = location.pathname.split("/").filter(Boolean).map(decodeURIComponent);
  const mount = (view = h("div"));
  // The current page stays (faded) until the next one has drawn something,
  // and the title sweeps until the next one has finished loading.
  root.setAttribute("aria-busy", "true");
  loading.begin();
  const attach = new MutationObserver(() => {
    attach.disconnect();
    if (mine === generation) { put(root, mount); root.removeAttribute("aria-busy"); }
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
    put(view, emptyState(e.message, h("p", {}, h("a", { href: "/" }, "Back to projects"))));
  } finally {
    // Hand over to the cards: they start their first loads when they are
    // seen on screen, a frame or two after the view is attached. Ending a
    // little later keeps the sweep going from page to data without a break.
    requestAnimationFrame(() => requestAnimationFrame(() => loading.end()));
    if (mine === generation) root.removeAttribute("aria-busy");
  }
}

initAgent({ h, put, api, who, Unauthorized, onUnauthorized: () => showLogin() });
// Pages have real addresses (/runs/8), so the server knows which one is being
// asked for and can send its data along. Moving between them stays in the
// page: links and navigate() change the address without a reload.
function navigate(path, { replace = false } = {}) {
  if (path === location.pathname + location.search) return route();
  history[replace ? "replaceState" : "pushState"](null, "", path);
  window.scrollTo(0, 0);
  return route();
}

document.addEventListener("click", (e) => {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  const a = e.target.closest && e.target.closest("a[href]");
  if (!a || a.target || a.hasAttribute("download") || a.origin !== location.origin) return;
  if (a.getAttribute("href").startsWith("#") || a.pathname.startsWith("/api/")) return;
  e.preventDefault();
  navigate(a.pathname + a.search);
});
window.addEventListener("popstate", route);
// Addresses used to live after a "#"; old links and bookmarks still work.
if (location.hash.startsWith("#/")) history.replaceState(null, "", location.hash.slice(1));
route();
