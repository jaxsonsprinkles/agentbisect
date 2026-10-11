import json
import os
import stat
import threading
import time
import urllib.error
import urllib.request

import pytest

from agentbisect.llm_proxy import ISOLATION_NOTICE, LLMProxy

FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time
a = sys.argv[1:]
d = os.environ["FAKE_DIR"]
def arg(k):
    return a[a.index(k) + 1] if k in a else None
n = len(os.listdir(d)) if os.path.isdir(d) else 0
open(os.path.join(d, f"call{n}_{os.getpid()}"), "w").write(json.dumps(a))
mode = open(os.path.join(d, "mode")).read().strip() if os.path.exists(os.path.join(d, "mode")) else "ok"
prompt, model, schema = a[1], arg("--model"), arg("--json-schema")
cnt = len([f for f in os.listdir(d) if f.startswith("call")])
time.sleep(float(os.environ.get("FAKE_SLEEP", "0")))
def out(res=None, so=None, err=False, status=None):
    mu = {model: {"inputTokens": 100, "outputTokens": 10, "costUSD": 0.001}, "claude-side-1": {"inputTokens": 5, "outputTokens": 1, "costUSD": 0.0001}}
    print(json.dumps({"is_error": err, "result": res, "structured_output": so, "api_error_status": status, "modelUsage": mu, "total_cost_usd": 0.0011}))
if mode == "ratelimit":
    out("API Error: 429 rate limit exceeded", err=True, status=429); sys.exit(1)
if mode == "ratelimit_once" and cnt == 1:
    out("overloaded_error", err=True, status=529); sys.exit(1)
if not schema:
    out("echo:" + prompt[:40]); sys.exit(0)
if mode == "invalid_once" and cnt == 1:
    out(so={"text": None, "tool_calls": [{"name": "lookup_order", "input": {"order_id": 1042}}]}); sys.exit(0)
if mode == "invalid_always":
    out(so={"text": None, "tool_calls": [{"name": "nope", "input": {}}]}); sys.exit(0)
if "TOOL RESULT" in prompt:
    out(so={"text": "Your order shipped.", "tool_calls": []}); sys.exit(0)
