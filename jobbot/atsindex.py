"""Jobs from the open ATS index published by Feashliaa/job-board-aggregator (MIT).

The index (github.com/Feashliaa/job-board-data, refreshed daily) lists ~1.4M open postings from ~29k companies
on Greenhouse, Lever, Ashby, Workday, BambooHR... with title, location and URL but no description.
We download it (~75 MB gzipped, a few seconds), keep ML/AI titles whose location can fit the candidate
(targets.yaml location_ok, worldwide or a bare "Remote"), and fetch the full posting from the ATS API
only for those candidates. Workday/BambooHR/iCIMS/Paylocity are skipped (no public detail API).
"""

from __future__ import annotations

import concurrent.futures as cf
import gzip
import json
import re

import requests

from .config import companies, targets
from .sources import HEADERS, TIMEOUT, Job, html_to_text

DATA = "https://raw.githubusercontent.com/Feashliaa/job-board-data/HEAD/data/"
REMOTEISH = re.compile(r"remote|remoto|anywhere|telecommute|work from home|distributed", re.I)
BARE = re.compile(r"remote|remoto|anywhere|full[- ]?time|[\s,/|:;·()*-]+", re.I)

GH = re.compile(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)/jobs/(\d+)|[?&]gh_jid=(\d+)", re.I)
LV = re.compile(r"jobs\.lever\.co/([\w.-]+)/([0-9a-f-]{36})", re.I)
AB = re.compile(r"jobs\.ashbyhq\.com/([^/]+)/([0-9a-f-]{36})", re.I)


def load_index() -> list[dict]:
    manifest = requests.get(DATA + "chunks/jobs_manifest.json", headers=HEADERS, timeout=60).json()
    names = manifest["chunks"] if isinstance(manifest, dict) else manifest

    def chunk(name: str) -> list[dict]:
        r = requests.get(DATA + "chunks/" + name, headers=HEADERS, timeout=180)
        r.raise_for_status()
        return json.loads(gzip.decompress(r.content))

    out: list[dict] = []
    with cf.ThreadPoolExecutor(8) as ex:
        for part in ex.map(chunk, names):
            out.extend(part)
    return out


def candidates(index: list[dict], tg: dict) -> list[dict]:
    """ML/AI titles in locations that may fit (checked again on the full posting)."""
    from .match import TITLE_BROAD, _any

    known = {c["token"].lower() for c in companies()}
    out = []
    for j in index:
        title, loc = j.get("title") or "", j.get("location") or ""
        if j.get("ats") not in ("Greenhouse", "Lever", "Ashby") or j.get("is_recruiter"):
            continue
        if not TITLE_BROAD.search(title) or _any(tg["title_exclude"], title) \
                or _any(tg.get("title_exclude_seniority", []), title):
            continue
        if _any(tg["location_block"], loc):
            continue
        if not (_any(tg["location_ok"], loc) or (REMOTEISH.search(loc) and not BARE.sub(" ", loc).strip())):
            continue
        if (j.get("company") or "").lower() in known:  # already fetched from its own board
            continue
        out.append(j)
    return out


_ashby_boards: dict[str, list] = {}
_gh_names: dict[str, str] = {}


def _detail(rec: dict) -> Job | None:
    url = rec["url"]
    try:
        if (m := GH.search(url)) and m.group(1):
            token, jid = m.group(1), m.group(2)
            d = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{jid}",
                             headers=HEADERS, timeout=TIMEOUT).json()
            if token not in _gh_names:
                meta = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{token}", headers=HEADERS,
                                    timeout=TIMEOUT)
                _gh_names[token] = meta.json().get("name", token) if meta.ok else token
            loc = (d.get("location") or {}).get("name", rec.get("location", ""))
            return Job(source="greenhouse", company=_gh_names[token], title=d.get("title", rec["title"]), location=loc,
                       url=d.get("absolute_url", url), apply_url=f"https://job-boards.greenhouse.io/{token}/jobs/{jid}",
                       description=html_to_text(d.get("content", "")), external_id=jid,
                       remote=bool(REMOTEISH.search(loc)), posted_at=d.get("updated_at", ""),
                       extra={"token": token, "via": "atsindex"})
        if m := LV.search(url):
            token, jid = m.groups()
            j = requests.get(f"https://api.lever.co/v0/postings/{token}/{jid}", headers=HEADERS, timeout=TIMEOUT).json()
            cats = j.get("categories") or {}
            loc = cats.get("location", "") or ", ".join(cats.get("allLocations", []) or [])
            desc = (j.get("descriptionPlain", "") + "\n\n" + "\n\n".join(
                f"{l.get('text', '')}\n{html_to_text(l.get('content', ''))}" for l in j.get("lists", []))
                + "\n\n" + (j.get("additionalPlain") or ""))
            workplace = (j.get("workplaceType") or "").lower()
            return Job(source="lever", company=rec.get("company") or token, title=j.get("text", rec["title"]),
                       location=loc, url=j.get("hostedUrl", url), apply_url=j.get("applyUrl", url),
                       description=desc.strip(), external_id=jid,
                       remote=workplace == "remote" or bool(REMOTEISH.search(loc)), posted_at=str(j.get("createdAt", "")),
                       extra={"token": token, "workplace": workplace, "via": "atsindex"})
        if m := AB.search(url):
            org, jid = m.groups()
            if org not in _ashby_boards:
                r = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{org}", headers=HEADERS,
                                 params={"includeCompensation": "true"}, timeout=TIMEOUT)
                _ashby_boards[org] = r.json().get("jobs", []) if r.ok else []
            j = next((x for x in _ashby_boards[org] if x.get("id") == jid), None)
            if not j:
                return None
            locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations", []) or []]
            loc = " / ".join(l for l in locs if l)
            comp = (j.get("compensation") or {}).get("compensationTierSummary", "")
            return Job(source="ashby", company=rec.get("company") or org, title=j.get("title", rec["title"]),
                       location=loc, url=j.get("jobUrl", url), apply_url=j.get("applyUrl", url),
                       description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
                       external_id=jid, remote=bool(j.get("isRemote")) or bool(REMOTEISH.search(loc)),
                       posted_at=j.get("publishedAt", ""),
                       extra={"token": org, "compensation": comp, "via": "atsindex"})
    except (requests.RequestException, ValueError, KeyError):
        return None
    return None


def ats_index() -> list[Job]:
    tg = targets()
    cands = candidates(load_index(), tg)
    with cf.ThreadPoolExecutor(12) as ex:
        return [j for j in ex.map(_detail, cands) if j is not None]
