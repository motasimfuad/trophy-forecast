"""Offline backtest for main.py: the competition's scorer, the two house baselines, and
synthetic week-one histories.

There is no local grader for this competition, so this file re-implements the scoring
rule from the rules (pinball loss on the logit scale, clamped to 0.01..99.99) and checks
it on the rules' worked example. It then builds synthetic cases shaped like the final
(reads every 10-30 minutes, Exophase refreshing every few hours, rates rounded to 0.01,
players joining over the week), cuts them at 59.5 hours after 2026-11-19T00:00Z, and
scores main.py against the house persistence baseline and the house curve baseline on
the rate 144 hours after the origin.

    python tools/backtest.py            # scorer self-test + synthetic backtest
    python tools/backtest.py CASE.in ANSWER.json   # score main.py on one held-out case
"""

import json
import math
import os
import random
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(HERE, "..", "main.py")
LEVELS = [0.05, 0.25, 0.5, 0.75, 0.95]
Z = [-1.645, -0.674, 0.0, 0.674, 1.645]
ORIGIN = datetime(2026, 11, 19, tzinfo=timezone.utc)


def logit(x):
    p = min(max(x, 0.01), 99.99) / 100.0
    return math.log(p / (1.0 - p))


def pinball(y, qs):
    tot = 0.0
    for tau, q in zip(LEVELS, qs):
        d = logit(y) - logit(q)
        tot += 2.0 * max(tau * d, (tau - 1.0) * d)
    return tot / len(LEVELS)


def score(rows, actual):
    """Mean pinball loss over every trophy and level, plus coverage of the 90% band."""
    by_id = {r["id"]: r["q"] for r in rows}
    losses, cover = [], 0
    for tid, y in actual.items():
        q = by_id[tid]
        losses.append(pinball(y, q))
        cover += 1 if q[0] <= y <= q[4] else 0
    return sum(losses) / len(losses), cover / len(actual)


def self_test():
    rows = [{"id": 1, "q": [10, 15, 20, 25, 35]}, {"id": 2, "q": [10, 15, 20, 25, 35]}]
    loss, cov = score(rows, {1: 22.0, 2: 8.0})
    assert abs(loss - 0.403404) < 5e-7, loss
    assert cov == 0.5
    flat = [{"id": 1, "q": [30] * 5}, {"id": 2, "q": [5] * 5}]
    loss2, _ = score(flat, {1: 22.0, 2: 8.0})
    assert abs(loss2 - 0.46023) < 5e-6, loss2
    print("scorer self-test ok: %.6f %.6f" % (loss, loss2))


def baseline_persistence(case):
    last = case["history"][-1]["rates"]
    s = 0.5 * math.sqrt(case["horizon_minutes"] / 1440.0)
    rows = []
    for t, r in zip(case["trophies"], last):
        lg = logit(r if r is not None else 0.0)
        rows.append({"id": t["id"], "q": [round(100 / (1 + math.exp(-(lg + z * s))), 4) for z in Z]})
    return rows


def baseline_curve(case):
    """The house curve baseline as the rules describe it: per trophy, a least-squares
    line of logit(rate) on ln(1 + hours since the origin) over the last 24 hours of
    reads, read at the target; spread the random walk's and the residual combined."""
    hist = case["history"]
    at = datetime.fromisoformat(case["at"].replace("Z", "+00:00"))
    target = datetime.fromisoformat(case["target"].replace("Z", "+00:00"))
    recent = [h for h in hist if datetime.fromisoformat(h["at"].replace("Z", "+00:00")) >= at - timedelta(hours=24)]
    if len(recent) < 3:
        return baseline_persistence(case)
    s_rw = 0.5 * math.sqrt(case["horizon_minutes"] / 1440.0)
    xt = math.log(1 + (target - ORIGIN).total_seconds() / 3600.0)
    rows = []
    for i, t in enumerate(case["trophies"]):
        xs, ys = [], []
        for h in recent:
            r = h["rates"][i]
            if r is None:
                continue
            xs.append(math.log(1 + (datetime.fromisoformat(h["at"].replace("Z", "+00:00")) - ORIGIN).total_seconds() / 3600.0))
            ys.append(logit(r))
        mx = sum(xs) / len(xs)
        my = sum(ys) / len(ys)
        sxx = sum((x - mx) ** 2 for x in xs)
        b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx if sxx > 0 else 0.0
        a = my - b * mx
        res = math.sqrt(sum((y - a - b * x) ** 2 for x, y in zip(xs, ys)) / max(len(xs) - 2, 1))
        s = math.sqrt(s_rw ** 2 + res ** 2)
        m = a + b * xt
        rows.append({"id": t["id"], "q": [round(100 / (1 + math.exp(-(m + z * s))), 4) for z in Z]})
    return rows


