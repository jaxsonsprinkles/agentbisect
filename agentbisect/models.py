"""Core data shapes (see the plan's "Key data shapes"). Every class round-trips through plain JSON dicts."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

STATUSES = ("created", "running", "paused", "failed", "culprit_found", "fixing", "verifying", "reported")
VERDICTS = ("good", "bad", "flaky", "skip")


@dataclass
class RegressionSpec:
    source: str = "trace"  # trace | detection | issue
    bad_trace_id: str | None = None
    good_trace_ids: list[str] = field(default_factory=list)
    cases: list[str] = field(default_factory=list)
    held_out: list[str] = field(default_factory=list)
    predicate: dict[str, Any] = field(default_factory=dict)  # {"checks": [...], "rubric": str|None}
    metric: str = "pass_rate"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RegressionSpec":
        return cls(**d)


@dataclass
class Candidate:
    sha: str
    score: float = 0.0
    reason: str = ""


@dataclass
class Plan:
    divergent_span: str | None = None
    impacted_symbols: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)
    mode: str = "smart"  # smart | plain

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Plan":
        d = dict(d)
        d["candidates"] = [Candidate(**c) for c in d.get("candidates", [])]
        return cls(**d)


@dataclass
class CommitResult:
    """Verdict for one commit: results{sha: verdict, pass_rate, trace_ids} plus flaky/skip detail."""
    verdict: str  # good | bad | flaky | skip
    pass_rate: float | None = None
    trace_ids: list[str] = field(default_factory=list)
    reason: str = ""  # why skipped / flaky
    executed: int = 0
    repeats: int = 0  # repeats per case actually used (after escalation)
    flaky_cases: list[str] = field(default_factory=list)
    failing_cases: list[str] = field(default_factory=list)
    case_stats: dict[str, dict[str, int]] = field(default_factory=dict)  # case -> passes/fails/errors
    from_cache: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CommitResult":
        return cls(**d)


@dataclass
class BisectState:
    id: str
    repo: str = ""
    good_sha: str | None = None
    bad_sha: str | None = None
    spec: RegressionSpec | None = None
    plan: Plan | None = None
    results: dict[str, CommitResult] = field(default_factory=dict)
    step: int = 0
    status: str = "created"
    in_flight: dict[str, Any] | None = None  # step started but not completed (set => crashed mid-step)
    context_tokens_per_step: list[dict[str, Any]] = field(default_factory=list)  # {step, label, tokens}
    pinned_models: dict[str, str] = field(default_factory=dict)
    culprit: "CulpritReport | None" = None
    fix_attempts: list["FixAttempt"] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            **{k: v for k, v in asdict(self).items()
               if k not in ("spec", "plan", "results", "culprit", "fix_attempts")},
            "spec": self.spec.to_dict() if self.spec else None,
            "plan": self.plan.to_dict() if self.plan else None,
            "results": {s: r.to_dict() for s, r in self.results.items()},
            "culprit": self.culprit.to_dict() if self.culprit else None,
            "fix_attempts": [f.to_dict() for f in self.fix_attempts],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "BisectState":
        d = dict(d)
        d["spec"] = RegressionSpec.from_dict(d["spec"]) if d.get("spec") else None
        d["plan"] = Plan.from_dict(d["plan"]) if d.get("plan") else None
        d["results"] = {s: CommitResult.from_dict(r) for s, r in d.get("results", {}).items()}
        d["culprit"] = CulpritReport.from_dict(d["culprit"]) if d.get("culprit") else None
        d["fix_attempts"] = [FixAttempt.from_dict(f) for f in d.get("fix_attempts", [])]
        return cls(**d)


@dataclass
class CulpritReport:
    sha: str
    checkpoint_intent: str = ""
    semantic_diff: str = ""
    impacted_symbols: list[str] = field(default_factory=list)
    revert_test_passed: bool | None = None
    confidence: str = ""  # e.g. high | medium | low

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CulpritReport":
        return cls(**d)


@dataclass
class FixAttempt:
    branch: str
    diff: str = ""
    eval_score: float | None = None
    held_out_score: float | None = None
    intent_test_passed: bool | None = None
    trace_ids: list[str] = field(default_factory=list)
    attempt: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FixAttempt":
        return cls(**d)
