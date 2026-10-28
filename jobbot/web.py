"""Local dashboard (http://127.0.0.1:8765). Bound to localhost only; no data leaves the machine
except the public job/news fetches that the CLI already does."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import db, leads, pipeline
from .config import REPO, app_dir, base_cv_stem, cv_out_dir, fixed_cv, profile_dir

STATIC = Path(__file__).parent / "static"
app = FastAPI(title="jobbot")
_run = {"running": False, "last": ""}


def _rows(rows) -> list[dict]:
    return [dict(r) for r in rows]


@app.get("/", response_class=HTMLResponse)
def index():
    return (STATIC / "index.html").read_text(encoding="utf-8")


# ------------------------------------------------------------------ summary + runs

@app.get("/api/summary")
def summary():
    con = db.connect()
    counts = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
    posts = {r["status"]: r["n"] for r in con.execute("SELECT status, COUNT(*) n FROM posts GROUP BY status")}
    today = db.now()[:10]
    due = _rows(con.execute("SELECT id, topic, scheduled_for FROM posts WHERE status='approved' "
                            "AND substr(scheduled_for,1,10) <= ? ORDER BY scheduled_for", (today,)))
    last = con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    tasks = con.execute("SELECT COUNT(*) FROM tasks WHERE status='pending'").fetchone()[0]
    confirm = [q["match"] for q in yaml.safe_load((profile_dir() / "answers.yaml").read_text(encoding="utf-8"))["questions"]
               if q.get("confirm")]
    return {"jobs": counts, "posts": posts, "due_posts": due, "pending_tasks": tasks,
            "last_run": dict(last) if last else None, "running": _run["running"], "needs_confirm": confirm,
            "fixed_cv": bool(fixed_cv())}


@app.post("/api/run/daily")
def run_daily():
    if _run["running"]:
        return {"started": False}

    def work():
        _run["running"] = True
        try:
            _run["last"] = pipeline.daily(log=lambda m: None)
        except Exception as e:
            _run["last"] = f"error: {e}"
        finally:
            _run["running"] = False

    threading.Thread(target=work, daemon=True).start()
    return {"started": True}


@app.get("/api/runs")
def runs():
    return _rows(db.connect().execute("SELECT * FROM runs ORDER BY id DESC LIMIT 20"))


# ------------------------------------------------------------------ jobs

@app.get("/api/jobs")
def jobs(status: str = "", q: str = ""):
    sql = ("SELECT id, source, company, title, location, url, apply_url, score, reasons, missing, status, note, verdict, verdict_reasons, "
           "first_seen, applied_at FROM jobs WHERE 1=1")
    params: list = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if q:
        sql += " AND (company LIKE ? OR title LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    return _rows(db.connect().execute(sql + " ORDER BY score DESC, first_seen DESC LIMIT 300", params))


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    r = db.get(db.connect(), job_id)
    d = app_dir(r["id"])
    read = lambda n: (d / n).read_text(encoding="utf-8") if (d / n).exists() else None
    return {**dict(r), "files": {n: (d / n).exists() for n in ("cv.yaml", "cv.pdf", "answers.yaml", "cover_letter.md")},
            "verify": read("verify.md"), "people": read("people.md")}


class StatusIn(BaseModel):
    status: str
    note: str = ""


@app.post("/api/jobs/{job_id}/status")
def job_status(job_id: str, body: StatusIn):
    db.set_status(db.connect(), job_id, body.status, body.note)
    return {"ok": True}


@app.post("/api/jobs/{job_id}/pack")
def job_pack(job_id: str):
    return {"id": pipeline.pack(job_id)}


@app.post("/api/jobs/{job_id}/ok")
def job_ok(job_id: str):
    """You checked a job the screening layer couldn't confirm (location unclear, company page not found) and it fits."""
    con = db.connect()
    con.execute("UPDATE jobs SET verdict = 'ok_manual', verdict_reasons = 'OKed by you in the dashboard', verified_at = ? "
                "WHERE id = ?", (db.now(), job_id))
    con.commit()
    return {"ok": True}


@app.post("/api/jobs/{job_id}/verify")
def job_verify(job_id: str):
    if not fixed_cv():
        pipeline.render_job(job_id)
    return pipeline.verify_job(job_id)


