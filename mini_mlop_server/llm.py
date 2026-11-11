"""Talking to language-model providers, with nothing but the standard library.

Two wire formats cover nearly every provider:

  * "openai"    — the Chat Completions format (OpenRouter, OpenAI, and the many
                  gateways and local servers that copy it);
  * "anthropic" — the Messages format.

A provider is a wire format plus a base URL and a way to send the key. The
agent never sees either format: it works with the neutral messages below and
`complete()` translates in both directions.

Neutral conversation, a list of:

    {"role": "user", "content": "text"}
    {"role": "assistant", "content": "text", "tool_calls": [{"id", "name", "input"}],
     "native": {"wire": ..., "message": ...}}
    {"role": "tool", "results": [{"id", "name", "content", "is_error"}]}

`native` is the assistant message exactly as the provider returned it. It is
replayed verbatim to the same wire format, which is what providers require
for reasoning/thinking content they attach to a turn.
"""

import json
import time
import urllib.error
import urllib.request

TIMEOUT_S = 180
MAX_RESPONSE = 8 * 1024 * 1024

# `tested` is about what this project has exercised against the real service,
# not about whether the provider works.
PROVIDERS = {
    "openrouter": {
        "label": "OpenRouter",
        "wire": "openai",
        "base_url": "https://openrouter.ai/api/v1",
        "key_hint": "sk-or-…",
        "model_hint": "provider/model, e.g. as listed on openrouter.ai/models",
        "note": "One key for models from many vendors. The first supported provider.",
    },
    "openai": {
        "label": "OpenAI",
        "wire": "openai",
        "base_url": "https://api.openai.com/v1",
        "key_hint": "sk-…",
        "model_hint": "a chat model id",
        "note": "Uses the Chat Completions format.",
    },
    "anthropic": {
        "label": "Anthropic",
        "wire": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "key_hint": "sk-ant-…",
        "model_hint": "a model id, e.g. claude-opus-5-5",
        "note": "Uses the Messages format.",
    },
    "custom-openai": {
        "label": "Other (OpenAI-compatible)",
        "wire": "openai",
        "base_url": "",
        "key_hint": "API key, if the server needs one",
        "model_hint": "model id",
        "note": "Any server that speaks Chat Completions, including local ones.",
    },
    "custom-anthropic": {
        "label": "Other (Anthropic-compatible)",
        "wire": "anthropic",
        "base_url": "",
        "key_hint": "API key",
        "model_hint": "model id",
        "note": "Any server that speaks the Messages format.",
    },
}


class LLMError(Exception):
    """A provider call failed; the message is safe to show to the user."""


def resolve(cfg):
    """Fill in a stored LLM config from the provider catalog."""
    cfg = cfg or {}
    provider = PROVIDERS.get(cfg.get("provider") or "")
    if not provider:
        raise LLMError("no model provider is configured; set one up in Settings")
    base = (cfg.get("base_url") or provider["base_url"] or "").rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise LLMError("the model provider needs a base URL starting with http:// or https://")
    return {
        "provider": cfg["provider"],
        "label": provider["label"],
        "wire": provider["wire"],
        "base_url": base,
        "api_key": cfg.get("api_key") or "",
        "model": (cfg.get("model") or "").strip(),
    }


def _headers(cfg):
    headers = {"Content-Type": "application/json", "User-Agent": "mini-mlop"}
    if cfg["wire"] == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        if cfg["api_key"]:
            headers["x-api-key"] = cfg["api_key"]
    elif cfg["api_key"]:
        headers["Authorization"] = "Bearer " + cfg["api_key"]
    return headers


def _error_text(raw):
    """Pull the human-readable part out of a provider's error body."""
    try:
        body = json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", "replace").strip()[:300]
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)[:300]
    if isinstance(err, str):
        return err[:300]
    return json.dumps(body)[:300]


def _request(cfg, method, path, body=None, retries=1):
    url = cfg["base_url"] + path
    data = json.dumps(body).encode() if body is not None else None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=_headers(cfg), method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
                raw = r.read(MAX_RESPONSE + 1)
            if len(raw) > MAX_RESPONSE:
                raise LLMError(f"{cfg['label']} sent an unexpectedly large response")
            try:
                return json.loads(raw)
            except ValueError:
                raise LLMError(f"{cfg['label']} sent a response that is not JSON")
        except urllib.error.HTTPError as e:
            detail = _error_text(e.read(65536))
            if e.code in (429, 500, 502, 503, 529) and attempt < retries:
                time.sleep(2)
                continue
            if e.code in (401, 403):
                raise LLMError(f"{cfg['label']} rejected the API key ({e.code}): {detail}")
            raise LLMError(f"{cfg['label']} returned an error ({e.code}): {detail}")
        except (urllib.error.URLError, OSError) as e:
            if attempt < retries:
                time.sleep(1)
                continue
            reason = getattr(e, "reason", e)
            raise LLMError(f"could not reach {cfg['label']} at {cfg['base_url']}: {reason}")


def list_models(cfg):
    cfg = resolve(cfg)
    body = _request(cfg, "GET", "/models")
    items = body.get("data") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise LLMError(f"{cfg['label']} did not return a model list")
    return sorted({str(m["id"]) for m in items if isinstance(m, dict) and m.get("id")})


