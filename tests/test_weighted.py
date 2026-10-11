"""Property tests for the weighted (probability-mass) probe choice, simulated on a pure history (no git)."""
import random

from agentbisect import planner as P
from agentbisect.bisect import narrow
from agentbisect.models import CommitResult


def simulate(n_commits, culprit, scores=None, plain=False):
    """Probes needed to isolate `culprit` (index into history, 1..n_commits) when history[0] is good and the
    last commit is bad."""
    history = [f"c{i}" for i in range(n_commits + 1)]
    results = {history[0]: CommitResult("good"), history[-1]: CommitResult("bad")}
    budget = P.plain_worst_case(n_commits)
    probes = 0
    while True:
        n = narrow(history, results)
        if n.done:
            assert n.culprit == culprit
            return probes
        if plain:
            mid = (n.lo + n.hi) / 2
            i = min(n.untested, key=lambda j: (abs(j - mid), j))
        else:
            i = P.weighted_pick(history, n, scores, budget - probes)
        results[history[i]] = CommitResult("bad" if i >= culprit else "good")
        probes += 1


def test_uniform_scores_are_exactly_plain():
    for n in range(2, 40):
        for score in (1.0, 0.37):
            sc = {f"c{i}": score for i in range(1, n + 1)}
            for c in range(1, n + 1):
                assert simulate(n, c, sc) == simulate(n, c, plain=True), (n, c)


def test_no_scores_is_plain():
    for n in (5, 13, 30):
        for c in range(1, n + 1):
            assert simulate(n, c, {}) == simulate(n, c, plain=True)


def test_never_beyond_plain_worst_case_for_any_scores():
    rng = random.Random(7)
    for _ in range(300):
        n = rng.randint(2, 60)
        sc = {f"c{i}": rng.choice([0, 0, rng.random(), rng.random() ** 6]) for i in range(1, n + 1)}
        for c in range(1, n + 1):
            r = simulate(n, c, sc)
            assert r <= P.plain_worst_case(n)  # smart rounds <= plain's worst-case rounds
            assert r <= simulate(n, c, plain=True) + 1  # and within one probe of plain on this very culprit


def test_confident_correct_ranking_beats_plain():
    n, c = 30, 21
    sc = {f"c{i}": 0.01 for i in range(1, n + 1)}
    sc[f"c{c}"] = 5.0
    assert simulate(n, c, sc) < simulate(n, c, plain=True)
    # when the top candidate is right the probes collapse onto it
    total = sum(simulate(n, k, sc) for k in range(1, n + 1))
    assert total <= sum(simulate(n, k, plain=True) for k in range(1, n + 1)) + n



def test_wrong_top_candidate_stays_within_plain_bound():
    n = 30
    sc = {f"c{i}": 0.01 for i in range(1, n + 1)}
    sc["c3"] = 9.0
    for c in range(1, n + 1):
        assert simulate(n, c, sc) <= P.plain_worst_case(n)


def test_tiny_scores_still_split_like_plain():
    n = 20
    sc = {f"c{i}": 1e-12 for i in range(1, n + 1)}
    for c in range(1, n + 1):
        assert simulate(n, c, sc) == simulate(n, c, plain=True)
