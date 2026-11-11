// The agent side panel and its settings. Everything the model writes is
// rendered by building DOM nodes from a small Markdown subset; nothing it
// produces is ever treated as HTML.

let h, put, api, who, Unauthorized, onUnauthorized;
let panel, log, input, sendButton, picker, scopePicker, chatId = null, busy = false;
let scope = null;  // project a new conversation is confined to; null = all projects

export function initAgent(deps) {
  ({ h, put, api, who, Unauthorized, onUnauthorized } = deps);
  document.getElementById("nav-ask").addEventListener("click", (e) => {
    e.preventDefault();
    toggle();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && panel && !panel.hidden) toggle(false);
  });
  // Moving to another project starts from that project, unless a conversation is open.
  window.addEventListener("hashchange", async () => {
    if (!panel || panel.hidden || chatId || busy) return;
    await followPage();
    greet();
    loadChats().catch(() => {});
  });
}

// -- Markdown (subset) → DOM ----------------------------------------------------
function inline(text) {
  const out = [];
  const re = /(`[^`\n]+`)|\*\*([^*\n]+)\*\*|\*([^*\n]+)\*|\[([^\]\n]+)\]\(([^)\s]+)\)|\b(run (\d+))\b/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    if (m[1]) out.push(h("code", {}, m[1].slice(1, -1)));
    else if (m[2]) out.push(h("strong", {}, inline(m[2])));
    else if (m[3]) out.push(h("em", {}, inline(m[3])));
    else if (m[4]) {
      const ok = /^(https?:\/\/|#\/)/.test(m[5]);
      out.push(ok ? h("a", m[5].startsWith("#") ? { href: m[5] } : { href: m[5], target: "_blank", rel: "noopener noreferrer" }, m[4]) : m[0]);
    } else if (m[6]) out.push(h("a", { href: `#/runs/${m[7]}` }, m[6]));
    last = re.lastIndex;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

const cells = (line) => line.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());

