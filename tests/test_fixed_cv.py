"""`cv: fixed`: one PDF for every job, nothing tailored or queued per job."""

from __future__ import annotations

import shutil

import pytest
import yaml

from jobbot import db, pipeline
from jobbot.config import EXAMPLES, app_dir, base_cv_stem, cv_out_dir
from jobbot.sources import Job


@pytest.fixture
def fixed(isolated, monkeypatch):
    profile = isolated / "profile"
    shutil.copytree(EXAMPLES / "profile", profile)
    tg = yaml.safe_load((profile / "targets.yaml").read_text(encoding="utf-8"))
    (profile / "targets.yaml").write_text(yaml.safe_dump(tg | {"cv": "fixed"}), encoding="utf-8")
    monkeypatch.setenv("JOBBOT_PROFILE", str(profile))
    pdf = cv_out_dir() / f"{base_cv_stem()}.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 the one CV")
    con = db.connect()
    for n, status in enumerate(["new", "packed"]):
        db.upsert(con, Job(source="greenhouse", company=f"Acme{n}", title="ML Engineer", location="Remote - LATAM",
                           url="u", apply_url="u", description="d", external_id=str(n), remote=True), 9.0, [], [])
    ids = [r["id"] for r in con.execute("SELECT id FROM jobs ORDER BY company")]
    con.execute("UPDATE jobs SET verdict = 'verified'")
    con.execute("UPDATE jobs SET status = 'packed' WHERE id = ?", (ids[1],))
    con.commit()
    db.enqueue(con, "tailor", ids[1])
    return ids, pdf


def test_verified_jobs_go_straight_to_ready(fixed):
    ids, _ = fixed
    pipeline.prepare_all()
    con = db.connect()
    assert {r["status"] for r in con.execute("SELECT status FROM jobs")} == {"ready"}
    assert [r["status"] for r in con.execute("SELECT status FROM tasks WHERE kind = 'tailor'")] == ["cancelled"]
    assert not (app_dir(ids[0]) / "cv.yaml").exists()


def test_the_fixed_pdf_is_what_gets_sent(fixed):
    ids, pdf = fixed
    assert pipeline.verify_job(ids[0])["ok"]
    sent = pipeline.cv_to_send(ids[0])
    assert sent == app_dir(ids[0]) / "cv.pdf" and sent.read_bytes() == pdf.read_bytes()
    pdf.unlink()
    assert not pipeline.verify_job(ids[0])["ok"]


def test_nothing_to_draft(fixed):
    from jobbot.drafter import draft

    with pytest.raises(SystemExit, match="cv: fixed"):
        draft(fixed[0][0])
