"""Every test runs offline, on the fictional example profile, with its data in a temporary folder."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import requests
import yaml

from jobbot.config import EXAMPLES, app_dir

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Data in tmp, the example profile, the hashing embedder, the built-in PDF renderer, and no network."""
    monkeypatch.setenv("JOBBOT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JOBBOT_PROFILE", str(EXAMPLES / "profile"))
    monkeypatch.setenv("JOBBOT_EMBEDDER", "hash")
    monkeypatch.setenv("JOBBOT_PDF", "basic")
    monkeypatch.delenv("JOBBOT_OFFLINE", raising=False)

    def no_network(*args, **kwargs):
        raise AssertionError("tests must not touch the network")

    monkeypatch.setattr(requests, "get", no_network)
    monkeypatch.setattr(requests, "post", no_network)
    monkeypatch.setattr(requests.Session, "request", no_network)
    return tmp_path


@pytest.fixture
def base():
    return yaml.safe_load((EXAMPLES / "profile" / "base_cv.yaml").read_text(encoding="utf-8"))


@pytest.fixture
def profile_answers():
    return yaml.safe_load((EXAMPLES / "profile" / "answers.yaml").read_text(encoding="utf-8"))


def fixture_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def demo_app():
    """Copy one recorded agent output into applications/<id>/ and return that folder."""
    def make(job_id: str) -> Path:
        d = app_dir(job_id)
        shutil.copytree(EXAMPLES / "demo" / "agent_output" / job_id, d)
        return d
    return make
