"""Where jobbot reads the profile and keeps its data.

- home (JOBBOT_HOME, default: the repo root): applications/ (tracker DB + one folder per job), cv/, logs/,
  .browser-profile/ and .models/ live here. None of it is committed.
- profile (JOBBOT_PROFILE, default: <home>/profile if it exists, else examples/profile): the candidate's
  facts and rules. The repo ships a fictional profile in examples/profile.

Paths are resolved on every call, so tests and `--home`/`--demo` can redirect them at runtime.
"""

import os
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"


def home() -> Path:
    return Path(os.environ.get("JOBBOT_HOME") or REPO)


def profile_dir() -> Path:
    if os.environ.get("JOBBOT_PROFILE"):
        return Path(os.environ["JOBBOT_PROFILE"])
    own = home() / "profile"
    return own if own.exists() else EXAMPLES / "profile"


def apps_dir() -> Path:
    return home() / "applications"


def db_path() -> Path:
    return apps_dir() / "jobs.db"


def cv_out_dir() -> Path:
    return home() / "cv"


def browser_profile_dir() -> Path:
    """Persistent Chrome profile for autofill (keeps the logins you make with `jobbot login`)."""
    return home() / ".browser-profile"


def models_dir() -> Path:
    return home() / ".models"


def offline() -> bool:
    """Demo/offline mode: job boards are read from recorded files instead of the network."""
    return os.environ.get("JOBBOT_OFFLINE") == "1"


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def base_cv() -> dict:
    return load_yaml(profile_dir() / "base_cv.yaml")


def answers() -> dict:
    return load_yaml(profile_dir() / "answers.yaml")


def targets() -> dict:
    return load_yaml(profile_dir() / "targets.yaml")


def base_cv_stem(base: dict | None = None) -> str:
    """File name (without extension) of the general CV: base_cv.yaml `output_stem`, else '<name> - CV'."""
    base = base or base_cv()
    return base.get("output_stem") or f"{base['contact']['name']} - CV"


def companies() -> list[dict]:
    """Hand-picked boards plus the ones found by `jobbot discover-boards` (offline: the demo's recorded boards)."""
    if offline():
        return load_yaml(EXAMPLES / "demo" / "companies.yaml").get("companies", [])
    out = load_yaml(profile_dir() / "companies.yaml").get("companies", [])
    auto = profile_dir() / "companies_auto.yaml"
    if auto.exists():
        seen = {(c["ats"], c["token"].lower()) for c in out}
        out += [c for c in load_yaml(auto).get("companies", []) if (c["ats"], c["token"].lower()) not in seen]
    return out


def app_dir(job_id: str) -> Path:
    return apps_dir() / job_id
