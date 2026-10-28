"""Hard filters (title, location, language, eligibility, stack, salary) and the 0-10 score."""

from __future__ import annotations

import pytest

from jobbot.config import targets
from jobbot.match import (
    _stage1,
    evaluate_many,
    gates,
    location_status,
    missing_required,
    salary_usd_year,
    text_salary_max_usd_year,
)
from jobbot.sources import Job


def job(title="Senior Machine Learning Engineer", location="Remote - LATAM", description="", remote=True, **kw):
    return Job(source=kw.pop("source", "greenhouse"), company=kw.pop("company", "Acme"), title=title, location=location,
               url="https://example.com/j", apply_url="https://example.com/j/apply", description=description,
               external_id=kw.pop("external_id", "1"), remote=remote, **kw)


@pytest.fixture
def tg():
    return targets()


@pytest.mark.parametrize("location, description, expected", [
    ("Remote - LATAM", "", "ok"),
    ("Remote (Worldwide)", "", "ok"),
    ("Montevideo, Uruguay (Remote)", "", "ok"),
    ("Remote - US", "", "no"),
    ("Remote", "This role is US-only.", "no"),
    ("Remote", "Work Environment: Fully remote, U.S.-based.", "no"),
    ("Remote (Germany)", "", "no"),
    ("Remote", "We hire people located in Latin America.", "ok"),
    ("Remote", "A great team.", "unclear"),
])
def test_location_status(tg, location, description, expected):
    assert location_status(job(location=location, description=description), tg)[0] == expected


def test_onsite_is_not_remote(tg):
    assert location_status(job(location="Berlin, Germany", remote=False), tg) == ("no", "not remote")


def test_home_region_is_configurable(tg):
    posting = job(location="Remote", description="Candidates based in Uruguay are welcome.")
    assert location_status(posting, tg)[0] == "ok"
    assert location_status(posting, tg | {"home_region": "kenya"})[0] == "unclear"


@pytest.mark.parametrize("text, gate", [
    ("Fluent German is required.", "requires a language you don't speak"),
    ("You must be authorized to work in the US.", "eligibility"),
    ("Must be based in Canada.", "eligibility"),
    ("Security clearance required.", "eligibility"),
])
def test_hard_gates(tg, text, gate):
    reason, _, _ = gates(job(description=text), tg)
    assert reason and reason.startswith(gate)


def test_eligibility_sentence_that_opens_the_role_to_us_passes(tg):
    reason, _, _ = gates(job(description="You must be based in the US or anywhere in Latin America."), tg)
    assert reason is None


def test_payment_currency_adjusts_the_score(tg):
    _, notes, adj = gates(job(description="Contractor role, paid in USD monthly."), tg)
    assert notes == ["pays in USD"] and adj > 0
    _, notes, adj = gates(job(description="El salario se paga en pesos argentinos."), tg)
    assert notes == ["pays in local currency"] and adj < 0


def test_salary_from_text_and_structured_data():
    assert text_salary_max_usd_year("Compensation: USD 2,000 - 2,500 / month") == 30000
    assert text_salary_max_usd_year("$40 - $55 per hour") == 110000
    assert text_salary_max_usd_year("no numbers here") is None
    ashby = job(source="ashby", extra={"compensation": "$120K – $150K"})
    assert salary_usd_year(ashby) == 150000
    gob = job(source="getonboard", extra={"salary": [3000, 4000]})
    assert salary_usd_year(gob) == 48000


def test_annual_range_without_a_period():
    assert text_salary_max_usd_year("The pay range is base salary of $200,000 –$250,000 USD.") == 250000
    assert text_salary_max_usd_year("Salary Range: $130K–$200K, depending on experience") == 200000
    assert text_salary_max_usd_year("Series A: raised $5 - $10 to build it") is None  # too small to be a salary
    assert text_salary_max_usd_year("You will close deals between $250K-$2M+.") is None  # not pay
    assert text_salary_max_usd_year("Competitive salary ($140K-$180K CAN) and stock") is None  # not USD


def test_posting_text_beats_an_inflated_feed():
    text = "The national pay range for this full-time position is base salary of $130,000 –$160,000 USD."
    inflated = job(source="himalayas", description=text,
                   extra={"salary": [220000, 300000], "salary_period": "annual", "currency": "USD"})
    assert salary_usd_year(inflated) == 160000
    close = job(source="himalayas", description=text,
                extra={"salary": [140000, 170000], "salary_period": "annual", "currency": "USD"})
    assert salary_usd_year(close) == 170000  # within 30%: the structured field stands
    out_of_range = job(source="himalayas", description="",
                       extra={"salary": [300000, 700000], "salary_period": "annual", "currency": "USD"})
    assert salary_usd_year(out_of_range) is None


def test_missing_required_ignores_alternatives_and_nice_to_haves():
    never = ["AWS", "Kubernetes", "Spark"]
    assert missing_required("Hands-on experience with Kubernetes and AWS SageMaker.", never) == ["AWS", "Kubernetes"]
    assert missing_required("Experience with GCP, AWS or Azure.", never) == []
    assert missing_required("Spark is a nice to have.", never) == []


@pytest.mark.parametrize("title, description, reason", [
    ("Junior Data Scientist", "", "title excluded"),
    ("Data Engineer", "", "title excluded"),
    ("Staff Machine Learning Engineer", "", "seniority/research role"),
    ("Account Executive", "", "title excluded"),
    ("Machine Learning Engineer", "Kubernetes, AWS and Spark are required.", "asks for 3 techs"),
    ("Machine Learning Engineer", "Pay: USD 1,500 - 2,000 / month.", "pays at most"),
])
def test_stage1_rejections(tg, base, title, description, reason):
    ok, reasons, _, _ = _stage1(job(title=title, description=description), tg, base["never_claim"])
    assert not ok and reasons[0].startswith(reason)


def test_lead_title_allowed_when_it_is_a_first_leadership_step(tg, base):
    lead = job(title="ML Engineer, Team Lead", description="Your first leadership role, with 2 engineers.")
    assert _stage1(lead, tg, base["never_claim"])[0]


def test_score_ranks_a_fitting_posting_above_a_weak_one(tg, base):
    strong = job(description="Build RAG and LLM evaluation systems in Python with structured outputs on GCP. "
                             "Evaluate extraction with precision and recall against a gold set. Deploy with "
                             "FastAPI and Docker. Paid in USD, remote from LATAM.")
    weak = job(title="Machine Learning Engineer", external_id="2",
               description="Optimize ad bidding auctions for our marketing platform with real-time pipelines.")
    (keep_s, score_s, reasons, _), (_, score_w, _, _) = evaluate_many([strong, weak], tg, base)
    assert score_s > score_w
    assert any(r.startswith("CV fit") for r in reasons) and "pays in USD" in reasons


def test_salary_adds_to_the_score_by_tier(tg, base):
    text = "Build RAG systems in Python on GCP. Salary: USD {} - {} per year."
    posts = [job(description=text.format(lo, hi), external_id=str(hi)) for lo, hi in
             [(55000, 60000), (70000, 75000), (85000, 90000)]]
    tiers = dict(tg, salary_floor_usd_year=50000, salary_target_usd_year=72000, salary_ideal_usd_year=84000)
    low, mid, high = (s for _, s, _, _ in evaluate_many(posts, tiers, base))
    assert round(mid - low, 2) == 0.5 and round(high - mid, 2) == 0.5
