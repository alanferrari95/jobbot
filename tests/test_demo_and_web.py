"""End to end on the offline demo (recorded boards -> rank -> pack -> agent output -> gate), plus the dashboard API."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jobbot import db
from jobbot.config import EXAMPLES


@pytest.fixture
def demo_home(isolated, monkeypatch):
    monkeypatch.setenv("JOBBOT_HOME", str(isolated / ".demo"))
    monkeypatch.setenv("JOBBOT_OFFLINE", "1")
    from jobbot.demo import run
    lines = []
    run(log=lines.append)
    return "\n".join(lines)


def test_demo_runs_the_whole_loop(demo_home):
    out = demo_home
    assert "15 recorded postings" in out
    assert "dropped  Contoso Labs     MLOps Engineer" in out          # never_claim stack
    assert "pays at most ~USD 30,000/year" in out                     # salary floor
    assert out.count("**Result: PASS**") == 1 and out.count("**Result: FAIL**") == 1
    con = db.connect()
    statuses = dict(con.execute("SELECT id, status FROM jobs").fetchall())
    assert statuses["greenhouse-northwind-ai-4001001"] == "ready"      # passed the gate
    assert statuses["lever-contoso-labs-c0a1e5d2-0001"] == "packed"    # failed it: never becomes ready
    assert statuses["ashby-fabrikam-health-f4b1-0001"] == "applied"
    assert statuses["ashby-fabrikam-health-f4b1-0004"] == "ready"      # drafted locally, no LLM, passed the gate


def test_demo_refuses_to_write_outside_a_demo_folder(isolated):
    from jobbot.demo import run
    with pytest.raises(SystemExit):
        run()


def test_dashboard_api(demo_home):
    from jobbot.web import app

    client = TestClient(app)
    assert "<title>jobbot</title>" in client.get("/").text
    summary = client.get("/api/summary").json()
    assert summary["jobs"]["ready"] == 2 and summary["jobs"]["applied"] == 1
    assert len(summary["needs_confirm"]) == 2  # salary and start date wait for the candidate
    jobs = client.get("/api/jobs", params={"status": "ready"}).json()
    assert {j["id"] for j in jobs} == {"greenhouse-northwind-ai-4001001", "ashby-fabrikam-health-f4b1-0004"}
    detail = client.get("/api/jobs/greenhouse-northwind-ai-4001001").json()
    assert detail["files"]["cv.pdf"] and "**Result: PASS**" in detail["verify"]
    assert client.get("/files/greenhouse-northwind-ai-4001001/cv.pdf").headers["content-type"] == "application/pdf"
    assert client.post("/api/jobs/ashby-fabrikam-health-f4b1-0004/ok").json() == {"ok": True}
    jobs = client.get("/api/jobs", params={"status": "ready"}).json()
    assert {j["id"]: j["verdict"] for j in jobs}["ashby-fabrikam-health-f4b1-0004"] == "ok_manual"


def test_example_profile_is_complete():
    names = {p.name for p in (EXAMPLES / "profile").iterdir()}
    assert {"base_cv.yaml", "answers.yaml", "targets.yaml", "companies.yaml"} <= names
