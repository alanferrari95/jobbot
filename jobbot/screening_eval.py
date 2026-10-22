"""Measure the screening layer against the candidate's real decisions (profile/verify_testset.yaml).

`python -m jobbot verify-test [--fast]`: every applied job should pass, every rejected one should not.
Format:
    must_pass: [<job id>, ...]                       # jobs the candidate applied to
    must_fail: {<job id>: {category: location|form|stack|role_type|closed|duplicate|subjective, reason: "..."}}
"subjective" rejections (pay guesses, "a third variant of the same role") are reported but not scored.
The metrics are in-sample: the rules were written from these cases. Add every new decision and re-run.
"""

from __future__ import annotations

import collections

from . import db
from .config import load_yaml, profile_dir
from .screening import verify_rows

PASSING = ("verified", "location_unclear")


def run(deep: bool = True, log=print) -> dict:
    f = profile_dir() / "verify_testset.yaml"
    if not f.exists():
        log(f"No test set at {f}: add the jobs you applied to (must_pass) and rejected (must_fail).")
        return {}
    ts = load_yaml(f)
    must_pass, must_fail = ts.get("must_pass") or [], ts.get("must_fail") or {}
    con = db.connect()
    all_rows = con.execute("SELECT * FROM jobs").fetchall()
    by_id = {r["id"]: r for r in all_rows}
    ids = [i for i in must_pass + list(must_fail) if i in by_id]
    # Judge each job as it was BEFORE the decision: the others list only holds applied jobs.
    res = verify_rows([by_id[i] for i in ids], all_rows, deep=deep, log=lambda m: None)
    if not deep:  # --fast: passing the text checks is all that can be judged
        res = {i: ("verified", r) if r == ["network checks not run"] else (v, r) for i, (v, r) in res.items()}

    wrong_pass = [(i, res[i]) for i in must_pass if i in res and res[i][0] not in PASSING]
    unclear = [i for i in must_pass if i in res and res[i][0] == "location_unclear"]
    per_cat = collections.defaultdict(lambda: [0, 0])
    missed = []
    for i, meta in must_fail.items():
        if i not in res:
            continue
        caught = res[i][0] not in PASSING
        per_cat[meta["category"]][0] += caught
        per_cat[meta["category"]][1] += 1
        if not caught:
            missed.append((i, meta))
    log(f"Must pass: {len(must_pass) - len(wrong_pass)}/{len(must_pass)} pass "
        f"({len(unclear)} as 'location_unclear': {', '.join(d[:25] for d in unclear)})")
    for i, (v, r) in wrong_pass:
        log(f"   x {i[:50]:50} -> {v}: {'; '.join(r)[:110]}")
    log("Must fail (caught / total per category):")
    for c, (a, n) in sorted(per_cat.items()):
        log(f"   {c:12} {a}/{n}" + ("   (not scored)" if c == "subjective" else ""))
    for i, meta in missed:
        log(f"   missed: {i[:50]:50} [{meta['category']}] {meta['reason'][:60]}")
    return {"res": res, "wrong_pass": wrong_pass, "missed": missed, "per_cat": dict(per_cat)}