def run_main(case):
    with tempfile.TemporaryDirectory() as d:
        ind, outd = os.path.join(d, "in"), os.path.join(d, "out")
        os.makedirs(ind)
        with open(os.path.join(ind, case["id"] + ".in"), "w", encoding="utf-8") as fh:
            json.dump(case, fh)
        env = dict(os.environ, MAKEFUN_INPUT_DIR=ind, MAKEFUN_OUTPUT_DIR=outd)
        subprocess.run([sys.executable, "-I", MAIN], env=env, check=True)
        with open(os.path.join(outd, case["id"] + ".out"), encoding="utf-8") as fh:
            return json.load(fh)["trophies"]


def synthetic_world(seed, slow=1.0, churn=0.0):
    """A make-believe launch week: players join Exophase's tracking over the week, each
    plays at a lognormal speed, and trophy k needs H_k hours of play. `slow` stretches
    the later weekdays' play, `churn` is the share of players who stop after a day."""
    rng = random.Random(seed)
    n_players = 6000
    joins = [abs(rng.gauss(0, 1)) * 40.0 + rng.random() * 30.0 for _ in range(n_players)]
    speed = [math.exp(rng.gauss(-0.9, 0.55)) for _ in range(n_players)]
    quits = [rng.random() < churn for _ in range(n_players)]
    hours_needed = [0.0] + sorted(math.exp(rng.uniform(0.0, 4.2)) for _ in range(40))
    hours_needed += [1e9] * 4  # never earned in week one (100% completion and the like)

    def state(t):
        tracked = [j for j in range(n_players) if joins[j] <= t]
        rates = []
        for h in hours_needed:
            got = 0
            for j in tracked:
                play_t = t - joins[j]
                if quits[j]:
                    play_t = min(play_t, 24.0)
                if t > 72:
                    play_t -= (1.0 - slow) * max(0.0, min(t, t) - max(72.0, joins[j]))
                if speed[j] * max(play_t, 0.0) >= h:
                    got += 1
            rates.append(round(100.0 * got / max(len(tracked), 1), 2))
        return len(tracked), rates

    return hours_needed, state


def synthetic_case(seed, slow=1.0, churn=0.0):
    rng = random.Random(seed + 1000)
    hours_needed, state = synthetic_world(seed, slow, churn)
    n = len(hours_needed)
    trophies = [{"id": i + 1, "name": "T%d" % (i + 1), "description": "", "tier": "bronze", "points": 15, "secret": False} for i in range(n)]
    at_h, target_h = 59.5, 144.0
    history = []
    refresh_every = rng.choice([1.0, 2.0, 3.0, 4.0])
    t = 5.0
    shown = state(4.5)
    next_refresh = 4.5 + refresh_every
    while t < at_h:
        if t >= next_refresh:
            shown = state(next_refresh)
            next_refresh += refresh_every
        stamp = (ORIGIN + timedelta(hours=t)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        history.append({"slot": stamp, "at": stamp, "players_tracked": shown[0], "trophies_earned": 0, "rates": list(shown[1])})
        t += 0.5
    # the answer: the page as of Exophase's last refresh before the target
    k = math.floor((target_h - 4.5) / refresh_every)
    answer = state(4.5 + k * refresh_every)[1]
    case = {
        "schema": "makefun/gta6-trophies-case/v1", "id": "s%03d" % seed,
        "at": (ORIGIN + timedelta(hours=at_h)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "horizon_minutes": int((target_h - at_h) * 60),
        "target": (ORIGIN + timedelta(hours=target_h)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "levels": LEVELS, "trophies": trophies, "history": history,
    }
    return case, {i + 1: answer[i] for i in range(n)}


def main():
    self_test()
    if len(sys.argv) == 3:
        with open(sys.argv[1], encoding="utf-8") as fh:
            case = json.load(fh)
        with open(sys.argv[2], encoding="utf-8") as fh:
            actual = {int(k): v for k, v in json.load(fh).items()}
        cases = [(case, actual)]
    else:
        worlds = [(s, 1.0, 0.0) for s in range(3)] + [(s, 0.7, 0.3) for s in range(3, 6)]
        cases = [synthetic_case(*w) for w in worlds]
    tot = {"main": [], "persistence": [], "curve": []}
    for case, actual in cases:
        for name, fn in (("main", run_main), ("persistence", baseline_persistence), ("curve", baseline_curve)):
            loss, cov = score(fn(case), actual)
            tot[name].append(loss)
            print("%s %-12s loss %.4f  cover90 %.2f" % (case["id"], name, loss, cov))
    for name, v in tot.items():
        print("MEAN %-12s %.4f" % (name, sum(v) / len(v)))


if __name__ == "__main__":
    main()
