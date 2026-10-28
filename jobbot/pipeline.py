"""Operations shared by the CLI, the dashboard and the scheduled daily run."""

from __future__ import annotations

import collections
import concurrent.futures as cf
import importlib.util
import json
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone

import yaml

from . import db
from .config import answers, app_dir, apps_dir, base_cv, companies, fixed_cv, load_yaml, offline, profile_dir, targets
from .cv import render, resolve
from .grounding import report_md, verify
from .match import TITLE_BROAD, evaluate_many
from .news import fetch_all
from .people import people_md
from .sources import BOARDS, FETCHERS, Job, hn_who_is_hiring, recorded, torre_details

MAX_OPEN_FRONTIER = 4  # don't queue more frontier drafts while this many are waiting
OFFICIAL = {"OpenAI", "Google DeepMind", "Google Research", "Google AI", "Hugging Face", "Microsoft Research"}


# ------------------------------------------------------------------ jobs

def personal_post_ideas() -> list[str]:
    """Topics for personal posts, from the optional profile/posts.yaml (`personal_ideas:`)."""
    f = profile_dir() / "posts.yaml"
    return load_yaml(f).get("personal_ideas", []) if f.exists() else []


def local_sources() -> dict:
    """Sources you keep out of the repo: profile/local_sources.py defining SOURCES = {name: fetch() -> list[Job]}."""
    f = profile_dir() / "local_sources.py"
    if not f.exists():
        return {}
    spec = importlib.util.spec_from_file_location("jobbot_local_sources", f)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return dict(getattr(module, "SOURCES", {}))


def enabled_sources(tg: dict) -> dict:
    """Extra sources to query besides the company boards, plus your local ones.
    targets.yaml `sources:` turns any of them off ({name: false})."""
    from .atsindex import ats_index

    conf = tg.get("sources") or {}
    every = dict(BOARDS) | {"atsindex": ats_index, "hn": hn_who_is_hiring} | local_sources()
    return {name: f for name, f in every.items() if conf.get(name, True)}


def discover(no_hn: bool = False, log=print) -> dict:
    base, tg = base_cv(), targets()
    jobs: list[Job] = []

    def fetch(c):
        try:
            if offline():
                return recorded(c["ats"], c["token"], c["name"])
            return FETCHERS[c["ats"]](c["token"], c["name"])
        except Exception as e:  # one broken board must not stop the run
            log(f"  ! {c['name']} ({c['ats']}): {e}")
            return []

    with cf.ThreadPoolExecutor(10) as ex:
        for batch in ex.map(fetch, companies()):
            jobs.extend(batch)
    extra = {} if offline() else enabled_sources(tg)  # offline demo: only the recorded company boards
    if no_hn:
        extra.pop("hn", None)
    for name, fetch_board in extra.items():
        try:
            jobs.extend(fetch_board())
        except Exception as e:
            log(f"  ! {name}: {e}")

    for job in jobs:  # Torre search results carry only a tagline; fetch the full text for plausible ones
        if job.source == "torre" and TITLE_BROAD.search(job.title):
            torre_details(job)
    con = db.connect()
    kept, new_ids, dropped = 0, [], []
    for job, (keep, score, reasons, missing) in zip(jobs, evaluate_many(jobs, tg, base)):
        if not keep:
            why = f"score {score:.1f} is below min_score" if score else (reasons[0] if reasons else "")
            dropped.append((job.company, job.title, why))
            continue
        kept += 1
        if db.upsert(con, job, score, reasons, missing):
            new_ids.append(job.id)
    con.commit()
    return {"scanned": len(jobs), "kept": kept, "new": new_ids, "dropped": dropped}


def job_md(r) -> str:
    return (f"# {r['company']}: {r['title']}\n\n- Location: {r['location']}\n- Posting: {r['url']}\n"
            f"- Apply: {r['apply_url']}\n- Score: {r['score']} ({r['reasons']})\n"
            f"- Asks for (not in profile): {r['missing'] or '-'}\n\n## Description\n\n{r['description']}\n")


def spec_template(base: dict, job_id: str) -> str:
    spec = {
        "job_id": job_id,
        "title": base["title"],
        "summary": " ".join(base["summary"].split()),
        "experience": {role["id"]: [b["id"] for g in role["groups"] for b in g["bullets"]]
                       for role in base["experience"]},
        "project": [b["id"] for p in base["projects"] for b in p["bullets"]],
        "skills": base["skills"],
    }
    header = ("# Tailored CV for this job. Rules: .claude/skills/tailor-cv/SKILL.md\n"
              "# Reorder/drop bullet ids, rewrite title/summary, reorder skills. Never add facts.\n")
    return header + yaml.safe_dump(spec, allow_unicode=True, sort_keys=False, width=110)


