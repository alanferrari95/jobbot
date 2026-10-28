"""jobbot: remote ML job search, agent-tailored CVs checked by a grounding gate, and form prefill. You click Submit.

Usage (from the repo root; `jobbot ...` works the same after `pip install -e .`):
  python -m jobbot demo                      offline demo: fictional profile + recorded postings, no accounts
  python -m jobbot [--demo] web [--port N]   dashboard at http://127.0.0.1:8765
  python -m jobbot daily                     discover + screen + pack best jobs + fetch AI news + queue posts
  python -m jobbot discover [--no-hn]
  python -m jobbot list [--status new] [--min-score 6] [--limit 30]
  python -m jobbot show <id>
  python -m jobbot pack <id>                 create applications/<id>/ and queue CV tailoring for the agent
  python -m jobbot draft <id> [--drafter local|gemini|ollama|openai-compatible] [--force]
                                             tailor without an agent session, then render + verify
  python -m jobbot render <id>|base
  python -m jobbot verify <id>               the grounding gate (exit code 1 on FAIL)
  python -m jobbot apply <id>                open the form, prefill, you click Submit
  python -m jobbot scan <id>|all             read each application form's questions into form.json
  python -m jobbot prepare-all               pack every verified good-fit new job and queue its tailoring
  python -m jobbot apply-batch               prefill ALL ready applications, one tab each; you click Submit
  python -m jobbot verify-jobs | verify-test screening layer: run it / measure it against your decisions
  python -m jobbot status <id> <status> [--note "..."]
  python -m jobbot people "<Company>"
  python -m jobbot tasks                     pending writing tasks for the agent
  python -m jobbot news [--refresh]
  python -m jobbot post-add --kind frontier|personal --topic "..." --file <md> [--source URL ...] [--news ID]

Global options (before the command): --home DIR (data folder, default: repo root), --profile DIR,
--demo (fictional profile, recorded postings, data in .demo/).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
from pathlib import Path

import yaml

from . import db, pipeline
from .config import REPO, answers, app_dir, base_cv, base_cv_stem, browser_profile_dir, cv_out_dir, fixed_cv, profile_dir
from .cv import render, resolve
from .grounding import report_md
from .people import people_md


def _print_rows(rows) -> None:
    for r in rows:
        print(f"{r['score']:>5.1f}  {r['status']:<9} {r['id']}\n       {r['company']} · {r['title']} · {r['location'][:60]}")


def cmd_demo(args) -> None:
    from .demo import run
    run()


def cmd_discover(args) -> None:
    res = pipeline.discover(args.no_hn, log=lambda m: print(m, file=sys.stderr))
    print(f"Scanned {res['scanned']} postings. Kept {res['kept']}, new {len(res['new'])}.")
    _print_rows(db.connect().execute(
        "SELECT * FROM jobs WHERE status = 'new' ORDER BY score DESC LIMIT 15").fetchall())


def cmd_daily(args) -> None:
    print(pipeline.daily(log=lambda m: print(m, file=sys.stderr)))


def cmd_verify_jobs(args) -> None:
    print(dict(pipeline.verify_candidates(limit=args.limit)))


def cmd_verify_test(args) -> None:
    from .screening_eval import run
    run(deep=not args.fast)


def cmd_discover_boards(args) -> None:
    from .discovery import discover_boards
    print(discover_boards())


def cmd_list(args) -> None:
    q, params = "SELECT * FROM jobs WHERE score >= ?", [args.min_score]
    if args.status:
        q += " AND status = ?"
        params.append(args.status)
    _print_rows(db.connect().execute(q + " ORDER BY score DESC LIMIT ?", params + [args.limit]).fetchall())


def cmd_show(args) -> None:
    r = db.get(db.connect(), args.id)
    print(f"{r['company']} · {r['title']}\n{r['location']}\nscore {r['score']} · {r['reasons']}\n"
          f"status {r['status']} {r['note']}\n{r['url']}\napply: {r['apply_url']}\n")
    print(textwrap.shorten(r["description"], 1500))


def cmd_pack(args) -> None:
    jid = pipeline.pack(args.id)
    print(f"Packed {app_dir(jid)}\nNext: ask Claude Code to \"tailor the CV for {jid}\" (or to process the task queue).")


def cmd_draft(args) -> bool:
    from .drafter import draft

    jid, result, note = draft(args.id, backend=args.drafter, force=args.force)
    print(f"Drafted {app_dir(jid) / 'cv.yaml'} ({note})")
    print(report_md(jid, result))
    return result["ok"]


def cmd_render(args) -> None:
    if args.id == "base":
        generic = profile_dir() / "generic_cv.yaml"  # optional selection of bullets for the general CV
        spec = yaml.safe_load(generic.read_text(encoding="utf-8")) if generic.exists() else None
        pdf = render(resolve(base_cv(), spec), cv_out_dir(), base_cv_stem())
    else:
        pdf = pipeline.render_job(args.id)
    print(f"Rendered {pdf}")


def cmd_verify(args) -> bool:
    r = db.get(db.connect(), args.id)
    result = pipeline.verify_job(r["id"])
    print(report_md(r["id"], result))
    return result["ok"]


def cmd_apply(args) -> None:
    from .autofill import run

    con = db.connect()
    r = db.get(con, args.id)
    if not cmd_verify(args):
        raise SystemExit("Verify failed: fix the errors above before applying.")
    d = app_dir(r["id"])
    cover = (d / "cover_letter.md").read_text(encoding="utf-8") if (d / "cover_letter.md").exists() else None
    report = run(r["apply_url"], pipeline.cv_to_send(r["id"]), answers(), cover, job_answers=pipeline.job_answers(r["id"]),
                 interactive=not args.from_web)
    (d / "autofill.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    if report.get("submitted"):
        db.set_status(con, r["id"], "applied")
        print("Marked as applied.")


def cmd_prepare_all(args) -> None:
    ids = pipeline.prepare_all()
    then = ("They are ready to open (`jobbot apply-batch`), all with the same CV." if fixed_cv()
            else "Tailoring is queued for the agent (`jobbot tasks`).")
    print(f"Prepared {len(ids)} jobs. {then}")


def cmd_apply_batch(args) -> None:
    n = pipeline.open_ready_batch()
    print(f"Opened {n} applications." if n else "No applications are ready (verify must pass).")


def cmd_login(args) -> None:
    """Open the jobbot Chrome profile on a site so you log in yourself (once); the session is kept."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(user_data_dir=str(browser_profile_dir()),
                                                    channel="chrome", headless=False)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(args.url)
        print("Log in in the browser window, then close it. The session stays saved for jobbot.")
        ctx.wait_for_event("close", timeout=0)


