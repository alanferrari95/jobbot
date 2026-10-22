"""Find an aggregator posting (Himalayas, Jobicy, ...) on the company's own ATS board, so it can be autofilled.

Himalayas pages sit behind a Cloudflare check, so instead of scraping them we guess the company's board token
(Greenhouse / Lever / Ashby / Workable) and look for a posting with the same title.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

import requests

from .sources import HEADERS, TIMEOUT


def _tokens(company: str) -> list[str]:
    name = re.sub(r"\b(inc|llc|ltd|gmbh|ag|s\.?a\.?|limited|corp|co|group|technologies|labs?)\b\.?", "", company.lower())
    out = [re.sub(r"[^a-z0-9]", "", name), re.sub(r"[^a-z0-9]+", "-", name).strip("-"), re.sub(r"[^a-z0-9]", "", company.lower())]
    return list(dict.fromkeys(t for t in out if t))


def _boards(token: str):
    """Yield (title, apply_url) for every open posting on any ATS that knows this token."""
    try:
        r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", headers=HEADERS, timeout=TIMEOUT)
        if r.ok:
            for j in r.json().get("jobs", []):
                yield j["title"], f"https://job-boards.greenhouse.io/{token}/jobs/{j['id']}"
        r = requests.get(f"https://api.lever.co/v0/postings/{token}", params={"mode": "json"}, headers=HEADERS, timeout=TIMEOUT)
        if r.ok and isinstance(r.json(), list):
            for j in r.json():
                yield j["text"], j.get("applyUrl") or j["hostedUrl"]
        r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{token}", headers=HEADERS, timeout=TIMEOUT)
        if r.ok:
            for j in r.json().get("jobs", []):
                yield j["title"], j.get("applyUrl") or j["jobUrl"]
        r = requests.get(f"https://apply.workable.com/api/v1/widget/accounts/{token}", headers=HEADERS, timeout=TIMEOUT)
        if r.ok:
            for j in r.json().get("jobs", []):
                yield j["title"], j.get("application_url") or j.get("url")
    except (requests.RequestException, ValueError, KeyError):
        return


def _norm(t: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", t.lower())).strip()


def find_on_ats(company: str, title: str, min_ratio: float = 0.85) -> str | None:
    best, best_r = None, 0.0
    for tok in _tokens(company):
        for t, url in _boards(tok):
            r = SequenceMatcher(None, _norm(t), _norm(title)).ratio()
            if r > best_r:
                best, best_r = url, r
        if best_r >= 0.97:
            break
    return best if best_r >= min_ratio else None
