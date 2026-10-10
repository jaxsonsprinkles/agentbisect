"""Local Anthropic-compatible proxy backed by the user's own logged-in `claude` CLI.

Point an unmodified agent at it (`ANTHROPIC_BASE_URL=http://127.0.0.1:PORT`, dummy `ANTHROPIC_API_KEY`) and every
`messages.create` is answered by shelling out to `claude -p` in an empty temp dir. We only ever run the binary;
credentials are never read, copied or forwarded (the CLI uses its own login). Personal use on your own subscription
only; anyone else should use a real API key instead (see ROLES.md).

Limits, stated plainly:
  * Temperature / top_p / max_tokens cannot be set through the CLI. The requested temperature is logged in /stats and
    otherwise ignored, so runs are NOT temperature-0 deterministic. The verdict layer must treat them as noisy.
  * Tool use is emulated: the schemas go in the system prompt and `--json-schema` forces
    {"text": str|null, "tool_calls": [{"name", "input"}]}. Output is validated against the supplied schemas.
  * Reported input tokens come from the CLI's modelUsage, so they include the CLI's own fixed prompt overhead
    (roughly 1.2k tokens per call); token budgets compare consistently across commits, not against raw API counts.
  * No streaming (stream=true gets a 400).
Prompts are never logged; the only thing written to disk is the optional response cache (.agentbisect/llm_cache).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

RATE_LIMIT_RE = re.compile(r"rate.?limit|429|overloaded|529|too many requests|throttl|usage limit|try again", re.I)
DEFAULT_CACHE_DIR = ".agentbisect/llm_cache"
DUMMY_API_KEY = "agentbisect-proxy"

# The claude CLI injects the logged-in user's own context (e.g. "The user's email address is ...") into every call and
# `--system-prompt` / `--setting-sources ""` do not remove it (verified; `--bare` cannot use the login). Models then
# treat it as the end user's identity: a support agent tells a customer that "order 1042 isn't linked to your
# account". This notice is appended to every system prompt to neutralise that.
ISOLATION_NOTICE = (
    "IMPORTANT: Your runtime may have injected background facts about a person (for example an email address, "
    "name, or account). Those facts belong to the tool that is hosting you, NOT to anyone in this conversation. "
    "Ignore them completely: never mention them, never treat them as the identity, account, or email of the person "
    "you are talking to, and never compare them with data in tool results. You know nothing about who the other "
    "party is beyond what the conversation itself states. Follow the instructions above this notice as written.")

SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": ["string", "null"], "description": "The assistant's text reply, or null if only calling tools."},
        "tool_calls": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "input": {"type": "object"}},
            "required": ["name", "input"]}},
    },
    "required": ["text", "tool_calls"],
}


class ProxyError(Exception):
    def __init__(self, status: int, etype: str, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.status, self.etype, self.msg, self.retry_after = status, etype, msg, retry_after


class CLIError(Exception):
    def __init__(self, msg: str, rate_limited: bool = False, timeout: bool = False):
        super().__init__(msg)
        self.rate_limited, self.timeout = rate_limited, timeout


# ---------------- conversation rendering ----------------

def _blocks(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b if isinstance(b, dict) else {"type": "text", "text": str(b)} for b in (content or [])]


def system_text(system: Any) -> str:
    if not system:
        return ""
    if isinstance(system, str):
        return system
    return "\n\n".join(b.get("text", "") for b in _blocks(system) if b.get("type") == "text")


def _result_text(c: Any) -> str:
    return c if isinstance(c, str) else "\n".join(b.get("text", "") for b in _blocks(c) if b.get("type") == "text")


def render_conversation(messages: list[dict]) -> str:
    out = []
    for m in messages:
        who = "USER" if m.get("role") == "user" else "ASSISTANT"
        for b in _blocks(m.get("content")):
            t = b.get("type")
            if t == "text":
                out.append(f"[{who}]\n{b.get('text', '')}")
            elif t == "tool_use":
                out.append(f"[ASSISTANT called tool {b.get('name')} (id {b.get('id')})]\n"
                           f"{json.dumps(b.get('input', {}), sort_keys=True)}")
            elif t == "tool_result":
                err = " ERROR" if b.get("is_error") else ""
                out.append(f"[TOOL RESULT{err} for call {b.get('tool_use_id')}]\n{_result_text(b.get('content'))}")
    return "\n\n".join(out)


def render_tools(tools: list[dict], tool_choice: dict | None) -> str:
    lines = ["TOOLS ARE AVAILABLE TO YOU. They are executed by the caller, not by you: you invoke a tool by listing it in "
             "the `tool_calls` field of your structured output as {\"name\", \"input\"}; the result comes back in the "
             "next turn as a TOOL RESULT. Never say a tool is unavailable and never answer from memory what a tool "
             "can look up: if the user's request needs a tool, emit the tool call (text may be null). `input` must "
             "match the tool's input_schema exactly (only listed properties, required ones present, correct JSON "
             "types). You may call several tools at once. After TOOL RESULTs arrive, answer the user in `text` with "
             "`tool_calls` = [] unless more information is needed.", ""]
    for t in tools:
        lines.append(f"TOOL {t['name']}: {t.get('description', '')}\ninput_schema: "
                     f"{json.dumps(t.get('input_schema', {}), sort_keys=True)}\n")
    tc = (tool_choice or {}).get("type")
    if tc == "any":
        lines.append("You MUST call at least one tool in this turn.")
    elif tc == "tool":
        lines.append(f"You MUST call the tool {tool_choice.get('name')!r} in this turn.")
    return "\n".join(lines)


# ---------------- schema validation (small JSON-schema subset) ----------------

_TYPES = {"string": str, "number": (int, float), "integer": int, "boolean": bool, "array": list,
          "object": dict, "null": type(None)}


def validate(value: Any, schema: dict, path: str = "input") -> str | None:
    """Return an error string, or None. Supports type/enum/required/properties/items/additionalProperties."""
    t = schema.get("type")
    if t is not None:
        ts = t if isinstance(t, list) else [t]
        ok = False
        for x in ts:
            py = _TYPES.get(x)
            if py is None:
                ok = True
            elif x in ("number", "integer") and isinstance(value, bool):
                continue
            elif isinstance(value, py):
                ok = True
        if not ok:
            return f"{path}: expected {t}, got {type(value).__name__}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path}: {value!r} not in {schema['enum']}"
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for r in schema.get("required", []):
            if r not in value:
                return f"{path}: missing required '{r}'"
        for k, v in value.items():
            if k in props:
                e = validate(v, props[k], f"{path}.{k}")
                if e:
                    return e
            elif schema.get("additionalProperties") is False:
                return f"{path}: unexpected property '{k}'"
    if isinstance(value, list) and "items" in schema:
        for i, v in enumerate(value):
            e = validate(v, schema["items"], f"{path}[{i}]")
            if e:
                return e
    return None


def validate_output(out: Any, tools: list[dict], tool_choice: dict | None) -> str | None:
    if not isinstance(out, dict) or not isinstance(out.get("tool_calls", []), list):
        return "output must be an object with text and tool_calls"
    text, calls = out.get("text"), out.get("tool_calls") or []
    if text is not None and not isinstance(text, str):
        return "text must be a string or null"
    by_name = {t["name"]: t for t in tools}
    for i, c in enumerate(calls):
        if not isinstance(c, dict) or c.get("name") not in by_name:
            return f"tool_calls[{i}]: unknown tool {c.get('name') if isinstance(c, dict) else c!r}; " \
                   f"available: {sorted(by_name)}"
        if not isinstance(c.get("input"), dict):
            return f"tool_calls[{i}]: input must be an object"
        e = validate(c["input"], by_name[c["name"]].get("input_schema", {}), f"{c['name']}.input")
        if e:
            return f"tool_calls[{i}]: {e}"
    tc = (tool_choice or {}).get("type")
    if tc == "any" and not calls:
        return "a tool call is required"
    if tc == "tool" and not any(c["name"] == tool_choice.get("name") for c in calls):
        return f"tool {tool_choice.get('name')!r} must be called"
    if not calls and not (text or "").strip():
        return "empty reply: provide text or a tool call"
    return None


# ---------------- the proxy ----------------

class Stats:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.d = {"calls": 0, "cli_calls": 0, "cache_hits": 0, "input_tokens": 0, "output_tokens": 0,
                  "cost_usd": 0.0, "rate_limit_events": 0, "retries": 0, "errors": 0, "invalid_output_retries": 0,
                  "latency_s_total": 0.0, "max_concurrent": 0, "in_flight": 0, "temperatures": {}}

    def add(self, **kw: float) -> None:
        with self.lock:
            for k, v in kw.items():
                self.d[k] += v

    def snapshot(self) -> dict:
        with self.lock:
            d = dict(self.d)
        d["avg_latency_s"] = round(d["latency_s_total"] / d["cli_calls"], 3) if d["cli_calls"] else 0.0
        d["cost_usd"] = round(d["cost_usd"], 6)
        return d


class LLMProxy:
    def __init__(self, claude_bin: str = "claude", concurrency: int = 3, cache_dir: str | None = None,
                 timeout: float = 180.0, max_retries: int = 3, backoff: float = 2.0, retry_after: float = 5.0,
                 debug_dump: str | None = None):
        self.claude_bin, self.timeout = claude_bin, timeout
        self.debug_dump = Path(debug_dump) if debug_dump else None  # DEBUG ONLY: writes full prompts to disk
        if self.debug_dump:
            self.debug_dump.mkdir(parents=True, exist_ok=True)
        self.max_retries, self.backoff, self.retry_after = max_retries, backoff, retry_after
        self.sem = threading.BoundedSemaphore(concurrency)
        self.cache_dir = Path(cache_dir) if cache_dir else None
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.stats = Stats()
        self.httpd: ThreadingHTTPServer | None = None

    # -- CLI call --
    def _cli_once(self, prompt: str, system: str, model: str, schema: dict | None) -> dict:
        system = (system or "You are a helpful assistant.") + "\n\n" + ISOLATION_NOTICE
        cmd = [self.claude_bin, "-p", prompt, "--model", model, "--output-format", "json", "--tools", "",
               "--system-prompt", system, "--setting-sources", "",
               "--strict-mcp-config", "--no-session-persistence"]
        if schema:
            cmd += ["--json-schema", json.dumps(schema)]
        with tempfile.TemporaryDirectory(prefix="agentbisect-llm-") as cwd:  # empty cwd: nothing to read or index
            try:
                p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=self.timeout)
            except subprocess.TimeoutExpired:
                raise CLIError(f"claude timed out after {self.timeout}s", timeout=True)
            except OSError as e:
                raise CLIError(f"cannot run {self.claude_bin!r}: {e}")
        raw = p.stdout.strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            msg = (p.stderr or raw)[-400:]
            raise CLIError(f"claude exit {p.returncode}, no JSON: {msg}", rate_limited=bool(RATE_LIMIT_RE.search(msg)))
        if data.get("is_error") or p.returncode != 0:
            msg = f"{data.get('result') or ''} {p.stderr or ''}".strip()[-400:]
            rl = data.get("api_error_status") in (429, 529) or bool(RATE_LIMIT_RE.search(msg))
            raise CLIError(f"claude error: {msg}", rate_limited=rl)
        if self.debug_dump:
            f = self.debug_dump / f"{time.time_ns()}-{uuid.uuid4().hex[:6]}.json"
            f.write_text(json.dumps({"model": model, "system": system, "prompt": prompt, "schema": schema,
                                     "result": data.get("result"), "structured_output": data.get("structured_output")},
                                    indent=2))
        return data

    def _cli(self, prompt: str, system: str, model: str, schema: dict | None) -> tuple[dict, float]:
        """One CLI call under the concurrency cap, retrying rate-limit-style failures with exponential backoff."""
        for attempt in range(self.max_retries + 1):
            with self.sem:
                with self.stats.lock:
                    self.stats.d["in_flight"] += 1
                    self.stats.d["max_concurrent"] = max(self.stats.d["max_concurrent"], self.stats.d["in_flight"])
                t0 = time.perf_counter()
                try:
                    data = self._cli_once(prompt, system, model, schema)
                    dt = time.perf_counter() - t0
                    self.stats.add(cli_calls=1, latency_s_total=dt)
                    return data, dt
                except CLIError as e:
                    if not e.rate_limited:
                        self.stats.add(errors=1)
                        raise ProxyError(504 if e.timeout else 500, "timeout_error" if e.timeout else "api_error", str(e))
                    self.stats.add(rate_limit_events=1)
                    last = e
                finally:
                    with self.stats.lock:
                        self.stats.d["in_flight"] -= 1
            if attempt < self.max_retries:
                self.stats.add(retries=1)
                time.sleep(self.backoff * (2 ** attempt))
        self.stats.add(errors=1)
        raise ProxyError(429, "rate_limit_error", f"claude CLI rate limited after retries: {last}",
                         retry_after=self.retry_after)

    @staticmethod
    def _usage(data: dict, model: str) -> tuple[str, int, int, float]:
        mu = data.get("modelUsage") or {}
        key = next((k for k in mu if k == model), None) \
            or next((k for k in mu if k.startswith(model) or model.startswith(k)), None)
        if key is None:  # fall back to the model with the most output
            key = max(mu, key=lambda k: mu[k].get("outputTokens", 0), default=None) if mu else None
        u = mu.get(key, {}) if key else {}
        inp = u.get("inputTokens", 0) + u.get("cacheReadInputTokens", 0) + u.get("cacheCreationInputTokens", 0)
        if not mu:
            du = data.get("usage") or {}
            inp, u = du.get("input_tokens", 0), {"outputTokens": du.get("output_tokens", 0)}
        cost = sum(v.get("costUSD", 0.0) for v in mu.values()) or data.get("total_cost_usd", 0.0)
        return (key or model), int(inp), int(u.get("outputTokens", 0)), float(cost)

    # -- request handling --
    def cache_key(self, body: dict) -> str:
        k = {x: body.get(x) for x in ("model", "system", "messages", "tools", "tool_choice")}
        return hashlib.sha256(json.dumps(k, sort_keys=True).encode()).hexdigest()

    def messages(self, body: dict) -> dict:
        self.stats.add(calls=1)
        if body.get("stream"):
            raise ProxyError(400, "invalid_request_error", "stream=true is not supported by the agentbisect LLM proxy")
        model, msgs = body.get("model"), body.get("messages")
        if not model or not isinstance(msgs, list) or not msgs:
            raise ProxyError(400, "invalid_request_error", "model and a non-empty messages list are required")
        temp = body.get("temperature")
        with self.stats.lock:
            self.stats.d["temperatures"][str(temp)] = self.stats.d["temperatures"].get(str(temp), 0) + 1
        tools = [t for t in (body.get("tools") or []) if isinstance(t, dict) and "name" in t]
        tool_choice = body.get("tool_choice")
        if (tool_choice or {}).get("type") == "none":
            tools = []
        key = self.cache_key(body)
        if self.cache_dir:
            f = self.cache_dir / f"{key}.json"
            if f.exists():
                self.stats.add(cache_hits=1)
                return json.loads(f.read_text())
        resp = self._complete(body, model, msgs, tools, tool_choice)
        if self.cache_dir:
            tmp = self.cache_dir / f"{key}.{uuid.uuid4().hex}.tmp"
            tmp.write_text(json.dumps(resp))
            os.replace(tmp, self.cache_dir / f"{key}.json")
        return resp

    def _complete(self, body: dict, model: str, msgs: list, tools: list, tool_choice: dict | None) -> dict:
        sys_text = system_text(body.get("system"))
        if not tools:
            only = msgs[0] if len(msgs) == 1 and msgs[0].get("role") == "user" else None
            prompt = _result_text(only["content"]) if only else \
                render_conversation(msgs) + "\n\nWrite the next ASSISTANT message. Output only that message."
            data, _ = self._cli(prompt, sys_text, model, None)
            used, i, o, cost = self._usage(data, model)
            self._account(i, o, cost)
            return self._response(used, [{"type": "text", "text": data.get("result") or ""}], "end_turn", i, o)
        system = (sys_text + "\n\n" if sys_text else "") + render_tools(tools, tool_choice) + \
            "\n\nYou are producing the next ASSISTANT turn of the conversation below, as the structured output."
        prompt = render_conversation(msgs) + "\n\nProduce the next ASSISTANT turn now."
        err = ""
        for attempt in range(2):
            p = prompt if not err else prompt + f"\n\nYour previous output was invalid ({err}). Fix it."
            data, _ = self._cli(p, system, model, SCHEMA)
            used, i, o, cost = self._usage(data, model)
            self._account(i, o, cost)
            out = data.get("structured_output")
            if out is None:
                try:
                    out = json.loads(data.get("result") or "")
                except (json.JSONDecodeError, TypeError):
                    out = None
            err = validate_output(out, tools, tool_choice) if out is not None else "output was not valid JSON"
            if not err:
                break
            self.stats.add(invalid_output_retries=1)
        else:
            self.stats.add(errors=1)
            raise ProxyError(502, "api_error", f"model produced invalid tool output twice: {err}")
        blocks: list[dict] = []
        if (out.get("text") or "").strip():
            blocks.append({"type": "text", "text": out["text"]})
        for c in out.get("tool_calls") or []:
            blocks.append({"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:24], "name": c["name"],
                           "input": c["input"]})
        has_tool = any(b["type"] == "tool_use" for b in blocks)
        return self._response(used, blocks, "tool_use" if has_tool else "end_turn", i, o)

    def _account(self, i: int, o: int, cost: float) -> None:
        self.stats.add(input_tokens=i, output_tokens=o, cost_usd=cost)

    @staticmethod
    def _response(model: str, content: list, stop: str, i: int, o: int) -> dict:
        return {"id": "msg_" + uuid.uuid4().hex[:24], "type": "message", "role": "assistant", "model": model,
                "content": content, "stop_reason": stop, "stop_sequence": None,
                "usage": {"input_tokens": i, "output_tokens": o}}

    # -- server --
    def serve(self, port: int = 0, host: str = "127.0.0.1") -> ThreadingHTTPServer:
        proxy = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # never log request contents
                pass

            def _send(self, status: int, obj: dict, headers: dict | None = None) -> None:
                data = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/stats":
                    self._send(200, proxy.stats.snapshot())
                elif self.path in ("/health", "/"):
                    self._send(200, {"ok": True})
                else:
                    self._send(404, {"type": "error", "error": {"type": "not_found_error", "message": self.path}})

            def do_POST(self):
                if self.path.split("?")[0] != "/v1/messages":
                    return self._send(404, {"type": "error", "error": {"type": "not_found_error", "message": self.path}})
                try:
                    body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
                    self._send(200, proxy.messages(body))
                except json.JSONDecodeError:
                    self._send(400, {"type": "error", "error": {"type": "invalid_request_error",
                                                                "message": "body is not valid JSON"}})
                except ProxyError as e:
                    h = {"retry-after": str(int(e.retry_after))} if e.retry_after else {}
                    if e.status == 429:
                        h["x-should-retry"] = "true"
                    self._send(e.status, {"type": "error", "error": {"type": e.etype, "message": e.msg}}, h)
                except Exception as e:  # never leak a traceback with prompt content
                    proxy.stats.add(errors=1)
                    self._send(500, {"type": "error", "error": {"type": "api_error", "message": type(e).__name__}})

        self.httpd = ThreadingHTTPServer((host, port), H)
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self.httpd

    @property
    def url(self) -> str:
        h, p = self.httpd.server_address[:2]
        return f"http://{h}:{p}"

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()


def run_forever(port: int, concurrency: int, cache: bool, cache_dir: str | None, claude_bin: str = "claude",
                debug_dump: str | None = None) -> int:
    px = LLMProxy(claude_bin=claude_bin, concurrency=concurrency, debug_dump=debug_dump,
                  cache_dir=(cache_dir or DEFAULT_CACHE_DIR) if cache else None)
    px.serve(port)
    print(f"agentbisect llm-proxy on {px.url} (concurrency {concurrency}, cache {'on' if cache else 'off'}); "
          f"use ANTHROPIC_BASE_URL={px.url} ANTHROPIC_API_KEY={DUMMY_API_KEY}; GET /stats for counters", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        px.stop()
        print(json.dumps(px.stats.snapshot()))
    return 0
