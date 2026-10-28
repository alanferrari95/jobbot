"""Screening layer text checks and the application-form gate (no browser, no network)."""

from __future__ import annotations

from jobbot.config import targets
from jobbot.formgate import check
from jobbot.screening import is_duplicate, text_checks
from jobbot.sources import Job

LONG = " We build LLM systems in Python and evaluate them carefully against labeled data." * 4


def job(title="Machine Learning Engineer", description=LONG, company="Acme", location="Remote - LATAM"):
    return Job(source="greenhouse", company=company, title=title, location=location, url="u", apply_url="u",
               description=description, external_id="1", remote=True)


def test_clean_posting_passes(base):
    assert text_checks(job(), targets(), base["never_claim"]) == []


def test_foreign_stack(base):
    reasons = text_checks(job(description=LONG + " Required: TypeScript and React in production."), targets(),
                          base["never_claim"])
    assert reasons == ["requires stack you haven't used: TypeScript, React"]


def test_region_in_the_title(base):
    assert text_checks(job(title="ML Engineer (EMEA)"), targets(), base["never_claim"]) == \
        ["title restricts the region: (EMEA)"]


def test_gig_platform_and_short_posting(base):
    assert text_checks(job(company="Mindrift"), targets(), base["never_claim"]) == ["gig/task platform"]
    assert text_checks(job(description="Apply now."), targets(), base["never_claim"]) == \
        ["posting has no description (closed or broken)"]


def test_duplicates_across_sources():
    row = {"id": "a", "company": "Northwind, Inc.", "title": "Senior ML Engineer - Remote", "status": "new"}
    other = {"id": "b", "company": "northwind.ai", "title": "Senior ML Engineer", "status": "applied"}
    assert is_duplicate(row, [other]) == "duplicate of northwind.ai · Senior ML Engineer (applied)"
    assert is_duplicate(row, [other | {"title": "Data Engineer"}]) is None


def test_form_gate_flags_required_questions_only():
    fields = [
        {"label": "Are you a citizen of India?", "required": True},
        {"label": "Expected salary in EUR", "required": False},
    ]
    assert check(fields) == ['asks for citizenship of another country: "Are you a citizen of India?"']


def test_form_gate_accepts_plain_sponsorship_questions():
    fields = [{"label": "Are you authorized to work in the US?", "required": True,
               "options": ["Yes", "No, I will require sponsorship"]}]
    assert check(fields) == []


def test_unverified_jobs_are_checked_again_after_a_week(monkeypatch):
    from jobbot import db, pipeline, screening

    con = db.connect()
    for n in range(5):
        db.upsert(con, job(company=f"Co{n}"), 8.0 - n * 0.1, [], [])
    ids = [r["id"] for r in con.execute("SELECT id FROM jobs ORDER BY score DESC")]
    old, recent = "2026-01-01T00:00:00+00:00", db.now()
    for jid, verdict, at in [(ids[1], "unverified", old), (ids[2], "unverified", recent),
                             (ids[3], "location_unclear", old), (ids[4], "verified", old)]:
        con.execute("UPDATE jobs SET verdict = ?, verified_at = ? WHERE id = ?", (verdict, at, jid))
    con.commit()
    seen = []

    def fake_verify_rows(rows, all_rows, deep=True, log=print):
        seen.append([r["id"] for r in rows])
        return {r["id"]: ("verified", ["ok"]) for r in rows}

    monkeypatch.setattr(screening, "verify_rows", fake_verify_rows)
    pipeline.verify_candidates(min_score=0, limit=40)
    assert seen[0] == [ids[0], ids[1]]  # never screened first, then the week-old unverified one