out(so={"text": None, "tool_calls": [{"name": "lookup_order", "input": {"order_id": "1042"}}]})
'''

TOOLS = [{"name": "lookup_order", "description": "d",
          "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}}}]


@pytest.fixture
def px(tmp_path, monkeypatch):
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    d = tmp_path / "calls"
    d.mkdir()
    monkeypatch.setenv("FAKE_DIR", str(d))
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    p = LLMProxy(concurrency=2, backoff=0.01, retry_after=1, cache_dir=str(tmp_path / "cache"))
    p.serve(0)
    p.calls_dir = d
    yield p
    p.stop()


def post(px, body, path="/v1/messages"):
    req = urllib.request.Request(px.url + path, json.dumps(body).encode(), {"content-type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read()), r.headers
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read()), e.headers


def body(content="hi", **kw):
    return {"model": "claude-haiku-5-5", "max_tokens": 100, "messages": [{"role": "user", "content": content}], **kw}


def test_plain_text_and_model_pin_and_usage(px):
    s, r, _ = post(px, body("hello", model="claude-sonnet-5-5", system=[{"type": "text", "text": "SYS"}]))
    assert s == 200 and r["content"] == [{"type": "text", "text": "echo:hello"}]
    assert r["model"] == "claude-sonnet-5-5" and r["stop_reason"] == "end_turn"
    assert r["usage"] == {"input_tokens": 100, "output_tokens": 10}  # requested model's modelUsage, not the side call
    args = json.loads(next(px.calls_dir.glob("call*")).read_text())
    assert args[args.index("--model") + 1] == "claude-sonnet-5-5" and args[args.index("--system-prompt") + 1] == "SYS\n\n" + ISOLATION_NOTICE
    assert "--json-schema" not in args and args[args.index("--tools") + 1] == ""


def test_tool_call_emitted(px):
    s, r, _ = post(px, body("Where is order 1042?", tools=TOOLS, system="You are support."))
    assert s == 200 and r["stop_reason"] == "tool_use"
    tu = r["content"][0]
    assert tu["type"] == "tool_use" and tu["name"] == "lookup_order" and tu["input"] == {"order_id": "1042"}
    assert tu["id"].startswith("toolu_")


def test_multi_turn_tool_result(px):
    msgs = [{"role": "user", "content": "Where is order 1042?"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "lookup_order",
                                               "input": {"order_id": "1042"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "{\"status\": \"shipped\"}"}]}]
    s, r, _ = post(px, {"model": "claude-haiku-5-5", "max_tokens": 9, "messages": msgs, "tools": TOOLS})
    assert s == 200 and r["stop_reason"] == "end_turn" and r["content"] == [{"type": "text", "text": "Your order shipped."}]
    prompt = json.loads(next(px.calls_dir.glob("call*")).read_text())[1]
    assert "toolu_1" in prompt and "shipped" in prompt


def test_invalid_output_retries_once_then_ok(px):
    (px.calls_dir / "mode").write_text("invalid_once")
    s, r, _ = post(px, body("x", tools=TOOLS))
    assert s == 200 and r["content"][0]["input"] == {"order_id": "1042"}
    assert px.stats.snapshot()["invalid_output_retries"] == 1


def test_invalid_output_twice_is_error(px):
    (px.calls_dir / "mode").write_text("invalid_always")
    s, r, _ = post(px, body("x", tools=TOOLS))
    assert s == 502 and r["type"] == "error" and "invalid tool output" in r["error"]["message"]


def test_rate_limit_backoff_then_success(px):
    (px.calls_dir / "mode").write_text("ratelimit_once")
    s, r, _ = post(px, body("x"))
    assert s == 200
    st = px.stats.snapshot()
    assert st["rate_limit_events"] == 1 and st["retries"] == 1


def test_rate_limit_exhausted_is_429_with_retry_after(px):
    (px.calls_dir / "mode").write_text("ratelimit")
    px.max_retries = 2
    s, r, h = post(px, body("x"))
    assert s == 429 and r["error"]["type"] == "rate_limit_error" and h["retry-after"] == "1"
    assert px.stats.snapshot()["rate_limit_events"] == 3


def test_concurrency_cap(px, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "0.3")
    ts = [threading.Thread(target=post, args=(px, body(f"q{i}"))) for i in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    st = px.stats.snapshot()
    assert st["cli_calls"] == 6 and st["max_concurrent"] == 2


def test_cache_hit_and_stats(px):
    b = body("same", temperature=0)
    post(px, b)
    s, r, _ = post(px, b)
    assert s == 200 and r["content"][0]["text"] == "echo:same"
    with urllib.request.urlopen(px.url + "/stats") as f:
        st = json.loads(f.read())
    assert st["calls"] == 2 and st["cache_hits"] == 1 and st["cli_calls"] == 1
    assert st["input_tokens"] == 100 and st["output_tokens"] == 10 and st["cost_usd"] > 0
    assert st["temperatures"] == {"0": 2}
    assert len(list(px.calls_dir.glob("call*"))) == 1


def test_stream_rejected(px):
    s, r, _ = post(px, body("x", stream=True))
    assert s == 400 and "stream" in r["error"]["message"]


def test_runner_env_sets_proxy(tmp_path):
    from agentbisect.runner import Runner, RunnerConfig
    r = Runner(tmp_path, RunnerConfig(cases=["a"], llm_proxy_url="http://127.0.0.1:1"), None)
    env = r._env(None, None, None)
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:1" and env["ANTHROPIC_API_KEY"] == "agentbisect-proxy"


def test_isolation_notice_on_every_system_prompt(px):
    post(px, body("Where is order 1042?", tools=TOOLS, system="You are support."))
    post(px, body("judge me"))  # tool-free path, no system given
    for f in px.calls_dir.glob("call*"):
        a = json.loads(f.read_text())
        sp = a[a.index("--system-prompt") + 1]
        assert sp.endswith(ISOLATION_NOTICE) and ("You are support." in sp or "helpful assistant" in sp)


def test_debug_dump_writes_prompt_to_dir(tmp_path, monkeypatch):
    fake = tmp_path / "bin" / "claude"
    fake.parent.mkdir()
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "calls").mkdir()
    monkeypatch.setenv("FAKE_DIR", str(tmp_path / "calls"))
    monkeypatch.setenv("PATH", f"{fake.parent}:{os.environ['PATH']}")
    p = LLMProxy(debug_dump=str(tmp_path / "dump"))
    p.messages(body("Where is order 1042?", tools=TOOLS))
    (f,) = (tmp_path / "dump").glob("*.json")
    d = json.loads(f.read_text())
    assert "Where is order 1042?" in d["prompt"] and ISOLATION_NOTICE in d["system"] and d["structured_output"]


def test_salt_makes_repeats_distinct_samples_but_replays_cache(px):
    b = body("same")
    post(px, b, "/salt/abc.case.r0/v1/messages")
    post(px, b, "/salt/abc.case.r1/v1/messages")  # same prompt, other repeat: a fresh call
    post(px, b, "/salt/abc.case.r0/v1/messages")  # replay of repeat 0: cache hit
    st = px.stats.snapshot()
    assert st["cli_calls"] == 2 and st["cache_hits"] == 1
    assert not any("abc.case" in f.read_text() for f in px.calls_dir.glob("call*"))  # salt never reaches the model
    s, _, _ = post(px, b, "/salt/bad%20salt/v1/messages")
    assert s == 404


def test_backoff_is_exponential_with_equal_jitter_and_logged(px):
    import random
    waits = []
    px.max_retries, px.backoff, px._sleep, px.rng = 3, 2.0, waits.append, random.Random(1)
    (px.calls_dir / "mode").write_text("ratelimit")
    s, _, _ = post(px, body("x"))
    assert s == 429 and len(waits) == 3
    for attempt, w in enumerate(waits):  # d/2 <= wait <= d, d = 2 * 2^attempt
        d = 2.0 * 2 ** attempt
        assert d / 2 <= w <= d
    assert len(set(waits)) == 3
    kinds = [e["kind"] for e in px.events]
    assert kinds == ["backoff"] * 3 + ["gave_up"] and px.events[0]["wait_s"] == round(waits[0], 3)
    with urllib.request.urlopen(px.url + "/events") as f:
        d = json.loads(f.read())
    assert [e["kind"] for e in d["events"]] == kinds and d["concurrency"] == px.concurrency


def test_backoff_capped_and_recovery_event(px):
    px.max_backoff, px.backoff, px._sleep = 3.0, 100.0, lambda w: None
    (px.calls_dir / "mode").write_text("ratelimit_once")
    s, _, _ = post(px, body("x"))
    assert s == 200
    assert [e["kind"] for e in px.events] == ["backoff", "recovered"] and px.events[0]["wait_s"] <= 3.0


def test_concurrency_default_and_env(monkeypatch):
    from agentbisect import llm_proxy as lp
    monkeypatch.delenv("AGENTBISECT_MODEL_CONCURRENCY", raising=False)
    assert lp.LLMProxy().concurrency == 4
    monkeypatch.setenv("AGENTBISECT_MODEL_CONCURRENCY", "7")
    assert lp.LLMProxy().concurrency == 7 and lp.LLMProxy(concurrency=2).concurrency == 2
    for bad in ("abc", "-3", "0"):
        monkeypatch.setenv("AGENTBISECT_MODEL_CONCURRENCY", bad)
        assert lp.LLMProxy().concurrency >= 1
    monkeypatch.setenv("AGENTBISECT_MAX_RETRIES", "-5")
    assert lp.LLMProxy().max_retries == 0


def test_event_totals_survive_the_cap(monkeypatch):
    from agentbisect import llm_proxy as lp
    p = lp.LLMProxy()
    for i in range(lp.MAX_EVENTS + 20):
        p._event("backoff", "m", 0, 1.0)
    assert len(p.events) == lp.MAX_EVENTS and p.totals["backoff"] == lp.MAX_EVENTS + 20 and p._seq == lp.MAX_EVENTS + 20


def test_throttle_deltas_do_not_double_count_across_investigations(px, tmp_path, monkeypatch):
    from agentbisect import cli
    from agentbisect.report import build_report
    from agentbisect.state import StateStore
    px.backoff, px._sleep = 1.0, lambda w: None
    monkeypatch.setenv("AGENTBISECT_LLM_PROXY_URL", px.url)
    store = StateStore(tmp_path / "ab")
    first, second = store.create(str(tmp_path)), store.create(str(tmp_path))
    mark = cli._proxy_events()
    (px.calls_dir / "mode").write_text("ratelimit_once")
    post(px, body("x"))
    cli.record_throttle(store, first, mark)
    mark2 = cli._proxy_events()  # sibling starts after: sees nothing new
    post(px, body("y"))
    cli.record_throttle(store, second, mark2)
    assert store.load(second.id).metrics.get("throttle") is None
    t = store.load(first.id).metrics["throttle"]
    assert t["backoffs"] == 1 and t["recovered"] == 1 and t["gave_up"] == 0 and t["events"][0]["kind"] == "backoff"
    assert "1 backoff(s)" in build_report(store.load(first.id))["throttle"]
    cli.record_throttle(store, first, mark)  # resume-style second call with the same mark adds the delta again only
    assert build_report(store.load(second.id))["throttle"] is None
    monkeypatch.delenv("AGENTBISECT_LLM_PROXY_URL")
    cli.record_throttle(store, second, mark)  # no proxy: no-op


def test_slot_not_held_during_backoff_and_cap_enforced(px, monkeypatch):
    monkeypatch.setenv("FAKE_SLEEP", "0.2")
    ts = [threading.Thread(target=post, args=(px, body(f"c{i}"))) for i in range(5)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert px.stats.snapshot()["max_concurrent"] <= px.concurrency


def test_judge_smoke_records_pinned_model(px, monkeypatch, capsys):
    from agentbisect import cli
    from agentbisect.llm_proxy import judge_smoke
    r = judge_smoke(px.url, "claude-sonnet-5-5")
    assert r["match"] and r["recorded"] == "claude-sonnet-5-5" and r["input_tokens"] == 100
    monkeypatch.setenv("JUDGE_MODEL", "claude-opus-5")
    assert judge_smoke(px.url)["pinned"] == "claude-opus-5"
    monkeypatch.delenv("JUDGE_MODEL")
    assert cli.main(["judge-smoke", "--proxy-url", px.url]) == 0
    assert '"match": true' in capsys.readouterr().out
    assert cli.main(["judge-smoke", "--proxy-url", "http://127.0.0.1:1"]) == 1
