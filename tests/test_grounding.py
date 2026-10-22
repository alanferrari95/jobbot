"""The grounding gate: anything the agent writes that the profile doesn't back must fail verify."""

from __future__ import annotations

import os
import time

import pymupdf
import pytest
import yaml

from jobbot.config import app_dir
from jobbot.cv import render, resolve
from jobbot.grounding import _numbers, check_text, profile_text, report_md, verify
from jobbot.pipeline import spec_template

JOB = "greenhouse-acme-1"
POSTING = "We need 5+ years of Python, RAG and evaluation. Our team serves 2,000 clinics."


@pytest.fixture
def app(base):
    """applications/<JOB>/ with the untouched spec template (a valid tailoring) and its rendered PDF."""
    d = app_dir(JOB)
    d.mkdir(parents=True)
    (d / "cv.yaml").write_text(spec_template(base, JOB), encoding="utf-8")
    return d


def tailor(d, base, **changes):
    """Edit cv.yaml like the agent would, then render the PDF from it."""
    spec = yaml.safe_load((d / "cv.yaml").read_text(encoding="utf-8"))
    spec.update(changes)
    (d / "cv.yaml").write_text(yaml.safe_dump(spec, allow_unicode=True, sort_keys=False), encoding="utf-8")
    render(resolve(base, spec), d, "cv")
    return spec


def run(d, base, answers=None):
    return verify(d, base, POSTING, answers)


def test_untouched_template_passes(app, base, profile_answers):
    tailor(app, base)
    result = run(app, base, profile_answers)
    assert result["ok"], result["errors"]
    assert result["pages"] <= 2


def test_reordered_and_dropped_bullets_pass(app, base):
    tailor(app, base, experience={"acme": ["acme-rag", "acme-rag-eval", "acme-claims-extract", "acme-claims-eval"],
                                  "globex": ["globex-api"], "initech": []})
    assert run(app, base)["ok"]


def test_unknown_bullet_id_fails(app, base):
    spec = yaml.safe_load((app / "cv.yaml").read_text(encoding="utf-8"))
    spec["experience"]["acme"].append("acme-invented-bullet")
    (app / "cv.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")
    result = run(app, base)
    assert not result["ok"]
    assert "unknown bullet id 'acme-invented-bullet'" in result["errors"][0]


def test_invented_number_in_summary_fails(app, base):
    tailor(app, base, summary=base["summary"] + " I cut latency by 73%.")
    result = run(app, base)
    assert not result["ok"]
    assert any("number '73'" in e for e in result["errors"])


def test_number_from_profile_passes_with_other_separators(app, base):
    # The profile says "12,000 help-center articles"; "12000" and "12.000" are the same number.
    tailor(app, base, summary=base["summary"] + " Built a RAG assistant over 12000 articles.")
    assert run(app, base)["ok"]


def test_never_claim_technology_fails_in_summary_and_pdf(app, base):
    tailor(app, base, summary=base["summary"] + " Deployed everything on Kubernetes.")
    errors = run(app, base)["errors"]
    assert any(e.startswith("title/summary: mentions Kubernetes") for e in errors)
    assert any(e.startswith("PDF mentions Kubernetes") for e in errors)


def test_technology_missing_from_profile_fails(app, base):
    tailor(app, base, title="Senior ML Engineer · LangChain agents")
    errors = run(app, base)["errors"]
    assert errors == ["title/summary: mentions LangChain, which the profile doesn't mention"]


def test_technology_in_profile_passes(app, base):
    tailor(app, base, title="Senior ML Engineer · PyTorch, FastAPI and Terraform on GCP")
    assert run(app, base)["ok"]


def test_skill_item_not_copied_from_profile_fails(app, base):
    skills = [{"label": "AI engineering", "items": ["RAG", "Vector databases"]}]
    tailor(app, base, skills=skills)
    assert "skill not in base_cv.yaml: 'Vector databases'" in run(app, base)["errors"]


