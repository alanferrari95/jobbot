"""Job discovery from public ATS job-board APIs and Hacker News.

Only public, read-only endpoints are used:
- Greenhouse: https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
- Lever:      https://api.lever.co/v0/postings/{company}?mode=json
- Ashby:      https://api.ashbyhq.com/posting-api/job-board/{org}
- HN:         https://hn.algolia.com/api/v1 (monthly "Who is hiring?" thread)
plus the public APIs/feeds of the job boards in BOARDS. Each ATS has a fetch function (network) and a parse
function (JSON -> Job); `recorded()` feeds saved responses to the same parsers (offline demo and tests).
"""

from __future__ import annotations

import html
import json
import os
import re
import time
from dataclasses import dataclass, field

import requests

from .config import EXAMPLES

# Identify the client to the public APIs. JOBBOT_CONTACT adds a way to reach you (e.g. an email) if you want.
HEADERS = {"User-Agent": "jobbot/0.2 (personal job search; +https://github.com/alanferrari95/jobbot"
                         + (f"; {os.environ['JOBBOT_CONTACT']}" if os.environ.get("JOBBOT_CONTACT") else "") + ")"}
TIMEOUT = 20
RECORDED = EXAMPLES / "demo" / "boards"  # <ats>/<token>.json, same shape as the live API response


@dataclass
class Job:
    source: str  # greenhouse | lever | ashby | hn
    company: str
    title: str
    location: str
    url: str  # original posting on the company's ATS
    apply_url: str
    description: str  # plain text
    external_id: str
    remote: bool = False
    posted_at: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def id(self) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", self.company.lower()).strip("-")
        return f"{self.source}-{slug}-{self.external_id}"[:80]


