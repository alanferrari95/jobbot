"""cv.resolve (a tailoring spec over the base CV) and resolve.find_on_ats (aggregator -> company board)."""

from __future__ import annotations

import pytest

from jobbot import resolve as ats_resolve
from jobbot.cv import all_bullets, resolve


def bullet_ids(cv, role_id):
    role = next(r for r in cv["experience"] if r["id"] == role_id)
    return [b["id"] for g in role["groups"] for b in g["bullets"]]


def test_no_spec_returns_the_base(base):
    cv = resolve(base)
    assert cv["title"] == base["title"]
    assert bullet_ids(cv, "acme") == bullet_ids(base, "acme")
    assert cv is not base  # deep copy: the base is never modified


def test_reorders_and_drops_bullets_keeping_their_groups(base):
    cv = resolve(base, {"experience": {"acme": ["acme-rag", "acme-claims-cost"]}})
    acme = next(r for r in cv["experience"] if r["id"] == "acme")
    assert [g["client"] for g in acme["groups"]] == ["Adventure Works (retail)", "Litware Insurance (insurance)"]
    assert bullet_ids(cv, "acme") == ["acme-rag", "acme-claims-cost"]
    assert bullet_ids(cv, "globex") == bullet_ids(base, "globex")  # roles not in the spec keep every bullet


def test_bullet_text_always_comes_from_the_base(base):
    cv = resolve(base, {"experience": {"acme": ["acme-rag"]}})
    assert cv["experience"][0]["groups"][0]["bullets"][0]["text"] == all_bullets(base)["acme-rag"]


def test_empty_list_drops_the_role_bullets(base):
    cv = resolve(base, {"experience": {"initech": []}})
    assert bullet_ids(cv, "initech") == []


def test_unknown_bullet_id_raises(base):
    with pytest.raises(KeyError, match="unknown bullet id 'made-up'"):
        resolve(base, {"experience": {"acme": ["made-up"]}})


def test_bullet_from_another_role_raises(base):
    with pytest.raises(KeyError):
        resolve(base, {"experience": {"acme": ["globex-churn"]}})


def test_project_bullets_reorder_and_unknown_raises(base):
    cv = resolve(base, {"project": ["proj-eval-gcp"]})
    assert [b["id"] for b in cv["projects"][0]["bullets"]] == ["proj-eval-gcp"]
    with pytest.raises(KeyError, match="unknown project bullet id"):
        resolve(base, {"project": ["proj-nope"]})


def test_title_summary_and_skills_are_replaced(base):
    skills = [{"label": "Core", "items": ["Python"]}]
    cv = resolve(base, {"title": "ML Engineer", "summary": "Short.", "skills": skills})
    assert (cv["title"], cv["summary"], cv["skills"]) == ("ML Engineer", "Short.", skills)


# ------------------------------------------------------------------ find_on_ats

def test_tokens_strip_legal_suffixes():
    assert ats_resolve._tokens("Acme Labs Inc.") == ["acme", "acmelabsinc"]
    assert "northwind-ai" in ats_resolve._tokens("Northwind AI")


def test_find_on_ats_picks_the_closest_title(monkeypatch):
    boards = {"northwindai": [("Senior Machine Learning Engineer (Remote)", "https://example.com/gh/1"),
                              ("Data Engineer", "https://example.com/gh/2")]}
    monkeypatch.setattr(ats_resolve, "_boards", lambda token: iter(boards.get(token, [])))
    assert ats_resolve.find_on_ats("Northwind AI", "Senior Machine Learning Engineer - Remote") == "https://example.com/gh/1"
    assert ats_resolve.find_on_ats("Northwind AI", "Head of Sales") is None
    assert ats_resolve.find_on_ats("Unknown Co", "Senior Machine Learning Engineer") is None