def complete(cfg, system, messages, tools, max_tokens=4096):
    """One model call. Returns a neutral assistant message plus
    {"usage": {"input", "output"}, "stop": "end" | "tools" | "length" | "refusal"}."""
    cfg = resolve(cfg)
    if not cfg["model"]:
        raise LLMError("no model is selected; choose one in Settings")
    if cfg["wire"] == "anthropic":
        return _anthropic(cfg, system, messages, tools, max_tokens)
    return _openai(cfg, system, messages, tools, max_tokens)


# -- Chat Completions ----------------------------------------------------------


def _openai_messages(system, messages):
    out = [{"role": "system", "content": system}]
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            native = m.get("native") or {}
            if native.get("wire") == "openai":
                out.append(native["message"])
                continue
            msg = {"role": "assistant", "content": m.get("content") or None}
            if m.get("tool_calls"):
                msg["tool_calls"] = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {"name": c["name"], "arguments": json.dumps(c["input"])},
                    }
                    for c in m["tool_calls"]
                ]
            out.append(msg)
        elif m["role"] == "tool":
            for r in m["results"]:
                out.append({"role": "tool", "tool_call_id": r["id"], "content": r["content"]})
    return out


def _openai(cfg, system, messages, tools, max_tokens):
    body = {"model": cfg["model"], "messages": _openai_messages(system, messages)}
    # OpenAI's own API renamed the limit; gateways still take the original name.
    body["max_completion_tokens" if cfg["provider"] == "openai" else "max_tokens"] = max_tokens
    if tools:
        body["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["schema"],
                },
            }
            for t in tools
        ]
    data = _request(cfg, "POST", "/chat/completions", body)
    try:
        choice = data["choices"][0]
        msg = choice["message"]
    except (KeyError, IndexError, TypeError):
        detail = _error_text(json.dumps(data).encode()) if isinstance(data, dict) else ""
        raise LLMError(f"{cfg['label']} returned no completion: {detail}")
    content = msg.get("content")
    if isinstance(content, list):  # some gateways return content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    calls = []
    for c in msg.get("tool_calls") or []:
        fn = c.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
            bad = not isinstance(args, dict)
        except ValueError:
            args, bad = {}, True
        calls.append({
            "id": c.get("id") or f"call_{len(calls)}",
            "name": fn.get("name") or "",
            "input": {} if bad else args,
            "invalid": bad,
        })
    usage = data.get("usage") or {}
    finish = choice.get("finish_reason")
    return {
        "role": "assistant",
        "content": content or "",
        "tool_calls": calls,
        "native": {"wire": "openai", "message": msg},
        "usage": {
            "input": int(usage.get("prompt_tokens") or 0),
            "output": int(usage.get("completion_tokens") or 0),
        },
        "stop": "tools" if calls else "length" if finish == "length"
        else "refusal" if finish == "content_filter" else "end",
    }


# -- Messages ------------------------------------------------------------------


def _anthropic_messages(messages):
    out = []
    for m in messages:
        if m["role"] == "user":
            out.append({"role": "user", "content": m["content"]})
        elif m["role"] == "assistant":
            native = m.get("native") or {}
            if native.get("wire") == "anthropic":
                out.append(native["message"])
                continue
            blocks = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for c in m.get("tool_calls") or []:
                blocks.append(
                    {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]}
                )
            out.append({"role": "assistant", "content": blocks or [{"type": "text", "text": "…"}]})
        elif m["role"] == "tool":
            # Every result of one step goes back in a single user message.
            out.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": r["id"],
                        "content": r["content"],
                        **({"is_error": True} if r.get("is_error") else {}),
                    }
                    for r in m["results"]
                ],
            })
    return out


def _anthropic(cfg, system, messages, tools, max_tokens):
    # No sampling or thinking parameters: current models reject several of
    # them, and the defaults are what we want.
    body = {
        "model": cfg["model"],
        "max_tokens": max_tokens,
        "system": system,
        "messages": _anthropic_messages(messages),
    }
    if tools:
        body["tools"] = [
            {"name": t["name"], "description": t["description"], "input_schema": t["schema"]}
            for t in tools
        ]
    data = _request(cfg, "POST", "/messages", body)
    blocks = data.get("content") if isinstance(data, dict) else None
    if not isinstance(blocks, list):
        raise LLMError(f"{cfg['label']} returned no message")
    text, calls = [], []
    for b in blocks:
        if b.get("type") == "text":
            text.append(b.get("text") or "")
        elif b.get("type") == "tool_use":
            args = b.get("input")
            calls.append({
                "id": b.get("id") or f"call_{len(calls)}",
                "name": b.get("name") or "",
                "input": args if isinstance(args, dict) else {},
                "invalid": not isinstance(args, dict),
            })
    usage = data.get("usage") or {}
    stop = data.get("stop_reason")
    content = "".join(text)
    if stop == "refusal" and not content:
        content = "The model declined to answer this request."
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": calls if stop != "refusal" else [],
        # Replayed unchanged, thinking blocks included.
        "native": {"wire": "anthropic", "message": {"role": "assistant", "content": blocks}},
        "usage": {
            "input": int(usage.get("input_tokens") or 0)
            + int(usage.get("cache_read_input_tokens") or 0)
            + int(usage.get("cache_creation_input_tokens") or 0),
            "output": int(usage.get("output_tokens") or 0),
        },
        "stop": "refusal" if stop == "refusal" else "tools" if calls
        else "length" if stop == "max_tokens" else "end",
    }