def cmd_scan(args) -> None:
    con = db.connect()
    ids = ([r["id"] for r in con.execute("SELECT id FROM jobs WHERE status IN ('packed','ready')")]
           if args.id == "all" else [db.get(con, args.id)["id"]])
    for jid in ids:
        try:
            data = pipeline.scan_form(jid)
            print(f"{jid}: {len(data['fields'])} fields -> {app_dir(jid) / 'form.json'}")
        except Exception as e:
            print(f"{jid}: scan failed ({e.__class__.__name__}: {str(e)[:120]})")


def cmd_status(args) -> None:
    con = db.connect()
    r = db.get(con, args.id)
    db.set_status(con, r["id"], args.status, args.note)
    print(f"{r['id']} -> {args.status}")


def cmd_people(args) -> None:
    print(people_md(args.company))


def cmd_tasks(args) -> None:
    rows = db.connect().execute("SELECT * FROM tasks WHERE status = 'pending' ORDER BY id").fetchall()
    if not rows:
        print("No pending tasks.")
    for t in rows:
        print(f"[{t['id']}] {t['kind']:<10} {pipeline.task_instructions(t['kind'], t['ref'])}")


def cmd_news(args) -> None:
    if args.refresh:
        print(pipeline.refresh_news())
    for n in db.connect().execute("SELECT * FROM news WHERE used = 0 ORDER BY published DESC LIMIT 25"):
        print(f"{n['id']}  {n['source']:<18} {n['title'][:90]}\n              {n['url']}")


def cmd_post_add(args) -> None:
    text = Path(args.file).read_text(encoding="utf-8").strip()
    con = db.connect()
    pid = db.add_post(con, args.kind, args.topic, text, args.source or [], args.news)
    ref = f"frontier:{args.news}" if args.news else f"personal:{args.topic}"
    db.finish_task(con, "draft_post", ref)
    print(f"Post #{pid} saved as draft. Review it in the dashboard.")


