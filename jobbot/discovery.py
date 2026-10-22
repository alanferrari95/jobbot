"""Discover more company job boards (Ashby / Greenhouse / Lever) from open company directories.

Directories used (both public, community-maintained):
- yc-oss/api: every Y Combinator company that is hiring (https://yc-oss.github.io/api/companies/hiring.json)
- remoteintech/remote-jobs: remote-friendly tech companies with the region they hire in

For each company we guess board tokens (slug, name, domain) and probe the public ATS APIs.
A board is kept only if it has open jobs and, when checkable, its postings mention the company.
Results are cached in profile/companies_auto.yaml; `jobbot discover` reads it next to companies.yaml.
"""

from __future__ import annotations

import concurrent.futures as cf
import io
import re
import time
import zipfile
from urllib.parse import urlparse

import requests
import yaml

from .config import load_yaml, profile_dir
from .sources import HEADERS, TIMEOUT


def auto_path():
    return profile_dir() / "companies_auto.yaml"

YC_URL = "https://yc-oss.github.io/api/companies/hiring.json"
RIT_ZIP = "https://codeload.github.com/remoteintech/remote-jobs/zip/refs/heads/main"
YC_REGIONS = {"Remote", "Fully Remote", "Partly Remote", "Latin America", "America / Canada"}
RIT_REGIONS = {"worldwide", "americas", "latin-america", "south-america", "north-america"}


def yc_companies() -> list[dict]:
    data = requests.get(YC_URL, headers=HEADERS, timeout=60).json()
    out = []
    for c in data:
        regions = set(c.get("regions") or [])
        if not regions & {"Remote", "Fully Remote", "Partly Remote", "Latin America"}:
            continue  # office-only companies won't hire a remote contractor abroad
        out.append({"name": c["name"], "slug": c.get("slug", ""), "website": c.get("website", ""),
                    "origin": "yc", "note": ", ".join(sorted(regions & YC_REGIONS))})
    return out


def remoteintech_companies() -> list[dict]:
    raw = requests.get(RIT_ZIP, headers=HEADERS, timeout=120).content
    out = []
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for name in z.namelist():
            if "/src/companies/" not in name or not name.endswith(".md"):
                continue
            text = z.read(name).decode("utf-8", "replace")
            m = re.match(r"---\n(.*?)\n---", text, re.S)
            if not m:
                continue
            try:
                meta = yaml.safe_load(m.group(1)) or {}
            except yaml.YAMLError:
                continue
            region = str(meta.get("region") or "").lower()
            if region and region not in RIT_REGIONS:
                continue
            out.append({"name": str(meta.get("title") or meta.get("slug")), "slug": str(meta.get("slug") or ""),
                        "website": meta.get("website") or "", "careers_url": meta.get("careers_url") or "",
                        "origin": "remoteintech", "note": region or "unspecified"})
    return out


ATS_URL = re.compile(r"(?:job-)?boards(?:-api)?\.greenhouse\.io/(?:v1/boards/)?([\w-]+)|jobs\.lever\.co/([\w-]+)|"
                     r"jobs\.ashbyhq\.com/([\w.-]+)", re.I)


def _candidates(c: dict) -> list[tuple[str | None, str]]:
    """(ats or None for 'any', token) guesses, most specific first."""
    out = []
    m = ATS_URL.search(c.get("careers_url") or "")
    if m:
        gh, lv, ab = m.groups()
        out.append(("greenhouse", gh) if gh else ("lever", lv) if lv else ("ashby", ab))
    name = c["name"].lower()
    host = urlparse(c.get("website") or "").netloc.lower().removeprefix("www.")
    for tok in (c.get("slug"), re.sub(r"[^a-z0-9]", "", name), re.sub(r"[^a-z0-9]+", "-", name).strip("-"),
                host.split(".")[0] if host else None):
        if tok and (None, tok) not in out:
            out.append((None, tok))
    return out


def _mentions(company: str, texts: list[str]) -> bool:
    key = re.sub(r"[^a-z0-9]", "", company.lower())
    return any(key and key in re.sub(r"[^a-z0-9]", "", t.lower()) for t in texts)


def _probe(ats: str, token: str, company: str) -> int:
    """Number of open jobs if this board exists and plausibly belongs to the company, else 0."""
    try:
        if ats == "ashby":
            r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{token}", headers=HEADERS, timeout=TIMEOUT)
            jobs = r.json().get("jobs", []) if r.ok else []
            texts = [j.get("descriptionPlain") or "" for j in jobs[:5]]
        elif ats == "greenhouse":
            r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{token}", headers=HEADERS, timeout=TIMEOUT)
            if not r.ok:
                return 0
            board_name = r.json().get("name", "")
            r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", headers=HEADERS, timeout=TIMEOUT)
            jobs = r.json().get("jobs", []) if r.ok else []
            texts = [board_name]
        else:
            r = requests.get(f"https://api.lever.co/v0/postings/{token}", params={"mode": "json"},
                             headers=HEADERS, timeout=TIMEOUT)
            jobs = r.json() if r.ok and isinstance(r.json(), list) else []
            texts = [(j.get("descriptionPlain") or "") + (j.get("additionalPlain") or "") for j in jobs[:5]]
    except (requests.RequestException, ValueError):
        return 0
    if not jobs:
        return 0
    return len(jobs) if _mentions(company, texts) else 0


def find_board(c: dict) -> dict | None:
    for ats, tok in _candidates(c):
        for a in ([ats] if ats else ["ashby", "greenhouse", "lever"]):
            n = _probe(a, tok, c["name"])
            if n:
                return {"name": c["name"], "ats": a, "token": tok, "origin": c["origin"], "jobs": n}
    return None


def discover_boards(log=print, workers: int = 12) -> dict:
    known = {(c["ats"], c["token"].lower()) for c in load_yaml(profile_dir() / "companies.yaml").get("companies", [])}
    prev = load_yaml(auto_path()) if auto_path().exists() else {}
    checked = set(prev.get("checked", []))
    found = {(b["ats"], b["token"].lower()): b for b in prev.get("companies", [])}

    directory = []
    for fetch in (yc_companies, remoteintech_companies):
        try:
            directory += fetch()
        except Exception as e:  # a directory being down must not stop the run
            log(f"  ! {fetch.__name__}: {e}")
    todo = {c["name"].lower(): c for c in directory if c["name"].lower() not in checked}
    log(f"directory: {len(directory)} companies, {len(todo)} not probed yet")

    t0 = time.time()
    with cf.ThreadPoolExecutor(workers) as ex:
        for c, board in zip(todo.values(), ex.map(find_board, todo.values())):
            checked.add(c["name"].lower())
            if board and (board["ats"], board["token"].lower()) not in known:
                found[(board["ats"], board["token"].lower())] = board
    log(f"probed in {time.time() - t0:.0f}s; boards found so far: {len(found)}")

    auto_path().write_text(
        "# Written by `jobbot discover-boards` (jobbot/discovery.py). Safe to delete; it is rebuilt.\n"
        + yaml.safe_dump({"companies": sorted(found.values(), key=lambda b: b["name"].lower()),
                          "checked": sorted(checked)}, allow_unicode=True, sort_keys=False, width=120),
        encoding="utf-8")
    return {"directory": len(directory), "probed": len(todo), "boards": len(found)}