def pack(job_id: str) -> str:
    """Write the job's folder. Tailored CVs: queue the tailoring (the job is ready once its CV passes the gate).
    Fixed CV (targets.yaml `cv: fixed`): nothing is written or queued per job, so the job is ready right away."""
    con = db.connect()
    r = db.get(con, job_id)
    d = app_dir(r["id"])
    d.mkdir(parents=True, exist_ok=True)
    (d / "job.md").write_text(job_md(r), encoding="utf-8")
    (d / "people.md").write_text(people_md(r["company"]), encoding="utf-8")
    if fixed_cv():
        if r["status"] in ("new", "packed"):
            db.set_status(con, r["id"], "ready")
        con.execute("UPDATE tasks SET status = 'cancelled', note = 'fixed CV', updated = ? "
                    "WHERE kind = 'tailor' AND ref = ? AND status = 'pending'", (db.now(), r["id"]))
        con.commit()
        return r["id"]
    if not (d / "cv.yaml").exists():
        (d / "cv.yaml").write_text(spec_template(base_cv(), r["id"]), encoding="utf-8")
    if r["status"] == "new":
        db.set_status(con, r["id"], "packed")
    db.enqueue(con, "tailor", r["id"])
    return r["id"]


def cv_to_send(job_id: str):
    """The PDF to upload. With a fixed CV it is copied into the job's folder first, so cv.pdf there is always
    exactly what was sent."""
    d = app_dir(job_id)
    if (src := fixed_cv()):
        d.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, d / "cv.pdf")
    return d / "cv.pdf"


def render_job(job_id: str):
    r = db.get(db.connect(), job_id)
    d = app_dir(r["id"])
    spec = yaml.safe_load((d / "cv.yaml").read_text(encoding="utf-8"))
    return render(resolve(base_cv(), spec), d, "cv")


