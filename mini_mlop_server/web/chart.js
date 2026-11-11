// Canvas charts for the mini-mlop UI: a multi-series line chart with a crosshair
// readout, and a histogram. Small on purpose, so nothing has to be bundled.

const tooltip = document.getElementById("tooltip");
const M = { left: 58, right: 12, top: 8, bottom: 24 };

export function fmt(v) {
  if (v == null || Number.isNaN(v)) return "–";
  if (v === 0) return "0";
  const a = Math.abs(v);
  if (a >= 1e6 || a < 1e-4) return v.toExponential(3).replace(/\.?0+e/, "e");
  if (Number.isInteger(v)) return v.toLocaleString();
  return String(+v.toPrecision(5));
}

export function fmtStep(v) {
  const a = Math.abs(v);
  if (a >= 1e9) return `${+(v / 1e9).toPrecision(3)}B`;
  if (a >= 1e6) return `${+(v / 1e6).toPrecision(3)}M`;
  if (a >= 1e4) return `${+(v / 1e3).toPrecision(3)}k`;
  return String(Math.round(v));
}

export function fmtDuration(ms) {
  const s = Math.round(ms / 1000);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`;
}

function fmtBytes(v, digits = 3) {
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let i = 0, a = Math.abs(v);
  while (a >= 1024 && i < units.length - 1) { a /= 1024; v /= 1024; i++; }
  return `${+v.toPrecision(digits)} ${units[i]}`;
}

const UNITS = {
  bytes: { tick: (v) => fmtBytes(v, 5), value: (v) => fmtBytes(v, 4) },
  percent: { tick: (v) => `${+v.toPrecision(3)}%`, value: (v) => `${+v.toPrecision(4)}%` },
};

function axisTick(v) {
  if (v === 0) return "0";
  const a = Math.abs(v);
  if (a >= 1e5 || a < 1e-3) return v.toExponential(1).replace(/\.0e/, "e").replace("e+", "e");
  return String(+v.toPrecision(4));
}

function niceTicks(lo, hi, count) {
  const span = hi - lo || 1;
  const raw = span / Math.max(1, count);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw);
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(+v.toPrecision(12));
  return out;
}

function cssVar(el, name) {
  return getComputedStyle(el).getPropertyValue(name).trim();
}

function resolveColor(el, c) {
  return c && c.startsWith("--") ? cssVar(el, c) : c;
}

function fit(canvas) {
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, hgt = canvas.clientHeight;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(hgt * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(hgt * dpr);
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, hgt);
  return { ctx, w, h: hgt };
}

// Exponential moving average with bias correction, so the smoothed line
// starts on the data instead of being dragged toward zero.
function smooth(ys, alpha) {
  if (!alpha) return ys;
  const out = new Array(ys.length);
  let acc = 0, weight = 0;
  for (let i = 0; i < ys.length; i++) {
    acc = acc * alpha + ys[i] * (1 - alpha);
    weight = weight * alpha + (1 - alpha);
    out[i] = acc / weight;
  }
  return out;
}

function nearest(xs, x) {
  let lo = 0, hi = xs.length - 1;
  if (hi < 0) return -1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (xs[mid] < x) lo = mid; else hi = mid;
  }
  return Math.abs(xs[lo] - x) <= Math.abs(xs[hi] - x) ? lo : hi;
}

export class LineChart {
  constructor(canvas, options = {}) {
    this.canvas = canvas;
    this.options = { smooth: 0, logY: false, axis: "step", origin: 0, unit: null, ...options };
    this.series = [];
    this.hoverX = null;
    this._draw = this.draw.bind(this);
    this._ro = new ResizeObserver(() => this.schedule());
    this._ro.observe(canvas);
    this._theme = window.matchMedia("(prefers-color-scheme: dark)");
    this._theme.addEventListener("change", this._draw);
    canvas.addEventListener("pointermove", (e) => this.hover(e.offsetX, e));
    canvas.addEventListener("pointerleave", () => this.hover(null));
    canvas.addEventListener("blur", () => this.hover(null));
    canvas.addEventListener("keydown", (e) => this.key(e));
  }

  destroy() {
    this._ro.disconnect();
    this._theme.removeEventListener("change", this._draw);
  }

  value(v) {
    return (UNITS[this.options.unit] || { value: fmt }).value(v);
  }

  setOptions(options) {
    Object.assign(this.options, options);
    this.schedule();
  }

  setData(series) {
    this.series = series.filter((s) => s.x && s.x.length);
    this.schedule();
  }

  schedule() {
    if (this._raf) return;
    this._raf = requestAnimationFrame(() => {
      this._raf = 0;
      this.draw();
    });
  }

  // Positions and values as plotted (after smoothing / log filtering).
  plotted() {
    const { smooth: alpha, logY } = this.options;
    return this.series.map((s) => {
      const y = smooth(s.y, alpha);
      return { ...s, ys: y, ok: (i) => !logY || y[i] > 0 };
    });
  }

  draw() {
    const { ctx, w, h } = fit(this.canvas);
    if (!w || !h) return;
    const el = this.canvas;
    const text2 = cssVar(el, "--text-3"), grid = cssVar(el, "--grid"), ink = cssVar(el, "--text-2");
    const pw = w - M.left - M.right, ph = h - M.top - M.bottom;
    ctx.font = "11px system-ui, sans-serif";
    const series = this.plotted();
    this.scale = null;

    if (!series.length) {
      ctx.fillStyle = text2;
      ctx.textAlign = "center";
      ctx.fillText("no data yet", w / 2, h / 2);
      return;
    }

    const { logY, axis, origin } = this.options;
    let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
    for (const s of series) {
      x0 = Math.min(x0, s.x[0]);
      x1 = Math.max(x1, s.x[s.x.length - 1]);
      const banded = s.min && !this.options.smooth;
      for (let i = 0; i < s.ys.length; i++) {
        if (!s.ok(i)) continue;
        const lo = banded && (!logY || s.min[i] > 0) ? s.min[i] : s.ys[i];
        const hi = banded ? s.max[i] : s.ys[i];
        if (lo < y0) y0 = lo;
        if (hi > y1) y1 = hi;
      }
    }
    if (!Number.isFinite(y0)) {
      ctx.fillStyle = text2;
      ctx.textAlign = "center";
      ctx.fillText("no positive values to show on a log scale", w / 2, h / 2);
      return;
    }
    const ty = logY ? Math.log10 : (v) => v;
    let a = ty(y0), b = ty(y1);
    if (a === b) { a -= Math.abs(a) * 0.05 || 0.5; b += Math.abs(b) * 0.05 || 0.5; }
    const pad = (b - a) * 0.06;
    a -= pad; b += pad;
    if (x0 === x1) { x0 -= 0.5; x1 += 0.5; }
    const X = (x) => M.left + ((x - x0) / (x1 - x0)) * pw;
    const Y = (y) => M.top + (1 - (ty(y) - a) / (b - a)) * ph;
    this.scale = { X, x0, x1, pw };

    // grid and axes: recessive, hairline
    ctx.lineWidth = 1;
    ctx.strokeStyle = grid;
    ctx.fillStyle = text2;
    ctx.textBaseline = "middle";
    ctx.textAlign = "right";
    const yt = logY
      ? niceTicks(a, b, 4).filter((t) => Number.isInteger(t) || b - a < 2).map((t) => 10 ** t)
      : niceTicks(a, b, 4);
    for (const t of yt) {
      const y = Math.round(Y(t)) + 0.5;
      ctx.beginPath(); ctx.moveTo(M.left, y); ctx.lineTo(w - M.right, y); ctx.stroke();
      ctx.fillText((UNITS[this.options.unit] || { tick: axisTick }).tick(t), M.left - 6, y);
    }
    ctx.textBaseline = "top";
    ctx.textAlign = "center";
    let lastLabel = null;
    const xLabel = axis === "time" ? (v) => fmtDuration(Math.max(0, v - origin)) : fmtStep;
    const xt = axis === "time"
      ? niceTicks(x0 - origin, x1 - origin, Math.max(2, Math.floor(pw / 90))).map((t) => t + origin)
      : niceTicks(x0, x1, Math.max(2, Math.floor(pw / 70))).filter((t) => Number.isInteger(t));
    for (const t of xt) {
      const x = X(t);
      if (x < M.left - 1 || x > w - M.right + 1 || xLabel(t) === lastLabel) continue;
      lastLabel = xLabel(t);
      ctx.fillText(xLabel(t), Math.min(Math.max(x, M.left + 12), w - M.right - 14), h - M.bottom + 6);
    }

    ctx.save();
    ctx.beginPath();
    ctx.rect(M.left, M.top - 2, pw, ph + 4);
    ctx.clip();
    for (const s of series) {
      const color = resolveColor(el, s.color) || ink;
      // min–max envelope of downsampled buckets: raw extremes, so it is only
      // shown while the line itself is unsmoothed
      if (s.min && !this.options.smooth) {
        ctx.beginPath();
        let started = false;
        for (let i = 0; i < s.x.length; i++) {
          if (logY && s.min[i] <= 0) continue;
          started ? ctx.lineTo(X(s.x[i]), Y(s.max[i])) : ctx.moveTo(X(s.x[i]), Y(s.max[i]));
          started = true;
        }
        for (let i = s.x.length - 1; i >= 0; i--) {
          if (logY && s.min[i] <= 0) continue;
          ctx.lineTo(X(s.x[i]), Y(s.min[i]));
        }
        ctx.closePath();
        ctx.globalAlpha = 0.14;
        ctx.fillStyle = color;
        ctx.fill();
        ctx.globalAlpha = 1;
      }
      ctx.beginPath();
      ctx.lineWidth = 2;
      ctx.lineJoin = "round";
      ctx.strokeStyle = color;
      let pen = false;
      for (let i = 0; i < s.x.length; i++) {
        if (!s.ok(i)) { pen = false; continue; }
        pen ? ctx.lineTo(X(s.x[i]), Y(s.ys[i])) : ctx.moveTo(X(s.x[i]), Y(s.ys[i]));
        pen = true;
      }
      ctx.stroke();
      if (s.x.length === 1 && s.ok(0)) {
        ctx.beginPath();
        ctx.arc(X(s.x[0]), Y(s.ys[0]), 4, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
      }
    }
    ctx.restore();

    if (this.hoverX != null) {
      const x = Math.round(X(this.hoverX)) + 0.5;
      ctx.strokeStyle = text2;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x, M.top); ctx.lineTo(x, h - M.bottom); ctx.stroke();
      const surface = cssVar(el, "--surface");
      for (const s of series) {
        const i = nearest(s.x, this.hoverX);
        if (i < 0 || !s.ok(i)) continue;
        ctx.beginPath();
        ctx.arc(X(s.x[i]), Y(s.ys[i]), 4, 0, Math.PI * 2);
        ctx.fillStyle = resolveColor(el, s.color) || ink;
        ctx.fill();
        ctx.lineWidth = 2;
        ctx.strokeStyle = surface;
        ctx.stroke();
      }
    }
  }

  // The crosshair snaps to the data position nearest the pointer; the
  // readout lists every series there, so nobody has to aim at a 2px line.
  hover(px, event) {
    if (px == null || !this.scale || !this.series.length) {
      this.hoverX = null;
      tooltip.hidden = true;
      return this.schedule();
    }
    const { x0, x1, pw } = this.scale;
    const want = x0 + ((px - M.left) / pw) * (x1 - x0);
    let best = null;
    for (const s of this.series) {
      const i = nearest(s.x, want);
      if (i >= 0 && (best == null || Math.abs(s.x[i] - want) < Math.abs(best - want))) best = s.x[i];
    }
    this.hoverX = best;
    this.schedule();
    this.readout(event);
  }

  key(e) {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    const xs = [...new Set(this.series.flatMap((s) => s.x))].sort((p, q) => p - q);
    if (!xs.length) return;
    e.preventDefault();
    let i = this.hoverX == null ? xs.length - 1 : nearest(xs, this.hoverX) + (e.key === "ArrowRight" ? 1 : -1);
    this.hoverX = xs[Math.min(xs.length - 1, Math.max(0, i))];
    this.schedule();
    this.readout(null);
  }

  readout(event) {
    const { axis, origin } = this.options;
    const head = document.createElement("div");
    head.className = "x";
    head.textContent = axis === "time"
      ? `${fmtDuration(this.hoverX - origin)} in · ${new Date(this.hoverX).toLocaleTimeString()}`
      : `step ${Math.round(this.hoverX).toLocaleString()}`;
    const rows = [head];
    for (const s of this.plotted()) {
      const i = nearest(s.x, this.hoverX);
      if (i < 0) continue;
      const row = document.createElement("div");
      row.className = "row";
      const key = document.createElement("span");
      key.className = "swatch";
      key.style.background = resolveColor(this.canvas, s.color) || "";
      const value = document.createElement("b");
      value.textContent = this.value(s.ys[i]);
      const name = document.createElement("span");
      name.className = "n";
      name.textContent = s.label;
      row.append(key, value, name);
      rows.push(row);
    }
    tooltip.replaceChildren(...rows);
    tooltip.hidden = false;
    const box = this.canvas.getBoundingClientRect();
    const tw = tooltip.offsetWidth, th = tooltip.offsetHeight;
    const cx = event ? event.clientX : box.left + (this.scale ? this.scale.X(this.hoverX) : 0);
    const cy = event ? event.clientY : box.top + 30;
    let left = cx + 14;
    if (left + tw > window.innerWidth - 8) left = cx - tw - 14;
    tooltip.style.left = `${Math.max(8, left)}px`;
    tooltip.style.top = `${Math.max(8, Math.min(cy - th / 2, window.innerHeight - th - 8))}px`;
  }
}

// One step of a logged histogram: bars on a shared baseline, a hairline
// surface gap between neighbours, value on hover.
export function drawHistogram(canvas, d) {
  const freq = Array.isArray(d.freq) ? d.freq : [];
  let edges;
  if (d.bins && typeof d.bins === "object" && !Array.isArray(d.bins)) {
    const { min, max, num } = d.bins;
    edges = Array.from({ length: num + 1 }, (_, i) => min + ((max - min) * i) / num);
  } else edges = Array.isArray(d.bins) ? d.bins : [];
  const paint = (hot) => {
    const { ctx, w, h } = fit(canvas);
    if (!w || !h) return;
    const pw = w - M.left - M.right, ph = h - M.top - M.bottom;
    const top = Math.max(1, ...freq);
    ctx.font = "11px system-ui, sans-serif";
    ctx.fillStyle = cssVar(canvas, "--text-3");
    ctx.strokeStyle = cssVar(canvas, "--grid");
    ctx.textBaseline = "middle";
    ctx.textAlign = "right";
    for (const t of niceTicks(0, top, 3)) {
      const y = Math.round(M.top + (1 - t / top) * ph) + 0.5;
      ctx.beginPath(); ctx.moveTo(M.left, y); ctx.lineTo(w - M.right, y); ctx.stroke();
      ctx.fillText(fmtStep(t), M.left - 6, y);
    }
    ctx.textBaseline = "top";
    if (edges.length > 1) {
      ctx.textAlign = "left";
      ctx.fillText(axisTick(edges[0]), M.left, h - M.bottom + 6);
      ctx.textAlign = "right";
      ctx.fillText(axisTick(edges[edges.length - 1]), w - M.right, h - M.bottom + 6);
    }
    const bw = pw / Math.max(1, freq.length);
    const gap = bw > 4 ? 1 : 0;
    const color = cssVar(canvas, "--s1");
    freq.forEach((f, i) => {
      const bh = (f / top) * ph;
      ctx.globalAlpha = hot == null || hot === i ? 1 : 0.55;
      ctx.fillStyle = color;
      ctx.fillRect(M.left + i * bw + gap / 2, M.top + ph - bh, Math.max(1, bw - gap), bh);
    });
    ctx.globalAlpha = 1;
  };
  paint(null);
  canvas.onpointermove = (e) => {
    const pw = canvas.clientWidth - M.left - M.right;
    const i = Math.floor(((e.offsetX - M.left) / pw) * freq.length);
    if (i < 0 || i >= freq.length) return canvas.onpointerleave();
    paint(i);
    const head = document.createElement("div");
    head.className = "x";
    head.textContent = edges.length > i + 1 ? `${axisTick(edges[i])} to ${axisTick(edges[i + 1])}` : `bin ${i}`;
    const value = document.createElement("b");
    value.textContent = `${freq[i].toLocaleString()} values`;
    tooltip.replaceChildren(head, value);
    tooltip.hidden = false;
    tooltip.style.left = `${Math.min(e.clientX + 14, window.innerWidth - tooltip.offsetWidth - 8)}px`;
    tooltip.style.top = `${Math.max(8, e.clientY - tooltip.offsetHeight - 10)}px`;
  };
  canvas.onpointerleave = () => {
    paint(null);
    tooltip.hidden = true;
  };
}