def cmd_web(args) -> None:
    import uvicorn

    port = args.port or (8766 if os.environ.get("JOBBOT_OFFLINE") == "1" else 8765)
    print(f"Dashboard: http://127.0.0.1:{port}  (data: {db.db_path()})")
    uvicorn.run("jobbot.web:app", host="127.0.0.1", port=port, log_level="warning")


def use_demo() -> None:
    """Fictional profile, recorded postings, offline embeddings and PDF; data in .demo/ (never in your folders)."""
    os.environ["JOBBOT_HOME"] = str(REPO / ".demo")
    os.environ["JOBBOT_PROFILE"] = str(REPO / "examples" / "profile")
    os.environ["JOBBOT_OFFLINE"] = "1"
    os.environ.setdefault("JOBBOT_EMBEDDER", "hash")
    os.environ.setdefault("JOBBOT_PDF", "basic")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(prog="jobbot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--home", help="data folder (applications/, cv/, logs/); default: the repo root or $JOBBOT_HOME")
    p.add_argument("--profile", help="profile folder; default: <home>/profile, else examples/profile")
    p.add_argument("--demo", action="store_true", help="fictional profile + recorded postings, data in .demo/")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("demo").set_defaults(f=cmd_demo)
    s = sub.add_parser("discover"); s.add_argument("--no-hn", action="store_true"); s.set_defaults(f=cmd_discover)
    sub.add_parser("daily").set_defaults(f=cmd_daily)
    sub.add_parser("discover-boards").set_defaults(f=cmd_discover_boards)
    s = sub.add_parser("verify-jobs"); s.add_argument("--limit", type=int, default=40); s.set_defaults(f=cmd_verify_jobs)
    s = sub.add_parser("verify-test"); s.add_argument("--fast", action="store_true"); s.set_defaults(f=cmd_verify_test)
    s = sub.add_parser("list"); s.add_argument("--status"); s.add_argument("--min-score", type=float, default=0)
    s.add_argument("--limit", type=int, default=30); s.set_defaults(f=cmd_list)
    for name, f in [("show", cmd_show), ("pack", cmd_pack), ("render", cmd_render), ("verify", cmd_verify)]:
        s = sub.add_parser(name); s.add_argument("id"); s.set_defaults(f=f)
    s = sub.add_parser("draft"); s.add_argument("id"); s.add_argument("--force", action="store_true")
    s.add_argument("--drafter", choices=["local", "gemini", "ollama", "openai-compatible"],
                   help="default: $JOBBOT_DRAFTER, targets.yaml `drafter`, else local (no LLM)")
    s.set_defaults(f=cmd_draft)
    s = sub.add_parser("apply"); s.add_argument("id"); s.add_argument("--from-web", action="store_true")
    s.set_defaults(f=cmd_apply)
    sub.add_parser("prepare-all").set_defaults(f=cmd_prepare_all)
    s = sub.add_parser("scan"); s.add_argument("id", help="job id or 'all'"); s.set_defaults(f=cmd_scan)
    s = sub.add_parser("login"); s.add_argument("url", nargs="?", default="https://www.getonbrd.com/login")
    s.set_defaults(f=cmd_login)
    sub.add_parser("apply-batch").set_defaults(f=cmd_apply_batch)
    s = sub.add_parser("status"); s.add_argument("id"); s.add_argument("status", choices=db.STATUSES)
    s.add_argument("--note", default=""); s.set_defaults(f=cmd_status)
    s = sub.add_parser("people"); s.add_argument("company"); s.set_defaults(f=cmd_people)
    sub.add_parser("tasks").set_defaults(f=cmd_tasks)
    s = sub.add_parser("news"); s.add_argument("--refresh", action="store_true"); s.set_defaults(f=cmd_news)
    s = sub.add_parser("post-add"); s.add_argument("--kind", choices=["frontier", "personal"], required=True)
    s.add_argument("--topic", required=True); s.add_argument("--file", required=True)
    s.add_argument("--source", action="append"); s.add_argument("--news"); s.set_defaults(f=cmd_post_add)
    s = sub.add_parser("web"); s.add_argument("--port", type=int); s.set_defaults(f=cmd_web)
    args = p.parse_args()
    if args.demo or args.cmd == "demo":
        use_demo()
    if args.home:
        os.environ["JOBBOT_HOME"] = str(Path(args.home).resolve())
    if args.profile:
        os.environ["JOBBOT_PROFILE"] = str(Path(args.profile).resolve())
    if args.f(args) is False:
        sys.exit(1)


if __name__ == "__main__":
    main()
