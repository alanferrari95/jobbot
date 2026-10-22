"""Where data and profile live, and the tracker's migrations."""

from __future__ import annotations

import sqlite3

from jobbot import config, db


def test_profile_falls_back_to_the_example(isolated, monkeypatch):
    monkeypatch.delenv("JOBBOT_PROFILE")
    assert config.profile_dir() == config.EXAMPLES / "profile"
    own = isolated / "home" / "profile"
    own.mkdir(parents=True)
    assert config.profile_dir() == own  # a real profile in the data folder wins


def test_paths_follow_the_home(isolated):
    assert config.db_path() == isolated / "home" / "applications" / "jobs.db"
    assert config.cv_out_dir() == isolated / "home" / "cv"


def test_base_cv_stem(base):
    assert config.base_cv_stem(base) == "Avery Quinn - Machine Learning Engineer"
    assert config.base_cv_stem({"contact": {"name": "Sam"}}) == "Sam - CV"


def test_old_spanish_verdicts_are_migrated(isolated):
    path = config.db_path()
    path.parent.mkdir(parents=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, company TEXT, title TEXT, status TEXT, verdict TEXT, dedup_key TEXT)")
    con.executemany("INSERT INTO jobs VALUES (?, 'Acme', 'ML Engineer (Remote)', 'new', ?, 'acme|ml engineer remote')",
                    [("a", "verificada"), ("b", "ok_alan"), ("c", None)])
    con.commit()
    con.close()
    rows = dict(db.connect().execute("SELECT id, verdict FROM jobs").fetchall())
    assert rows == {"a": "verified", "b": "ok_manual", "c": None}
    keys = {r[0] for r in db.connect().execute("SELECT dedup_key FROM jobs")}
    assert keys == {"acme|ml engineer"}  # recomputed with the fixed regex
    cols = {r[1] for r in db.connect().execute("PRAGMA table_info(jobs)")}
    assert {"dedup_key", "verdict_reasons", "verified_at"} <= cols


def test_new_database_has_every_column():
    cols = {r[1] for r in db.connect().execute("PRAGMA table_info(jobs)")}
    assert {"verdict", "verdict_reasons", "verified_at", "dedup_key"} <= cols


def test_dedup_key_ignores_remote_markers():
    assert db.dedup_key("Acme", "ML Engineer (Remote, LATAM)") == db.dedup_key("acme", "ML Engineer")
