"""Offline demo: the whole loop on a fictional profile, with no accounts and no network.

  discover (recorded ATS responses) -> rank (embeddings) -> pack -> tailor (a recorded agent output)
  -> verify (the grounding gate) -> dashboard

The agent step is replayed from examples/demo/agent_output/<job id>/: one tailoring that is grounded (PASS)
and one with three typical hallucinations (FAIL), so you can see what the gate blocks. In real use that
folder is written by Claude Code following .claude/skills/tailor-cv/SKILL.md.
"""

from __future__ import annotations

import shutil

from . import db, pipeline
from .config import EXAMPLES, app_dir, home
from .grounding import report_md

AGENT_OUTPUT = EXAMPLES / "demo" / "agent_output"
# Statuses set after the run so the dashboard shows a realistic tracker.
STATUSES = {"ashby-fabrikam-health-f4b1-0001": ("applied", "demo: applied after review"),
            "greenhouse-tailspin-data-5002001": ("interview", "demo: first call booked")}


def run(log=print) -> None:
    h = home()
    if h.name != ".demo":
        raise SystemExit(f"The demo only writes to a .demo/ folder, not to {h}")
    shutil.rmtree(h / "applications", ignore_errors=True)  # every run starts from the same state

    res = pipeline.discover(no_hn=True, log=lambda m: None)
    log(f"\n1. DISCOVER + RANK: {res['scanned']} recorded postings from 4 fictional company boards "
        f"(Greenhouse, Lever, Ashby) -> {res['kept']} kept")
    for company, title, why in res["dropped"]:
        log(f"   dropped  {company[:16]:16} {title[:44]:44} {why[:60]}")
    con = db.connect()
    for r in con.execute("SELECT * FROM jobs ORDER BY score DESC"):
        log(f"   {r['score']:>5.1f}    {r['company'][:16]:16} {r['title'][:44]:44} {r['id']}")

    for d in sorted(AGENT_OUTPUT.iterdir()):
        jid = d.name
        log(f"\n2. PACK {jid}\n   -> {app_dir(jid)} (job.md + a cv.yaml template for the agent)")
        pipeline.pack(jid)
        for f in d.iterdir():
            shutil.copy(f, app_dir(jid) / f.name)
        note = (d / "NOTE.txt").read_text(encoding="utf-8").strip() if (d / "NOTE.txt").exists() else ""
        log(f"3. TAILOR (replayed agent output): {note}")
        pipeline.render_job(jid)
        result = pipeline.verify_job(jid)
        log("4. VERIFY (grounding gate)\n   " + report_md(jid, result).split("## Keywords")[0].strip()
            .replace("\n", "\n   "))

    for jid, (status, note) in STATUSES.items():
        if con.execute("SELECT 1 FROM jobs WHERE id = ?", (jid,)).fetchone():
            db.set_status(con, jid, status, note)
    log("\nNext:\n  python -m jobbot --demo web        dashboard on http://127.0.0.1:8766 (demo data only)\n"
        "  python -m jobbot --demo show <id>\n"
        "  python -m jobbot --demo apply <id>  would open the form in Chrome and stop before Submit "
        "(the demo boards point to example.com)")
