"""Recruiter leads: LinkedIn post-search links and contact extraction."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from jobbot import leads


def test_post_searches_come_from_the_profile():
    searches = leads.linkedin_post_searches()
    assert [s["q"] for s in searches][0] == 'hiring "machine learning engineer" remote (LATAM OR Uruguay)'
    params = parse_qs(urlparse(searches[0]["url"]).query)
    assert params["keywords"] == ['hiring "machine learning engineer" remote (LATAM OR Uruguay)']
    assert params["datePosted"] == ['"past-week"'] and params["sortBy"] == ['"date_posted"']


def test_post_searches_fall_back_to_generic(monkeypatch):
    monkeypatch.setattr("jobbot.config.targets", lambda: {})
    assert [s["q"] for s in leads.linkedin_post_searches()] == leads.POST_SEARCHES


def test_extract_finds_email_and_how_to_apply():
    lead = leads.extract("We're hiring an ML engineer, remote LATAM. Send your CV to jobs@acme.io")
    assert lead["emails"] == ["jobs@acme.io"] and "enviar CV por email" in lead["how"]