def verify_job(job_id: str) -> dict:
    con = db.connect()
    r = db.get(con, job_id)
    d = app_dir(r["id"])
    if (src := fixed_cv()):  # nothing was written for this job: only check that the CV to send exists
        errors = [] if src.exists() else [f"fixed CV not found: {src} (run `jobbot render base` or fix targets.yaml)"]
        result = {"ok": not errors, "errors": errors, "warnings": ["fixed CV: not tailored for this job"],
                  "keywords": {}}
        d.mkdir(parents=True, exist_ok=True)
        (d / "verify.md").write_text(report_md(r["id"], result), encoding="utf-8")
        return result
    result = verify(d, base_cv(), r["description"], answers())
    (d / "verify.md").write_text(report_md(r["id"], result), encoding="utf-8")
    (d / "verify.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    if result["ok"] and r["status"] in ("new", "packed"):
        db.set_status(con, r["id"], "ready")
        db.finish_task(con, "tailor", r["id"])
    return result


def job_answers(job_id: str) -> dict:
    """Per-job answers: {labels: {exact question: answer}, questions: [{match, answer}]}."""
    f = app_dir(job_id) / "answers.yaml"
    data = yaml.safe_load(f.read_text(encoding="utf-8")) if f.exists() else None
    if isinstance(data, list):  # older format: a list of regex rules
        return {"questions": data}
    return data or {}


def scan_form(job_id: str, needs_login: bool | None = None) -> dict:
    """Read-only scan of the job's application form into applications/<id>/form.json."""
    from .formscan import scan

    r = db.get(db.connect(), job_id)
    if needs_login is None:
        needs_login = r["source"] == "getonboard"
    return scan(r["apply_url"], app_dir(r["id"]) / "form.json", needs_login=needs_login)


# ------------------------------------------------------------------ news + posts

def _news_rank(n) -> float:
    return (3 if n["source"] in OFFICIAL else 0) + (n["points"] or 0) / 100


def refresh_news(days: int = 3) -> dict:
    items, errors = fetch_all(days)
    con = db.connect()
    added = sum(db.add_news(con, it) for it in items)
    con.commit()
    return {"fetched": len(items), "new": added, "errors": errors}


def queue_posts() -> list[str]:
    """Queue the best unused news item as a frontier draft, and one personal post per week."""
    con = db.connect()
    queued = []
    open_frontier = con.execute(
        "SELECT (SELECT COUNT(*) FROM posts WHERE kind='frontier' AND status IN ('draft','approved')) + "
        "(SELECT COUNT(*) FROM tasks WHERE kind='draft_post' AND ref LIKE 'frontier:%' AND status='pending')"
    ).fetchone()[0]
    if open_frontier < MAX_OPEN_FRONTIER:
        pending = {r["ref"].split(":", 1)[1] for r in con.execute(
            "SELECT ref FROM tasks WHERE kind='draft_post' AND ref LIKE 'frontier:%'")}
        cands = [n for n in con.execute("SELECT * FROM news WHERE used = 0").fetchall() if n["id"] not in pending]
        if cands:
            best = max(cands, key=_news_rank)
            if db.enqueue(con, "draft_post", f"frontier:{best['id']}"):
                queued.append(f"frontier: {best['title']}")
    week_ago = (datetime.now() - timedelta(days=7)).isoformat()
    recent_personal = con.execute(
        "SELECT COUNT(*) FROM tasks WHERE kind='draft_post' AND ref LIKE 'personal:%' AND created > ?",
        (week_ago,)).fetchone()[0]
    if not recent_personal:
        used = {r["topic"] for r in con.execute("SELECT topic FROM posts WHERE kind='personal'")}
        done = {r["ref"].split(":", 1)[1] for r in con.execute(
            "SELECT ref FROM tasks WHERE ref LIKE 'personal:%'")}
        idea = next((i for i in personal_post_ideas() if i not in used and i not in done), None)
        if idea and db.enqueue(con, "draft_post", f"personal:{idea}"):
            queued.append(f"personal: {idea}")
    return queued


def task_instructions(kind: str, ref: str) -> str:
    """What to do for a queued task (read by Claude Code in a session)."""
    con = db.connect()
    if kind == "tailor":
        return f"Tailor the CV for {ref} with the tailor-cv skill (applications/{ref}/)."
    typ, arg = ref.split(":", 1)
    if typ == "frontier":
        n = con.execute("SELECT * FROM news WHERE id = ?", (arg,)).fetchone()
        return (f"Frontier post (POSTING.md) about: {n['title']} ({n['source']}) {n['url']} "
                f"-- save it with --news {arg}")
    return f"Personal post (POSTING.md) about: {arg}"


def next_slot(con) -> str:
    """Next free weekday slot at 14:30 local time (≈ 8:30 US Eastern), at most one post per day."""
    taken = {r[0][:10] for r in con.execute(
        "SELECT scheduled_for FROM posts WHERE scheduled_for IS NOT NULL AND status IN ('approved','published')")}
    day = datetime.now()
    if day.hour >= 14:
        day += timedelta(days=1)
    while day.weekday() >= 5 or day.strftime("%Y-%m-%d") in taken:
        day += timedelta(days=1)
    return day.strftime("%Y-%m-%d") + "T14:30"


# ------------------------------------------------------------------ daily

def verify_candidates(min_score: float | None = None, limit: int = 40, log=print) -> dict:
    """Run the screening layer (jobbot/screening.py) on new jobs above the threshold that weren't screened yet,
    then on 'unverified' ones checked more than targets.yaml `reverify_after_days` (default 7) ago: a page that
    errored may load now, and the weekly board discovery may have found the company page behind an aggregator link.
    'location_unclear' is not retried (the same page gives the same answer): you OK it in the dashboard.
    Network checks (company page + form scan, ~15 s each) run only for the top `limit`, new jobs first."""
    from . import screening

    con = db.connect()
    tg = targets()
    min_score = tg.get("verify_min_score", 6.5) if min_score is None else min_score
    cutoff = (datetime.now(timezone.utc) - timedelta(days=tg.get("reverify_after_days", 7))).isoformat(timespec="seconds")
    rows = con.execute("SELECT * FROM jobs WHERE status IN ('new', 'packed') AND score >= ? AND (verdict IS NULL OR "
                       "(verdict = 'unverified' AND COALESCE(verified_at, '') < ?)) "
                       "ORDER BY verdict IS NOT NULL, score DESC", (min_score, cutoff)).fetchall()
    all_rows = con.execute("SELECT * FROM jobs").fetchall()
    res = screening.verify_rows(rows[:limit], all_rows, deep=not offline(), log=log)
    res |= screening.verify_rows(rows[limit:], all_rows, deep=False, log=log)  # text checks only; retried next run
    for jid, (v, _) in list(res.items()):
        if v == "unverified" and "network checks not run" in res[jid][1][0]:
            res.pop(jid)  # leave verdict NULL so the next run does the deep checks
    screening.save(res)
    for jid, (v, reasons) in res.items():
        if v == "discarded":
            db.set_status(con, jid, "skipped", "screening: " + "; ".join(reasons))
    return collections.Counter(v for v, _ in res.values())


def prepare_all() -> list[str]:
    """Pack every verified (or OKed by hand) new job that fits well enough to apply (no fixed limit) and queue its
    CV tailoring.
    With targets.yaml `auto_draft: true`, `jobbot draft` (backend: targets.yaml `drafter`, default local) also
    writes, renders and verifies a first CV.
    With `cv: fixed` there is no tailoring: jobs go straight to ready, including ones packed before the switch, and
    tailoring tasks still pending are cancelled."""
    con = db.connect()
    tg = targets()
    statuses = ("new", "packed") if fixed_cv(tg) else ("new",)
    if fixed_cv(tg):
        con.execute("UPDATE tasks SET status = 'cancelled', note = 'fixed CV', updated = ? "
                    "WHERE kind = 'tailor' AND status = 'pending'", (db.now(),))
        con.commit()
    rows = con.execute(f"SELECT id FROM jobs WHERE status IN ({','.join('?' * len(statuses))}) AND score >= ? "
                       "AND verdict IN ('verified', 'ok_manual') ORDER BY score DESC",
                       (*statuses, tg.get("auto_apply_min_score", 6))).fetchall()
    ids = [pack(r["id"]) for r in rows]
    if tg.get("auto_draft") and not fixed_cv(tg):
        from .drafter import draft
        for jid in ids:
            try:
                draft(jid, log=lambda m: print(m, file=sys.stderr))
            except (Exception, SystemExit) as e:  # an edited spec or a PDF error must not stop the daily run
                print(f"  ! draft {jid}: {e}", file=sys.stderr)
    return ids


def fit_guard(job_id: str) -> bool:
    """Re-checks a job against the CURRENT rules (role type, techs, salary, location) right before applying.
    A job that no longer fits is marked skipped with the reason and never opened."""
    from .match import _stage1

    con = db.connect()
    r = db.get(con, job_id)
    job = Job(source=r["source"], company=r["company"], title=r["title"], location=r["location"], url=r["url"],
              apply_url=r["apply_url"], description=r["description"], external_id=r["id"],
              remote=bool(r["remote"]), extra=json.loads(r["extra"] or "{}"))
    # Only the hard rules (role type, location, techs, salary); the fit score may move as the model changes.
    keep, reasons, _, _ = _stage1(job, targets(), base_cv()["never_claim"])
    if not keep:
        db.set_status(con, job_id, "skipped", "guardrail: " + "; ".join(reasons))
    return keep


def ready_items() -> list[dict]:
    con = db.connect()
    items = []
    # Only jobs that passed the screening layer (or that the candidate OKed by hand) can be opened for sending.
    for r in con.execute("SELECT * FROM jobs WHERE status = 'ready' AND COALESCE(verdict, '') IN ('verified', 'ok_manual') "
                         "ORDER BY score DESC"):
        d = app_dir(r["id"])
        cover = (d / "cover_letter.md").read_text(encoding="utf-8") if (d / "cover_letter.md").exists() else None
        items.append({"id": r["id"], "url": r["apply_url"], "cv_pdf": str(cv_to_send(r["id"])),
                      "job_answers": job_answers(r["id"]), "cover": cover})
    return items


def open_ready_batch() -> int:
    """Prefill every ready application in its own Chrome tab (nothing is submitted)."""
    from .autofill import open_batch

    items = [it for it in ready_items() if fit_guard(it["id"]) and verify_job(it["id"])["ok"]]

    def on_filled(job_id, report):
        (app_dir(job_id) / "autofill.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        con = db.connect()
        con.execute("UPDATE jobs SET note = ?, updated = ? WHERE id = ?",
                    ("opened for submission " + db.now()[:16], db.now(), job_id))
        con.commit()

    if items:
        open_batch(items, answers(), on_filled)
    return len(items)


def daily(log=print) -> str:
    from .leads import from_hn_jobs

    con = db.connect()
    started = db.now()
    boards_note = ""
    from .discovery import auto_path, discover_boards
    auto = auto_path()
    if not offline() and (not auto.exists() or (datetime.now().timestamp() - auto.stat().st_mtime) > 7 * 86400):
        try:  # weekly: look for new company boards in the YC / remoteintech directories
            b = discover_boards(log=log)
            boards_note = f" | boards {b['boards']}"
        except Exception as e:
            log(f"  ! discover-boards: {e}")
    res = discover(log=log)
    try:
        verdicts = dict(verify_candidates(log=lambda m: None))
    except Exception as e:  # the verification layer must never break the twice-daily run
        log(f"  ! verify: {e}")
        verdicts = {}
    packed = prepare_all()
    leads_added = from_hn_jobs()
    news = refresh_news()
    queued = queue_posts()
    summary = (f"jobs: scanned {res['scanned']}, kept {res['kept']}, new {len(res['new'])}, "
               f"verified {verdicts.get('verified', 0)} (discarded {verdicts.get('discarded', 0)}, "
               f"unverifiable {verdicts.get('unverified', 0)}), prepared {len(packed)} | leads +{leads_added} "
               f"| news +{news['new']} | posts queued {len(queued)}"
               f"{boards_note}")
    if news["errors"]:
        summary += " | news errors: " + "; ".join(e[:60] for e in news["errors"])
    db.log_run(con, "daily", started, summary)
    return summary


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def posts_dir():
    return apps_dir() / "posts"