@app.post("/api/jobs/{job_id}/apply")
def job_apply(job_id: str):
    r = db.get(db.connect(), job_id)
    res = pipeline.verify_job(r["id"])
    if not res["ok"]:
        raise HTTPException(400, "Verify failed: " + "; ".join(res["errors"]))
    subprocess.Popen([sys.executable, "-m", "jobbot", "apply", r["id"], "--from-web"], cwd=REPO,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {"started": True}


@app.get("/files/{job_id}/cv.pdf")
def cv_pdf(job_id: str):
    f = app_dir(db.get(db.connect(), job_id)["id"]) / "cv.pdf"
    if not f.exists():
        raise HTTPException(404)
    return FileResponse(f, media_type="application/pdf")


@app.get("/files/base-cv.pdf")
def base_pdf():
    f = cv_out_dir() / f"{base_cv_stem()}.pdf"
    if not f.exists():
        raise HTTPException(404, "Run `jobbot render base` first.")
    return FileResponse(f, media_type="application/pdf")


# ------------------------------------------------------------------ tasks, news, posts

@app.get("/api/tasks")
def tasks():
    rows = db.connect().execute("SELECT * FROM tasks ORDER BY status = 'pending' DESC, id DESC LIMIT 50").fetchall()
    return [{**dict(t), "what": pipeline.task_instructions(t["kind"], t["ref"])} for t in rows]


@app.get("/api/news")
def news():
    return _rows(db.connect().execute("SELECT * FROM news ORDER BY used, published DESC LIMIT 80"))


@app.post("/api/news/{news_id}/queue")
def news_queue(news_id: str):
    return {"queued": db.enqueue(db.connect(), "draft_post", f"frontier:{news_id}")}


@app.get("/api/posts")
def posts():
    rows = db.connect().execute(
        "SELECT * FROM posts ORDER BY CASE status WHEN 'draft' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END, "
        "scheduled_for, id DESC").fetchall()
    return [{**dict(p), "sources": json.loads(p["sources"] or "[]")} for p in rows]


class PostIn(BaseModel):
    text: str | None = None
    status: str | None = None
    scheduled_for: str | None = None


@app.post("/api/posts/{post_id}")
def post_update(post_id: int, body: PostIn):
    con = db.connect()
    p = con.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if not p:
        raise HTTPException(404)
    text = body.text if body.text is not None else p["text"]
    status = body.status or p["status"]
    if status not in db.POST_STATUSES:
        raise HTTPException(400, "bad status")
    when = body.scheduled_for or p["scheduled_for"]
    if status == "approved" and not when:
        when = pipeline.next_slot(con)
    published = db.now() if status == "published" and p["status"] != "published" else p["published_at"]
    con.execute("UPDATE posts SET text=?, status=?, scheduled_for=?, published_at=?, updated=? WHERE id=?",
                (text, status, when, published, db.now(), post_id))
    con.commit()
    return {"ok": True, "scheduled_for": when}


# ------------------------------------------------------------------ answers that need your confirmation

class AnswerIn(BaseModel):
    match: str
    answer: str


@app.post("/api/answers/confirm")
def answers_confirm(body: AnswerIn):
    path = profile_dir() / "answers.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    for q in data["questions"]:
        if q["match"] == body.match:
            q["answer"] = body.answer
            q["confirm"] = False
            break
    else:
        raise HTTPException(404)
    header = "# Standard answers for application forms. Edited from the dashboard.\n"
    path.write_text(header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8")
    return {"ok": True}


@app.get("/api/answers")
def answers_get():
    return yaml.safe_load((profile_dir() / "answers.yaml").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ batch actions

@app.post("/api/prepare-all")
def prepare_all():
    return {"prepared": pipeline.prepare_all()}


@app.post("/api/apply-batch")
def apply_batch():
    n = len(pipeline.ready_items())
    if not n:
        raise HTTPException(400, "No applications are ready (tailored CV that passed verify).")
    subprocess.Popen([sys.executable, "-m", "jobbot", "apply-batch"], cwd=REPO,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {"opening": n}


# ------------------------------------------------------------------ recruiter leads

@app.get("/api/leads")
def leads_list():
    rows = db.connect().execute("SELECT * FROM leads ORDER BY status = 'new' DESC, id DESC LIMIT 200").fetchall()
    return [{**dict(r), "emails": json.loads(r["emails"] or "[]"), "links": json.loads(r["links"] or "[]"),
             "draft": leads.draft_message(dict(r))} for r in rows]


@app.get("/api/leads/searches")
def leads_searches():
    return leads.linkedin_post_searches()


class LeadIn(BaseModel):
    text: str
    url: str = ""
    author: str = ""


@app.post("/api/leads")
def leads_add(body: LeadIn):
    lid = leads.add("manual", body.text, body.url, body.author)
    return {"id": lid, "duplicate": lid is None}


@app.post("/api/leads/{lead_id}/status")
def leads_status(lead_id: int, body: StatusIn):
    if body.status not in ("new", "contacted", "replied", "ignored"):
        raise HTTPException(400, "bad status")
    con = db.connect()
    con.execute("UPDATE leads SET status = ?, note = ?, updated = ? WHERE id = ?",
                (body.status, body.note, db.now(), lead_id))
    con.commit()
    return {"ok": True}