def test_answers_are_checked_like_the_cv(app, base, profile_answers):
    tailor(app, base)
    (app / "answers.yaml").write_text(yaml.safe_dump({"labels": {
        "Why us?": "You serve 2,000 clinics and I have 6 years of experience with Python and RAG.",  # posting + profile
        "Biggest win?": "I cut cloud costs by 45% with Kubernetes and Snowflake.",                  # all invented
    }}), encoding="utf-8")
    errors = run(app, base, profile_answers)["errors"]
    assert not [e for e in errors if "Why us?" in e]
    assert "answers.yaml [Biggest win?]: number '45' does not appear in the profile" in errors
    assert any("[Biggest win?]: mentions Kubernetes, which is in never_claim" in e for e in errors)
    assert any("[Biggest win?]: mentions Snowflake, which the profile doesn't mention" in e for e in errors)


def test_profile_answers_ground_numbers(app, base, profile_answers):
    tailor(app, base)  # 54000 is in the standard answers (salary floor), not in the CV
    (app / "answers.yaml").write_text(yaml.safe_dump({"labels": {"Anything else?": "My floor is 54,000 a year."}}),
                                      encoding="utf-8")
    assert run(app, base, profile_answers)["ok"]
    assert not run(app, base)["ok"]


def test_pay_expectation_is_a_warning_not_an_error(app, base, profile_answers):
    tailor(app, base)
    (app / "answers.yaml").write_text(yaml.safe_dump({"labels": {"Expected salary (USD/month)": "6200, paid in USD"}}),
                                      encoding="utf-8")
    result = run(app, base, profile_answers)
    assert result["ok"]
    assert any("pay expectation 6200" in w for w in result["warnings"])


def test_cover_letter_is_checked(app, base):
    tailor(app, base)
    (app / "cover_letter.md").write_text("I led a team of 12 engineers using Airflow.", encoding="utf-8")
    errors = run(app, base)["errors"]
    assert "cover_letter.md: number '12' does not appear in the profile" in errors
    assert any(e.startswith("cover_letter.md: mentions Airflow") for e in errors)


def test_missing_pdf_fails(app, base):
    assert run(app, base)["errors"] == ["cv.pdf missing: run `jobbot render <id>`"]


def test_stale_pdf_fails(app, base):
    tailor(app, base)
    later = time.time() + 5
    os.utime(app / "cv.yaml", (later, later))
    assert "cv.pdf is older than cv.yaml: run `jobbot render <id>` again" in run(app, base)["errors"]


def test_pdf_must_be_readable_and_complete(app, base):
    doc = pymupdf.open()
    for _ in range(3):
        doc.new_page().insert_text((72, 72), "SUMMARY only, no contact details")
    doc.save(app / "cv.pdf")
    errors = run(app, base)["errors"]
    assert "CV has 3 pages; keep it to 2" in errors
    assert "almost no text extracted from the PDF (would fail an ATS)" in errors
    assert "email not found in PDF text" in errors
    assert "section EXPERIENCE not found in PDF text" in errors


def test_numbers_glued_to_names_are_not_claims():
    assert _numbers("K8s, YOLOv11, GPT4 and Python3") == set()
    assert _numbers("6,000 and 6.000 and 1.5x and 92%") == {"6000", "1.5", "92"}


def test_check_text_reports_each_problem_once(base):
    grounded = profile_text(base)
    errors = check_text("answer", "Used Spark and Ray for 3 years", base, grounded)
    assert errors == ["answer: mentions Spark, which is in never_claim (not used by the candidate)",
                      "answer: mentions Ray, which the profile doesn't mention"]


def test_report_md(app, base):
    tailor(app, base, summary=base["summary"] + " 99% uptime.")
    md = report_md(JOB, run(app, base))
    assert "**Result: FAIL**" in md and "number '99'" in md and "## Keywords" in md


def test_demo_agent_outputs(demo_app, base, profile_answers):
    """The recorded agent outputs used in the README demo: one grounded, one with three hallucinations."""
    from jobbot.config import base_cv

    for jid, ok in [("greenhouse-northwind-ai-4001001", True), ("lever-contoso-labs-c0a1e5d2-0001", False)]:
        d = demo_app(jid)
        render(resolve(base_cv(), yaml.safe_load((d / "cv.yaml").read_text(encoding="utf-8"))), d, "cv")
        result = verify(d, base, "Python, LLMs, 5+ years.", profile_answers)
        assert result["ok"] is ok, result["errors"]
    errors = result["errors"]
    assert any("number '75'" in e for e in errors)
    assert any("mentions Kubernetes" in e for e in errors)
    assert any("mentions LangChain" in e for e in errors)
    assert any("number '8'" in e for e in errors)