def html_to_text(raw: str) -> str:
    text = html.unescape(raw or "")
    text = re.sub(r"<p[^>]*>", "\n\n", text, flags=re.I)  # HN uses bare <p> separators
    text = re.sub(r"<(br|/p|/li|/h\d|/div)\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "• ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _get(url: str, **params):
    for attempt in range(4):  # back off on rate limits (429) and transient 5xx
        r = requests.get(url, params=params or None, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code in (429, 502, 503, 504) and attempt < 3:
            time.sleep(float(r.headers.get("Retry-After") or 0) or 5 * (attempt + 1))
            continue
        break
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def _safe_get(url: str, **params):
    """_get that returns None instead of raising, so one failed page doesn't lose a whole source."""
    try:
        return _get(url, **params)
    except (requests.RequestException, ValueError):
        return None


def greenhouse(token: str, company: str) -> list[Job]:
    return parse_greenhouse(_get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs", content="true"),
                            token, company)


def parse_greenhouse(data: dict | None, token: str, company: str) -> list[Job]:
    if data is None:
        return []
    jobs = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        jid = str(j["id"])
        jobs.append(Job(
            source="greenhouse", company=company, title=j.get("title", ""), location=loc,
            url=j.get("absolute_url", ""),
            apply_url=f"https://job-boards.greenhouse.io/{token}/jobs/{jid}",
            description=html_to_text(j.get("content", "")), external_id=jid,
            remote="remote" in loc.lower(), posted_at=j.get("updated_at", ""),
            extra={"token": token},
        ))
    return jobs


def lever(token: str, company: str) -> list[Job]:
    return parse_lever(_get(f"https://api.lever.co/v0/postings/{token}", mode="json"), token, company)


def parse_lever(data: list | None, token: str, company: str) -> list[Job]:
    if data is None:
        return []
    jobs = []
    for j in data:
        cats = j.get("categories") or {}
        loc = cats.get("location", "") or ", ".join(cats.get("allLocations", []) or [])
        workplace = (j.get("workplaceType") or "").lower()
        desc = j.get("descriptionPlain", "") + "\n\n" + "\n\n".join(
            f"{l.get('text', '')}\n{html_to_text(l.get('content', ''))}" for l in j.get("lists", [])
        ) + "\n\n" + (j.get("additionalPlain") or "")
        jobs.append(Job(
            source="lever", company=company, title=j.get("text", ""), location=loc,
            url=j.get("hostedUrl", ""), apply_url=j.get("applyUrl", ""),
            description=desc.strip(), external_id=j["id"],
            remote=workplace == "remote" or "remote" in loc.lower(),
            posted_at=str(j.get("createdAt", "")),
            extra={"token": token, "workplace": workplace, "commitment": cats.get("commitment", "")},
        ))
    return jobs


def ashby(token: str, company: str) -> list[Job]:
    return parse_ashby(_get(f"https://api.ashbyhq.com/posting-api/job-board/{token}", includeCompensation="true"),
                       token, company)


def parse_ashby(data: dict | None, token: str, company: str) -> list[Job]:
    if data is None:
        return []
    jobs = []
    for j in data.get("jobs", []):
        if not j.get("isListed", True):
            continue
        locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations", []) or []]
        loc = " / ".join(l for l in locs if l)
        comp = (j.get("compensation") or {}).get("compensationTierSummary", "")
        jobs.append(Job(
            source="ashby", company=company, title=j.get("title", ""), location=loc,
            url=j.get("jobUrl", ""), apply_url=j.get("applyUrl", ""),
            description=j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
            external_id=j.get("id", ""), remote=bool(j.get("isRemote")) or "remote" in loc.lower(),
            posted_at=j.get("publishedAt", ""),
            extra={"token": token, "employment_type": j.get("employmentType", ""), "compensation": comp},
        ))
    return jobs


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}
PARSERS = {"greenhouse": parse_greenhouse, "lever": parse_lever, "ashby": parse_ashby}


def recorded(ats: str, token: str, company: str) -> list[Job]:
    """A board from a saved API response (examples/demo/boards/<ats>/<token>.json) instead of the network."""
    f = RECORDED / ats / f"{token}.json"
    if not f.exists():
        return []
    return PARSERS[ats](json.loads(f.read_text(encoding="utf-8")), token, company)

BOARD_QUERIES = ["machine learning", "mlops", "ai engineer", "llm", "data scientist", "generative ai",
                 "nlp", "computer vision", "applied scientist", "ai agents"]
GOB_QUERIES = BOARD_QUERIES + ["inteligencia artificial", "científico de datos", "aprendizaje automático"]
GOB_CATEGORIES = ["machine-learning-ai", "data-science-analytics"]


def _gob_pages():
    """Yield Get on Board job items from keyword searches and whole categories."""
    for q in GOB_QUERIES:
        data = _safe_get("https://www.getonbrd.com/api/v0/search/jobs", query=q, per_page=100,
                    expand='["company"]') or {}
        yield from data.get("data", [])
        time.sleep(0.4)
    for cat in GOB_CATEGORIES:
        for page in range(1, 6):
            data = _safe_get(f"https://www.getonbrd.com/api/v0/categories/{cat}/jobs", per_page=100, page=page,
                        expand='["company"]') or {}
            yield from data.get("data", [])
            if page >= (data.get("meta") or {}).get("total_pages", 1):
                break
            time.sleep(0.4)


def getonboard() -> list[Job]:
    """Get on Board (LATAM). Keeps fully remote jobs, and remote-local ones with the countries they are open to."""
    jobs, seen = [], set()
    for x in _gob_pages():
        if x["id"] in seen:
            continue
        seen.add(x["id"])
        a = x["attributes"]
        countries = a.get("countries") or []
        modality = a.get("remote_modality") or ""
        real = [c for c in countries if c.lower() != "remote"]
        if modality in ("fully_remote", "remote_local") and real:
            loc = f"Remote ({', '.join(real)})"  # remote, but only for residents of these countries
        elif modality == "fully_remote":
            loc = "Remote (worldwide)"
        else:
            loc = ", ".join(real) or modality
        company = (((a.get("company") or {}).get("data") or {}).get("attributes") or {}).get("name", "Get on Board")
        desc = "\n\n".join(html_to_text(a.get(k) or "") for k in
                           ("description", "projects", "functions", "desirable", "benefits"))
        url = x["links"]["public_url"]
        jobs.append(Job(
            source="getonboard", company=company, title=a.get("title", ""), location=loc, url=url,
            apply_url=url, description=desc, external_id=x["id"][:40],
            remote=modality in ("fully_remote", "remote_local"),
            posted_at=str(a.get("published_at", "")),
            extra={"salary": [a.get("min_salary"), a.get("max_salary")], "lang": a.get("lang")},
        ))
    return jobs


def remotive() -> list[Job]:
    """Remotive remote jobs. Per its API terms, postings link back to Remotive."""
    jobs, seen = [], set()
    for q in BOARD_QUERIES:
        data = _safe_get("https://remotive.com/api/remote-jobs", search=q, limit=100) or {}
        for j in data.get("jobs", []):
            if j["id"] in seen:
                continue
            seen.add(j["id"])
            jobs.append(Job(
                source="remotive", company=j.get("company_name", ""), title=j.get("title", ""),
                location=j.get("candidate_required_location", ""), url=j.get("url", ""),
                apply_url=j.get("url", ""), description=html_to_text(j.get("description", "")),
                external_id=str(j["id"]), remote=True, posted_at=j.get("publication_date", ""),
                extra={"job_type": j.get("job_type"), "salary": j.get("salary")},
            ))
        time.sleep(0.5)
    return jobs


def _himalayas_job(j: dict) -> Job:
    guid = j.get("guid") or j.get("applicationLink", "")
    countries = j.get("locationRestrictions") or []
    if not countries:
        # No restriction listed is NOT the same as worldwide (ClanX, Unifonic, DeepHealth... were country-bound).
        loc = "Remote (unspecified)"
    else:
        shown = countries[:12] + (["Argentina"] if "Argentina" in countries[12:] else [])
        loc = "Remote (" + ", ".join(shown) + (", ..." if len(countries) > len(shown) else "") + ")"
    return Job(
        source="himalayas", company=j.get("companyName", ""), title=j.get("title", ""),
        location=loc, url=guid, apply_url=j.get("applicationLink") or guid,
        description=html_to_text(j.get("description", "")), external_id=guid.rstrip("/").rsplit("-", 1)[-1],
        remote=True, posted_at=str(j.get("pubDate", "")),
        extra={"employment_type": j.get("employmentType"), "currency": j.get("currency"),
               "salary": [j.get("minSalary"), j.get("maxSalary")], "salary_period": j.get("salaryPeriod"),
               "countries": countries},
    )


def himalayas(max_pages: int = 6) -> list[Job]:
    """Himalayas search API, restricted to jobs open to Argentina or worldwide (20 results per page)."""
    jobs, seen = [], set()
    for q in BOARD_QUERIES:
        for where in ({"country": "Argentina"}, {"worldwide": "true"}):
            for page in range(1, max_pages + 1):
                data = _safe_get("https://himalayas.app/jobs/api/search", q=q, page=page, **where) or {}
                batch = data.get("jobs", [])
                for j in batch:
                    guid = j.get("guid") or j.get("applicationLink", "")
                    if guid and guid not in seen:
                        seen.add(guid)
                        jobs.append(_himalayas_job(j))
                if len(batch) < 20:
                    break
                time.sleep(1.0)
    return jobs


def remoteok() -> list[Job]:
    """RemoteOK public feed (latest jobs; the first element is a legal notice)."""
    data = _get("https://remoteok.com/api") or []
    jobs = []
    for j in data[1:]:
        if not j.get("id"):
            continue
        jobs.append(Job(
            source="remoteok", company=j.get("company", ""), title=j.get("position", ""),
            location=j.get("location") or "Remote (worldwide)", url=j.get("url", ""),
            apply_url=j.get("apply_url") or j.get("url", ""), description=html_to_text(j.get("description", "")),
            external_id=str(j["id"]), remote=True, posted_at=j.get("date", ""),
            extra={"tags": j.get("tags"), "salary": [j.get("salary_min"), j.get("salary_max")]},
        ))
    return jobs


def jobicy() -> list[Job]:
    """Jobicy remote-jobs API: keyword tags, both for LATAM and for 'anywhere' geos."""
    jobs, seen = [], set()
    for tag in ("machine learning", "artificial intelligence", "llm", "data scientist", "mlops"):
        for geo in ("latam", "anywhere", None):
            params = {"count": 100, "tag": tag} | ({"geo": geo} if geo else {})
            try:
                data = _get("https://jobicy.com/api/v2/remote-jobs", **params) or {}
            except requests.HTTPError:
                continue
            for j in data.get("jobs", []):
                if j["id"] in seen:
                    continue
                seen.add(j["id"])
                jobs.append(Job(
                    source="jobicy", company=j.get("companyName", ""), title=html.unescape(j.get("jobTitle", "")),
                    location="Remote (" + (j.get("jobGeo") or "unspecified") + ")", url=j.get("url", ""),
                    apply_url=j.get("url", ""), description=html_to_text(j.get("jobDescription", "")),
                    external_id=str(j["id"]), remote=True, posted_at=j.get("pubDate", ""),
                    extra={"level": j.get("jobLevel")},
                ))
            time.sleep(0.4)
    return jobs


def torre(pages: int = 5) -> list[Job]:
    """Torre (LATAM-focused marketplace): remote opportunities by role. Descriptions come from torre_details()."""
    jobs, seen = [], set()
    hdr = HEADERS | {"Content-Type": "application/json"}
    for q in ("machine learning", "ai engineer", "data scientist", "llm", "mlops", "generative ai"):
        body = {"and": [{"skill/role": {"text": q, "experience": "potential-to-develop", "proficiency": "proficient"}},
                        {"remote": {"term": True}}]}
        results = []
        for page in range(pages):  # the API allows at most 20 results per request
            r = requests.post(f"https://search.torre.co/opportunities/_search?size=20&offset={page * 20}&aggregate=false",
                              json=body, headers=hdr, timeout=TIMEOUT)
            batch = r.json().get("results", []) if r.ok else []
            results += batch
            if len(batch) < 20:
                break
            time.sleep(0.3)
        for x in results:
            if x["id"] in seen or x.get("status") != "open":
                continue
            seen.add(x["id"])
            place = x.get("place") or {}
            locs = [l if isinstance(l, str) else (l.get("name") or l.get("country") or "")
                    for l in (place.get("location") or x.get("locations") or [])]
            loc = ("Remote (worldwide)" if place.get("anywhere")
                   else "Remote (" + (", ".join(l for l in locs if l) or "unspecified") + ")")
            comp = (x.get("compensation") or {}).get("data") or {}
            org = (x.get("organizations") or [{}])[0].get("name", "")
            skills = ", ".join(s["name"] for s in x.get("skills") or [])
            url = f"https://torre.ai/post/{x['id']}-{x.get('slug', '')}"
            jobs.append(Job(
                source="torre", company=org, title=x.get("objective", ""), location=loc, url=url, apply_url=url,
                description=f"{x.get('tagline', '')}\n\nSkills: {skills}", external_id=x["id"],
                remote=True, posted_at=x.get("created", ""),
                extra={"commitment": x.get("commitment"), "currency": comp.get("currency"),
                       "salary": [comp.get("minAmount") or None, comp.get("maxAmount") or None],
                       "salary_period": comp.get("periodicity")},
            ))
        time.sleep(0.5)
    return jobs


def torre_details(job: Job) -> None:
    """Prepend the full Torre posting text to a job's short description (one extra request)."""
    try:
        d = _get(f"https://torre.ai/api/suite/opportunities/{job.external_id}") or {}
    except requests.RequestException:
        return
    parts = [f"{x.get('code', '').title()}:\n{x.get('content', '')}" for x in d.get("details") or [] if x.get("content")]
    if parts:
        job.description = "\n\n".join(parts) + "\n\n" + job.description


def workable(max_pages: int = 8) -> list[Job]:
    """jobs.workable.com search: remote jobs plus jobs located in Argentina."""
    jobs, seen = [], set()
    for q in ("machine learning", "ai engineer", "data scientist", "llm", "mlops", "generative ai"):
        for where in ({"workplace": "remote"}, {"location": "Argentina"}):
            token = None
            for _ in range(max_pages):
                params = {"query": q, **where} | ({"pageToken": token} if token else {})
                data = _safe_get("https://jobs.workable.com/api/v1/jobs", **params) or {}
                for j in data.get("jobs", []):
                    if j["id"] in seen:
                        continue
                    seen.add(j["id"])
                    country = ((j.get("location") or {}).get("countryName")) or ""
                    remote = j.get("workplace") == "remote" or "TELECOMMUTE" in (j.get("locations") or [])
                    desc = "\n\n".join(html_to_text(j.get(k) or "") for k in
                                       ("description", "requirementsSection", "benefitsSection"))
                    jobs.append(Job(
                        source="workable", company=(j.get("company") or {}).get("title", ""), title=j.get("title", ""),
                        location=(f"Remote ({country})" if remote else country) or "Remote",
                        url=j.get("url", ""), apply_url=j.get("url", ""), description=desc,
                        external_id=j["id"][:36], remote=remote, posted_at=j.get("created", ""),
                        extra={"employment_type": j.get("employmentType"), "lang": j.get("language")},
                    ))
                token = data.get("nextPageToken")
                if not token:
                    break
                time.sleep(0.3)
    return jobs


WWR_FEEDS = ["remote-programming-jobs", "remote-back-end-programming-jobs", "remote-full-stack-programming-jobs",
             "all-other-remote-jobs"]


def weworkremotely() -> list[Job]:
    """We Work Remotely RSS feeds. Titles are 'Company: Role'."""
    import xml.etree.ElementTree as ET
    jobs, seen = [], set()
    for feed in WWR_FEEDS:
        r = requests.get(f"https://weworkremotely.com/categories/{feed}.rss", headers=HEADERS, timeout=TIMEOUT)
        if not r.ok:
            continue
        for it in ET.fromstring(r.content).iter("item"):
            link = it.findtext("link") or ""
            if not link or link in seen:
                continue
            seen.add(link)
            company, _, title = (it.findtext("title") or "").partition(": ")
            jobs.append(Job(
                source="wwr", company=company.strip(), title=title.strip() or company.strip(),
                location="Remote (" + (it.findtext("region") or "unspecified") + ")", url=link, apply_url=link,
                description=html_to_text(it.findtext("description") or ""), external_id=link.rstrip("/").rsplit("/", 1)[-1][:60],
                remote=True, posted_at=it.findtext("pubDate") or "",
            ))
        time.sleep(0.5)
    return jobs


BOARDS = {"getonboard": getonboard, "remotive": remotive, "himalayas": himalayas, "remoteok": remoteok,
          "jobicy": jobicy, "wwr": weworkremotely, "torre": torre, "workable": workable}


def hn_who_is_hiring(max_age_days: int = 40) -> list[Job]:
    """Top-level comments of the latest 'Ask HN: Who is hiring?' thread."""
    hits = _get("https://hn.algolia.com/api/v1/search_by_date",
                tags="story,author_whoishiring", query="Who is hiring", hitsPerPage=5)["hits"]
    story = next((h for h in hits if "who is hiring" in h["title"].lower()), None)
    if not story or time.time() - story["created_at_i"] > max_age_days * 86400:
        return []
    thread = _get(f"https://hn.algolia.com/api/v1/items/{story['objectID']}")
    jobs = []
    for c in thread.get("children", []):
        text = html_to_text(c.get("text") or "")
        if not text:
            continue
        first = text.split("\n", 1)[0]
        parts = [p.strip() for p in first.split("|")]
        company = parts[0][:60] if parts else "HN"
        # The header line is "Company | Role | Location | ..."; keep it short.
        first = " | ".join(parts[:4])[:200]
        links = re.findall(r"https?://[^\s<>\"')]+", html.unescape(c.get("text") or ""))
        url = f"https://news.ycombinator.com/item?id={c['id']}"
        location = next((p for p in parts[1:] if re.search(r"remote|onsite|hybrid", p, re.I)), "")
        jobs.append(Job(
            source="hn", company=company, title=first, location=location,
            url=url, apply_url=links[0] if links else url, description=text,
            external_id=str(c["id"]), remote="remote" in first.lower(),
            posted_at=c.get("created_at", ""), extra={"thread": story["title"], "links": links[:5]},
        ))
    return jobs
