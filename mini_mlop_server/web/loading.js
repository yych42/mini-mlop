// Loading indicator: while a page or an action is loading, a band of symbols
// sweeps across the "mini-mlop" title in the top bar, like a progress bar.
// Quick loads show nothing; background refreshes never call begin().

const WORD = "mini-mlop";
const GLYPHS = "<>/\\[]{}=+*#%~^";
const DELAY = 150;          // ms before the effect appears
const MIN_SHOWN = 400;      // ms it stays once it has appeared, so it never blinks
const FRAME = 55;           // ms between glyph changes
const SWEEP = 70;           // ms per letter the band travels
const SETTLE = 45;          // ms per letter while the word locks back in

const word = document.getElementById("brand-word");
const reduced = matchMedia("(prefers-reduced-motion: reduce)");

let pending = 0, timer = 0, raf = 0, startedAt = 0, settleAt = 0, lastFrame = 0, pulse = null;

const pick = () => GLYPHS[Math.floor(Math.random() * GLYPHS.length)];

function render(t) {
  word.replaceChildren();
  const settled = settleAt ? Math.floor((performance.now() - settleAt) / SETTLE) : 0;
  const band = (t / SWEEP) % (WORD.length + 5) - 2;
  for (let i = 0; i < WORD.length; i++) {
    if (t != null && i >= settled && Math.abs(i - band) < 1.6) {
      const g = document.createElement("span");
      g.className = "g";
      g.textContent = pick();
      word.append(g);
    } else {
      word.append(WORD[i]);
    }
  }
  return settled;
}

function tick(now) {
  if (!startedAt) return;
  if (now - lastFrame >= FRAME) {
    lastFrame = now;
    if (render(now - startedAt) >= WORD.length) return finish();
  }
  raf = requestAnimationFrame(tick);
}

function start() {
  startedAt = performance.now();
  settleAt = 0;
  if (reduced.matches) {
    pulse = word.animate([{ opacity: 1 }, { opacity: 0.45 }, { opacity: 1 }], { duration: 1100, iterations: Infinity });
  } else {
    raf = requestAnimationFrame(tick);
  }
}

function settle() {
  if (pending) return;  // something new started meanwhile
  if (pulse) return finish();
  settleAt = performance.now();
}

function finish() {
  cancelAnimationFrame(raf);
  pulse?.cancel();
  pulse = null;
  startedAt = settleAt = 0;
  render(null);
}

// Call begin() when something starts loading and end() when it is done;
// overlapping loads are counted.
export function begin() {
  if (pending++ > 0) return;
  settleAt = 0;  // a load starting mid-settle keeps the sweep going
  if (!startedAt) timer = setTimeout(start, DELAY);
}

export function end() {
  if (pending === 0 || --pending > 0) return;
  clearTimeout(timer);
  if (!startedAt) return;
  setTimeout(settle, Math.max(0, MIN_SHOWN - (performance.now() - startedAt)));
}

