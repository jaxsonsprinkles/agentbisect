import threading

from agentbisect import bisect as B
from agentbisect.models import RegressionSpec
from agentbisect.runner import Runner, RunnerConfig, RunResult, config_from_env
from agentbisect.state import StateStore
from test_bisect import PINS, hist  # noqa: F401


class Exec:
    """script(idx, case, repeat_index_in_call, call_no) -> 'pass'|'fail'; records env + requested repeats."""

    def __init__(self, script):
        self.script, self.reqs, self.lock, self.n = script, [], threading.Lock(), 0

    def run_case(self, req):
        idx = int((req.workdir / "marker.txt").read_text())
        out = []
        with self.lock:
            self.reqs.append((idx, req.case, req.repeats, req.env.get("ANTHROPIC_BASE_URL")))
        for i in range(req.repeats):
            with self.lock:
                self.n += 1
                k = self.n
            st, _, why = self.script(idx, req.case, i, k).partition(":")
            out.append(RunResult(req.case, st, trace_id=f"t{idx}-{k}", reason=why,
                                 **({} if st == "error" else {"agent_model": PINS["agent_model"],
                                                              "judge_model": PINS["judge_model"]})))
        return out


def mk(tmp_path, hist, script, **cfg):
    repo, shas = hist
    store = StateStore(tmp_path / "ab")
    ex = Exec(script)
    r = Runner(repo, RunnerConfig(cases=cfg.pop("cases", ["a", "b"]), install_cmd=None, models=dict(PINS), backoff=0.0,
                                  workers=1, **cfg), store.cache(str(repo)), ex, sleep=lambda s: None)
    return r, ex, shas, store


def test_default_is_one_repeat_per_case(tmp_path, hist):
    r, ex, shas, _ = mk(tmp_path, hist, lambda *a: "pass")
    res = r.evaluate_commit(shas[0], "x")
    assert res.verdict == "good" and res.executed == 2 and r.eval_rounds == 2 and res.repeats == 1


def test_env_override_keeps_working(monkeypatch):
    monkeypatch.setenv("AGENTBISECT_REPEATS", "4")
    monkeypatch.setenv("AGENTBISECT_JUDGE_REPEATS", "5")
    c = config_from_env(["a"])
    assert c.repeats == 4 and c.judge_repeats == 5


def test_clear_result_does_not_escalate_borderline_does(tmp_path, hist):
    # 1 of 2 cases fails => rate .5 is at bad_max (bad); 2 of 3 fail with good_min .85/bad_max .5 => borderline
    r, ex, shas, _ = mk(tmp_path, hist, lambda idx, c, i, k: "fail" if c == "a" else "pass",
                        cases=["a", "b", "c"], good_min=0.9, bad_max=0.3)
    res = r.evaluate_commit(shas[5], "x")  # 2/3 pass = .67: borderline -> re-run the failing case only
    assert res.repeats >= 2
    assert {c for _, c, _, _ in ex.reqs[3:]} == {"a"}  # only the failing case got extra repeats


def test_judge_cases_get_extra_repeats_up_front(tmp_path, hist):
    r, ex, shas, _ = mk(tmp_path, hist, lambda *a: "pass", judge_cases=("b",), judge_repeats=3)
    r.evaluate_commit(shas[0], "x")
    per_case = {}
    for _, c, n, _ in ex.reqs:
        per_case[c] = per_case.get(c, 0) + n
    assert per_case == {"a": 1, "b": 3}


def test_proxy_requests_are_salted_per_commit_case_repeat(tmp_path, hist):
    r, ex, shas, _ = mk(tmp_path, hist, lambda *a: "pass", repeats=3, llm_proxy_url="http://127.0.0.1:9", cases=["a"])
    r.evaluate_commit(shas[0], "x")
    urls = [u for _, _, n, u in ex.reqs]
    assert len(urls) == 3 and all(n == 1 for _, _, n, _ in ex.reqs) and len(set(urls)) == 3
    import hashlib
    tag = hashlib.sha1(b"a").hexdigest()[:10]
    assert urls[0] == f"http://127.0.0.1:9/salt/{shas[0][:12]}.{tag}.r0" and urls[2].endswith(f".{tag}.r2")


def test_resume_runs_only_missing_repeats(tmp_path, hist):
    r, ex, shas, store = mk(tmp_path, hist, lambda *a: "pass", repeats=2, cases=["a"], llm_proxy_url="http://p:1")
    r.evaluate_commit(shas[0], "x")
    assert r.cache.get(shas[0], "a", r.cfg.models)["runs"] == 2
    r.cfg.repeats = 3  # e.g. escalation after a crash: repeat index 2 is the only new unit
    r.evaluate_commit(shas[0], "x")
    assert [u.rsplit(".", 1)[-1] for *_, u in ex.reqs] == ["r0", "r1", "r2"]


def test_cache_key_is_per_repeat_and_legacy_entries_are_ignored(tmp_path, hist):
    r, ex, shas, store = mk(tmp_path, hist, lambda *a: "pass", repeats=2, cases=["a"])
    r.evaluate_commit(shas[0], "x")
    keys = [k for k in r.cache._data if shas[0] in k]
    assert sorted(k.rsplit("|", 1)[-1] for k in keys) == ["r0", "r1"] and all(k.startswith("v3|") for k in keys)
    r.cache._data = {f"{shas[1]}|a|{PINS['agent_model']}|{PINS['judge_model']}": {"passes": 9, "fails": 0}}
    assert r.cache.get(shas[1], "a", r.cfg.models) is None


def test_long_case_ids_stay_routable(tmp_path, hist):
    from agentbisect.llm_proxy import SALT_PATH
    case = "tests/test_" + "x" * 300 + "::case[param]"
    r, ex, shas, _ = mk(tmp_path, hist, lambda *a: "pass", cases=[case], llm_proxy_url="http://127.0.0.1:9")
    r.evaluate_commit(shas[0], "x")
    path = ex.reqs[0][3].split("127.0.0.1:9")[1] + "/v1/messages"
    assert SALT_PATH.match(path)


def test_rate_limit_retry_reuses_salt_and_backs_off(tmp_path, hist):
    calls = []

    def script(idx, c, i, k):
        return "error:429 rate limit" if k == 1 else "pass"

    r, ex, shas, _ = mk(tmp_path, hist, script, cases=["a"], repeats=2, llm_proxy_url="http://p:1")
    sleeps = []
    r.sleep = sleeps.append
    r.cfg.backoff = 2.0
    res = r.evaluate_commit(shas[0], "x")
    urls = [u.rsplit(".", 1)[-1] for *_, u in ex.reqs]
    assert res.executed == 2 and urls[:2] == ["r0", "r0"] and sleeps == [2.0]