export function markdown(source) {
  const lines = String(source).replace(/\r/g, "").split("\n");
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) { i++; continue; }
    if (/^```/.test(line)) {
      const body = [];
      for (i++; i < lines.length && !/^```/.test(lines[i]); i++) body.push(lines[i]);
      i++;
      out.push(h("pre", {}, body.join("\n")));
    } else if (/^#{1,6}\s/.test(line)) {
      const level = Math.min(line.match(/^#+/)[0].length + 2, 5);
      out.push(h(`h${level}`, {}, inline(line.replace(/^#+\s*/, ""))));
      i++;
    } else if (line.includes("|") && /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(lines[i + 1] || "")) {
      const head = cells(line), rows = [];
      for (i += 2; i < lines.length && lines[i].includes("|") && lines[i].trim(); i++) rows.push(cells(lines[i]));
      out.push(h("div", { class: "tablewrap" }, h("table", {},
        h("thead", {}, h("tr", {}, head.map((c) => h("th", {}, inline(c))))),
        h("tbody", {}, rows.map((r) => h("tr", {}, r.map((c) => h("td", {}, inline(c)))))))));
    } else if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d/.test(line), items = [];
      for (; i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i]); i++)
        items.push(h("li", {}, inline(lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/, ""))));
      out.push(h(ordered ? "ol" : "ul", {}, items));
    } else {
      const para = [];
      for (; i < lines.length && lines[i].trim() && !/^(```|#{1,6}\s|\s*([-*+]|\d+[.)])\s+)/.test(lines[i]); i++) {
        if (para.length && lines[i].includes("|") && /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(lines[i + 1] || "")) break;
        para.push(lines[i]);
      }
      out.push(h("p", {}, inline(para.join("\n"))));
    }
  }
  return out;
}

// -- panel ----------------------------------------------------------------------
function toolStep(call) {
  const what = call.name === "query" ? "Query" : call.name === "run_python" ? "Python" : call.name;
  const status = h("span", { class: "muted" }, call.summary ? ` · ${call.summary}` : " · running…");
  const body = call.name === "run_python"
    ? [(call.input.inputs || []).map((f) => h("pre", {}, `-- ${f.name}\n${f.sql}`)), h("pre", {}, call.input.code || "")]
    : h("pre", {}, call.input.sql || JSON.stringify(call.input, null, 1));
  const el = h("details", { class: "step" + (call.is_error ? " failed" : "") }, h("summary", {}, what, status), body);
  el.finish = (summary, failed) => {
    status.textContent = ` · ${summary}`;
    el.classList.toggle("failed", !!failed);
  };
  return el;
}

function addMessage(role, ...kids) {
  const el = h("div", { class: `msg ${role}` }, kids);
  log.append(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

function greet() {
  put(log, h("div", { class: "msg hint" },
    h("p", {}, scope === null
      ? "Ask about your runs across all projects. The assistant reads the data with SQL and cannot change anything."
      : ["Ask about the runs in ", h("strong", {}, scope), ". The assistant sees only this project, reads the data with SQL and cannot change anything."]),
    h("ul", {}, ["Which run in this project has the best validation loss?",
      "Did the loss change significantly after step 1000?",
      "Summarise what went wrong in this run."].map((q) =>
      h("li", {}, h("button", { type: "button", class: "linklike", onclick: () => { input.value = q; input.focus(); } }, q))))));
}

// The project shown on the current page, or null.
async function pageProject() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[0] === "projects" && parts[1]) return parts[1];
  if (parts[0] === "runs" && /^\d+$/.test(parts[1] || "")) {
    try { return (await api(`/api/runs/${parts[1]}`)).project || null; } catch { return null; }
  }
  return null;
}

async function loadScopes() {
  const { projects } = await api("/api/projects");
  const names = projects.map((p) => p.name);
  const bound = (await who()).project;  // a project key never leaves its project
  if (bound) scope = bound;
  else if (scope !== null && !names.includes(scope)) scope = null;
  put(scopePicker, bound ? null : h("option", { value: "" }, "All projects"),
    (bound ? [bound] : names).map((n) => h("option", { value: `p:${n}` }, n)));
  scopePicker.value = scope === null ? "" : `p:${scope}`;
}

async function followPage() {
  scope = await pageProject();
  await loadScopes();
}

async function loadChats() {
  const { chats } = await api("/api/chats");
  put(picker, h("option", { value: "" }, "New conversation"),
    chats.filter((c) => (c.project ?? null) === scope).map((c) => h("option", { value: String(c.id) }, c.title)));
  picker.value = chatId ? String(chatId) : "";
}

async function openChat(id) {
  chatId = id || null;
  if (!chatId) return greet();
  const chat = await api(`/api/chats/${chatId}`);
  scope = chat.project ?? null;
  scopePicker.value = scope === null ? "" : `p:${scope}`;
  put(log);
  for (const m of chat.messages) {
    if (m.role === "user") addMessage("user", m.text);
    else addMessage("assistant", (m.tools || []).length || m.text ? [m.text ? markdown(m.text) : null, (m.tools || []).map(toolStep)] : null);
  }
}

async function send(text) {
  if (busy || !text.trim()) return;
  busy = true;
  sendButton.disabled = true;
  if (!chatId) put(log);
  addMessage("user", text);
  const working = addMessage("assistant pending", "Thinking…");
  const steps = new Map();
  try {
    const res = await fetch("/api/chat", {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, chat_id: chatId, project: scope, context: { route: location.hash } }),
    });
    if (res.status === 401) return onUnauthorized();
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || `request failed (${res.status})`);
    const reader = res.body.getReader(), decoder = new TextDecoder();
    let buffer = "", finished = false;
    const handle = (ev) => {
      if (ev.type === "chat") chatId = ev.id;
      else if (ev.type === "text") log.insertBefore(h("div", { class: "msg assistant" }, markdown(ev.text)), working);
      else if (ev.type === "tool_call") {
        const el = toolStep(ev);
        steps.set(ev.id, el);
        log.insertBefore(h("div", { class: "msg assistant" }, el), working);
      } else if (ev.type === "tool_result") steps.get(ev.id)?.finish(ev.summary, ev.is_error);
      else if (ev.type === "notice") log.insertBefore(h("div", { class: "msg hint" }, ev.text), working);
      else if (ev.type === "error") log.insertBefore(h("div", { class: "msg error" }, ev.text), working);
      else if (ev.type === "done") finished = true;
      log.scrollTop = log.scrollHeight;
    };
    for (;;) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines) if (line.trim()) handle(JSON.parse(line));
      if (done) break;
    }
    if (!finished) throw new Error("the connection closed before the answer finished");
  } catch (e) {
    log.insertBefore(h("div", { class: "msg error" }, e.message), working);
  } finally {
    working.remove();
    busy = false;
    sendButton.disabled = false;
    loadChats().catch(() => {});
  }
}

async function build() {
  log = h("div", { class: "agent-log", "aria-live": "polite" });
  input = h("textarea", { rows: 2, placeholder: "Ask about your runs…", "aria-label": "Message" });
  sendButton = h("button", { class: "primary", type: "submit" }, "Send");
  picker = h("select", { "aria-label": "Conversation", onchange: () => openChat(+picker.value || null) });
  scopePicker = h("select", { "aria-label": "Project", title: "The project this conversation can see", onchange: () => {
    scope = scopePicker.value ? scopePicker.value.slice(2) : null;
    chatId = null;
    greet();
    loadChats();
  } });
  const form = h("form", { class: "agent-form", onsubmit: (e) => { e.preventDefault(); const t = input.value; input.value = ""; send(t); } },
    input, sendButton);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  });
  panel = h("aside", { id: "agent", "aria-label": "Assistant" },
    h("div", { class: "agent-head" }, h("strong", {}, "Ask"), scopePicker, picker, h("span", { class: "grow" }),
      h("button", { type: "button", title: "Delete this conversation", onclick: async () => {
        if (!chatId || !confirm("Delete this conversation?")) return;
        await api(`/api/chats/${chatId}`, { method: "DELETE" });
        chatId = null; greet(); loadChats();
      } }, "Delete"),
      h("button", { type: "button", "aria-label": "Close", onclick: () => toggle(false) }, "✕")),
    log, form);
  document.body.append(panel);
}

async function toggle(force) {
  try {
    if (!panel) { await build(); panel.hidden = true; }
    const show = force ?? panel.hidden;
    panel.hidden = !show;
    document.body.classList.toggle("agent-open", show);
    if (!show) return;
    const { ready } = await api("/api/agent/config");
    if (!ready) {
      put(log, h("div", { class: "msg hint" }, h("p", {}, "The assistant needs a language-model provider and a model before it can answer."),
        h("p", {}, h("a", { href: "#/settings", onclick: () => toggle(false) }, "Set one up in Settings"))));
      sendButton.disabled = true;
      return;
    }
    sendButton.disabled = busy;
    if (!chatId && !busy) await followPage(); else await loadScopes();
    if (!log.childNodes.length || log.querySelector(".hint")) await openChat(chatId);
    await loadChats();
    input.focus();
  } catch (e) {
    if (e instanceof Unauthorized) return onUnauthorized();
    throw e;
  }
}

// -- settings section -------------------------------------------------------------
export async function agentSettings(me) {
  const state = await api("/api/agent/config");
  const cfg = state.config;
  const field = (attrs) => h("input", { type: "text", ...attrs });
  const provider = h("select", { "aria-label": "Model provider" },
    h("option", { value: "" }, "Choose a provider…"),
    state.providers.map((p) => h("option", { value: p.id }, p.label)));
  provider.value = cfg.llm.provider || "";
  const baseUrl = field({ "aria-label": "Base URL", value: cfg.llm.base_url || "" });
  const key = h("input", { type: "password", "aria-label": "Provider API key", autocomplete: "off" });
  const model = field({ "aria-label": "Model", value: cfg.llm.model || "", list: "agent-models" });
  const models = h("datalist", { id: "agent-models" });
  const note = h("p", { class: "muted" });
  const limit = h("input", { type: "text", inputmode: "numeric", "aria-label": "Daily token limit", value: String(cfg.daily_token_limit) });
  const box = h("select", { "aria-label": "Sandbox" }, state.sandboxes.map((s) => h("option", { value: s.id }, s.label)));
  box.value = cfg.sandbox.provider || "none";
  const boxNote = h("p", { class: "muted" }), boxFields = h("div", { class: "formgrid" });
  const boxInputs = {};
  const status = h("span", { class: "muted" });

  function showProvider() {
    const p = state.providers.find((x) => x.id === provider.value);
    note.textContent = p ? p.note : "";
    baseUrl.placeholder = p?.base_url || "https://…/v1";
    baseUrl.closest("label").hidden = !p || !!p.base_url;
    key.placeholder = cfg.llm.api_key_hint && provider.value === cfg.llm.provider ? `saved (${cfg.llm.api_key_hint}); leave blank to keep` : p?.key_hint || "";
    model.placeholder = p?.model_hint || "";
  }
  function showBox() {
    const s = state.sandboxes.find((x) => x.id === box.value);
    boxNote.textContent = s.isolation;
    put(boxFields, s.fields.map((f) => {
      const saved = cfg.sandbox[`${f.name}_hint`];
      boxInputs[f.name] = h("input", { type: f.secret ? "password" : "text", autocomplete: "off",
        value: f.secret ? "" : cfg.sandbox[f.name] || "", placeholder: f.secret && saved ? `saved (${saved}); leave blank to keep` : "" });
      return h("label", {}, h("span", {}, f.label), boxInputs[f.name]);
    }));
  }
  const body = () => {
    const llm = { provider: provider.value, base_url: baseUrl.closest("label").hidden ? "" : baseUrl.value, model: model.value };
    if (key.value) llm.api_key = key.value;
    const sandbox = { provider: box.value };
    for (const [name, el] of Object.entries(boxInputs)) if (el.isConnected && (el.type !== "password" || el.value)) sandbox[name] = el.value;
    return { llm, sandbox, daily_token_limit: Number(limit.value) || 0 };
  };
  const save = async () => {
    const saved = await api("/api/agent/config", { method: "PUT", body: body() });
    Object.assign(cfg.llm, saved.config.llm);
    Object.assign(cfg.sandbox, saved.config.sandbox);
    key.value = "";
    showProvider();
    showBox();
  };

  provider.addEventListener("change", showProvider);
  box.addEventListener("change", showBox);
  const el = h("div", {},
    h("h2", {}, "Assistant"),
    h("form", { class: "card pad", onsubmit: async (e) => {
      e.preventDefault();
      status.textContent = "Saving…";
      try { await save(); status.textContent = "Saved."; } catch (ex) { status.textContent = ex.message; }
    } },
      h("p", { class: "muted", style: "margin-top:0" }, "The assistant answers questions about your runs from the Ask panel. " +
        "It sends your question, plus the run names, configs, metric values and log lines it looks up, to the model provider you choose here."),
      h("div", { class: "formgrid" },
        h("label", {}, h("span", {}, "Provider"), provider),
        h("label", {}, h("span", {}, "Base URL"), baseUrl),
        h("label", {}, h("span", {}, "API key"), key),
        h("label", {}, h("span", {}, "Model"), h("div", { class: "row" }, model, models,
          h("button", { type: "button", onclick: async () => {
            status.textContent = "Saving and loading models…";
            try {
              await save();
              const r = await api("/api/agent/models");
              put(models, r.models.map((m) => h("option", { value: m })));
              status.textContent = `${r.models.length} models available; type to search.`;
              model.focus();
            } catch (ex) { status.textContent = ex.message; }
          } }, "Load models")))),
      note,
      h("h3", {}, "Code sandbox"),
      h("div", { class: "formgrid" }, h("label", {}, h("span", {}, "Where analysis code runs"), box)),
      boxNote, boxFields,
      h("h3", {}, "Spending"),
      h("div", { class: "formgrid" }, h("label", {}, h("span", {}, "Daily token limit (0 = none)"), limit)),
      h("p", { class: "muted" }, `Used today: ${state.tokens_today.toLocaleString()} tokens. Saved keys are stored encrypted and are never shown again. ` +
        (state.encryption === "env" ? "They are unlocked by MINI_MLOP_SECRET_KEY."
          : "They are unlocked by a key file next to the database; set MINI_MLOP_SECRET_KEY on the server to protect copies and backups too.")),
      state.unreadable.length ? h("p", { class: "error" }, "A saved key can no longer be decrypted (was MINI_MLOP_SECRET_KEY changed?). Enter it again and save.") : null,
      h("div", { class: "row" }, h("button", { class: "primary", type: "submit" }, "Save"), status)),
    h("h2", {}, "Connect an outside agent"),
    h("div", { class: "card pad" },
      h("p", { class: "muted", style: "margin-top:0" }, "Coding agents can query this server directly: over MCP, or with the command line."),
      h("pre", {}, `# MCP (Streamable HTTP): ${me.url}/mcp, with an API key as a bearer token, e.g.\n` +
        `claude mcp add --transport http mini-mlop ${me.url}/mcp --header "Authorization: Bearer <API key>"\n\n` +
        `# or the CLI\nmini-mlop query          # describes the tables\nmini-mlop query "SELECT id, project, name, status FROM runs"`)));
  showProvider();
  showBox();
  return el;
}
