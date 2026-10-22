"""ATS parsers on recorded API responses (trimmed real Greenhouse, Lever and Ashby boards), no network."""

from __future__ import annotations

from conftest import fixture_json

from jobbot import sources
from jobbot.sources import html_to_text, parse_ashby, parse_greenhouse, parse_lever, recorded


def test_greenhouse():
    data = fixture_json("greenhouse_gitlab.json")
    jobs = parse_greenhouse(data, "gitlab", "GitLab")
    assert len(jobs) == len(data["jobs"]) == 2
    j, raw = jobs[0], data["jobs"][0]
    assert j.id == f"greenhouse-gitlab-{raw['id']}"
    assert (j.source, j.company, j.title) == ("greenhouse", "GitLab", raw["title"])
    assert j.location == raw["location"]["name"] and j.remote == ("remote" in j.location.lower())
    assert j.apply_url == f"https://job-boards.greenhouse.io/gitlab/jobs/{raw['id']}"
    assert j.description and "<" not in j.description and "&lt;" not in j.description  # HTML escaped twice upstream


def test_lever():
    data = fixture_json("lever_outreach.json")
    jobs = parse_lever(data, "outreach", "Outreach")
    assert [j.title for j in jobs] == [x["text"] for x in data]
    j, raw = jobs[1], data[1]
    assert j.id == f"lever-outreach-{raw['id']}"
    assert j.remote is (raw["workplaceType"] == "remote")
    assert j.apply_url == raw["applyUrl"] and j.extra["commitment"] == raw["categories"]["commitment"]
    for item in raw["lists"]:  # list sections are appended to the description as plain text
        assert item["text"].strip() in j.description


def test_ashby():
    data = fixture_json("ashby_airbyte.json")
    jobs = parse_ashby(data, "airbyte", "Airbyte")
    assert len(jobs) == 2
    j, raw = jobs[0], data["jobs"][0]
    assert j.id == f"ashby-airbyte-{raw['id']}"
    assert j.remote is bool(raw["isRemote"])
    assert j.extra["compensation"] == raw["compensation"]["compensationTierSummary"]
    assert j.description.startswith(raw["descriptionPlain"][:40])


def test_unlisted_ashby_jobs_are_skipped():
    data = fixture_json("ashby_airbyte.json")
    data["jobs"][0]["isListed"] = False
    assert len(parse_ashby(data, "airbyte", "Airbyte")) == 1


def test_missing_board_returns_no_jobs():
    assert parse_greenhouse(None, "x", "X") == parse_lever(None, "x", "X") == parse_ashby(None, "x", "X") == []


def test_fetchers_parse_what_the_api_returns(monkeypatch):
    monkeypatch.setattr(sources, "_get", lambda url, **params: fixture_json("greenhouse_gitlab.json"))
    assert len(sources.greenhouse("gitlab", "GitLab")) == 2


def test_recorded_demo_boards():
    assert {j.source for j in recorded("greenhouse", "northwind", "Northwind AI")} == {"greenhouse"}
    assert len(recorded("lever", "contoso-labs", "Contoso Labs")) == 4
    assert recorded("ashby", "no-such-board", "Nobody") == []


def test_html_to_text():
    assert html_to_text("<p>Hello</p><ul><li>one</li><li>two &amp; three</li></ul>") == "Hello\n• one\n• two & three"


def test_local_sources_plugin(isolated, monkeypatch):
    """Sources kept out of the repo: profile/local_sources.py defining SOURCES, loaded next to the built-in ones."""
    import shutil

    from jobbot.config import EXAMPLES
    from jobbot.pipeline import enabled_sources

    profile = isolated / "profile"
    shutil.copytree(EXAMPLES / "profile", profile)
    monkeypatch.setenv("JOBBOT_PROFILE", str(profile))
    (profile / "local_sources.py").write_text("SOURCES = {'mine': lambda: []}\n", encoding="utf-8")
    assert "mine" in enabled_sources({})
    assert "mine" not in enabled_sources({"sources": {"mine": False}})
    assert "remotive" in enabled_sources({}) and "indeed" not in enabled_sources({})
