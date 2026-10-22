"""Screening layer: decides whether a scored job is really worth the candidate's time BEFORE it reaches them.

Verdicts (stored in jobs.verdict / jobs.verdict_reasons):
  verified         - passed every check on the company's own page and application form, location confirmed
  location_unclear - passed, but only says "Remote" (no home region/worldwide): listed, never auto-packed
  unverified       - could not reach the company's page/form (e.g. only an aggregator link); never packed
  discarded        - failed a check (reasons say which)

Checks, cheapest first:
  1. text gates on the posting (location, eligibility, language, salary, seniority, title)   -> match._stage1
  2. hard requirements the candidate doesn't meet (stack, full-stack, GPU inference, LLM post-training, certifications)
  3. role type (gig/task platforms, offshore staffing, junior, no-code automation)
  4. duplicate of a job already applied/verified (company + title, across sources)
  5. aggregator -> company page (resolve.find_on_ats); aggregator-only = unverified
  6. company page: closed posting, location stated there ("Remote Melbourne, Australia"), re-run text gates
  7. application form: formgate (citizenship elsewhere, local currency incl. EUR/GBP/PLN, US authorization)
Built from real accept/reject decisions; `python -m jobbot verify-test` measures it against them
(profile/verify_testset.yaml).
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from urllib.parse import urlparse

from . import db
from .config import app_dir, base_cv, targets
from .match import _really_required, _stage1, home_region, location_status, tech_pattern
from .sources import Job

AGGREGATORS = ("himalayas.app", "jobs.workable.com/view", "torre.ai", "indeed.com", "remoteok.com", "jobicy.com",
               "weworkremotely.com", "remotive.com")

# Languages/stacks the candidate has never used professionally (targets.yaml `foreign_stack` overrides it).
# Two or more REQUIRED ones = not a fit.
FOREIGN_STACK = ["TypeScript", "JavaScript", "React", "Angular", "Vue", "Node.js", "C#", ".NET", "C++", "Golang", "Rust",
                 "PHP", "Ruby", "Kotlin", "Swift", "GraphQL", "RDF", "OWL", "Snowflake", "Oracle", "Unity", "Unreal"]

REQ = {
    "full-stack role": re.compile(r"(senior[- ]level )?full[- ]?stack (software )?(engineering experience|engineer\b|developer\b)|"
                                  r"looking for a full[- ]?stack", re.I),
    "GPU inference optimization": re.compile(r"vllm|tensorrt|sglang|cuda kernels?|inference engines?|gpu utilization", re.I),
    "requires hands-on LLM training/post-training": re.compile(
        r"(hands-on|practical) (llm )?(training|fine-tuning|post-training) experience|practical experience with supervised "
        r"fine-tuning|you have practical experience with supervised fine-tuning", re.I),
    "mandatory certifications": re.compile(r"certificaci[oó]n(es)? [^.\n]{0,30}(obligatori|excluyente)|certifications?[^.\n]{0,20}"
                                           r"\((obligatorias?|mandatory|required)\)|at least one mandatory professional certification",
                                           re.I),
}
ROLE = {
    "gig/task platform (paid per task)": re.compile(r"paid per task|output-based|per task that meets|compensation is output|"
                                                    r"(~|up to |around |approximately )\s?\d{1,2} hours per week|"
                                                    r"ai trainer|annotation (tasks|projects)|no coding required|"
                                                    r"experience in annotation, content review", re.I),
    "offshore staffing / talent marketplace": re.compile(r"virtual staffing|talent marketplace|talent pool|offshore talent", re.I),
    "junior role": re.compile(r"\b(0|1)\+? ?(-|to) ?[12]? ?years? of experience\b|\b1\+ years? of experience|basic understanding "
                              r"of machine learning|entry[- ]level", re.I),
}
NOCODE = re.compile(r"\bn8n\b|zapier|make\.com|go high level|low-code|no-code", re.I)
GIG_COMPANIES = ["micro1", "evb", "mindrift", "outlier", "alignerr", "dataannotation", "remotasks", "scale ai"]
REMOTE_TIED = re.compile(
    r"remote\s*(\(\d+%\)\s*)?(in|from)\s+(europe|the eu|the uk|the us|the united states|poland|germany|india)\b|"
    r"travel freely within europe|contract with (a )?european (llc|company)|"
    r"\bremote\s+[^\n]{2,50}?,\s*(australia|poland|germany|czechia|india|egypt|united kingdom|united states|usa|canada|spain|"
    r"france|netherlands|portugal|brazil|mexico|philippines|pakistan|nigeria|kenya|south africa)\b", re.I)


def _norm_company(c: str) -> str:
    c = c.lower()
    c = re.sub(r"^(try|get|use|join)(?=[a-z]{3})", "", c)
    c = re.sub(r"\b(inc|llc|ltd|gmbh|ag|s\.?a\.?|limited|corp|co|group|technologies|laboratories|labs?|systems)\b\.?"
               r"|\.(ai|com|io|co)\b", "", c)
    return re.sub(r"[^a-z0-9]", "", c)


def _norm_title(t: str) -> str:
    # Parenthesized variants are kept: "FDE (Gemini)" and "FDE (Anthropic)" are different jobs.
    t = re.sub(r"\[.*?\]|\bremote\b|\blatam\b|\bworldwide\b|- *$", " ", t.lower())
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def text_checks(job: Job, tg: dict, never_claim: list[str]) -> list[str]:
    """Checks 1-3 on posting text only (no network)."""
    ok, reasons, _, _ = _stage1(job, tg, never_claim)
    if not ok:
        return reasons
    if len((job.description or "").strip()) < 200:
        return ["posting has no description (closed or broken)"]
    m = re.search(r"\(([^)]*)\)|\|\s*([^|]+)$", job.title)
    region = (m.group(1) or m.group(2)) if m else ""
    if re.search(r"\b(europe|eu|emea|asia|apac|uk|usa?|united states|india|canada|australia)\b", region, re.I) \
            and not re.search(rf"{home_region(tg)}|latin|americas|global|worldwide|anywhere", region, re.I):
        return [f"title restricts the region: ({region.strip()})"]
    text = f"{job.title}\n{job.description}"
    foreign = []
    for term in tg.get("foreign_stack", FOREIGN_STACK):
        pat = tech_pattern(term) if term not in (".NET", "C#", "C++", "Node.js") else re.compile(re.escape(term))
        if any(_really_required(text, m, term) for m in pat.finditer(text)):
            foreign.append(term)
    if len({"C#", ".NET"} & set(foreign)) == 2:
        foreign.remove(".NET")  # "C# / .NET" is one stack
    if len(foreign) >= 2:
        return [f"requires stack you haven't used: {', '.join(foreign[:5])}"]
    for why, rx in REQ.items():
        hits = rx.findall(text)
        if hits and (why != "GPU inference optimization" or len(hits) >= 3):
            return [why]
    if job.company.strip().lower() in {c.lower() for c in tg.get("gig_companies", GIG_COMPANIES)}:
        return ["gig/task platform"]
    for why, rx in ROLE.items():
        if rx.search(text):
            return [why]
    if len(set(m.lower() for m in NOCODE.findall(text))) >= 2:
        return ["no-code automation role"]
    return []


def is_duplicate(row, others) -> str | None:
    c, t = _norm_company(row["company"]), _norm_title(row["title"])
    for o in others:
        if o["id"] == row["id"]:
            continue
        if _norm_company(o["company"]) == c and SequenceMatcher(None, _norm_title(o["title"]), t).ratio() >= 0.9:
            return f"duplicate of {o['company']} · {o['title']} ({o['status']})"
    return None


def _row_job(r) -> Job:
    return Job(source=r["source"], company=r["company"], title=r["title"], location=r["location"] or "", url=r["url"] or "",
               apply_url=r["apply_url"] or "", description=r["description"] or "", external_id=r["id"],
               remote=bool(r["remote"]), extra=json.loads(r["extra"] or "{}"))


def page_checks(page, url: str, title: str, tg: dict) -> tuple[str, list[str]]:
    """Open the company page: ('closed'|'ok'|'error', reasons)."""
    try:
        resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(3000)
        text = page.inner_text("body")
    except Exception as e:
        return "error", [f"page error: {str(e)[:60]}"]
    if resp is not None and resp.status in (404, 410):
        return "closed", ["posting closed (404)"]
    words = [w for w in re.findall(r"[a-z]{4,}", title.lower()) if w not in ("senior", "remote", "engineer")]
    if words and not any(w in text.lower() for w in words[:4]):
        return "closed", ["company page doesn't show this posting (closed or moved)"]
    if (m := REMOTE_TIED.search(text)):
        return "fail", [f"company page ties the role to a location: \"{m.group(0)[:70]}\""]
    # Only location checks here: the page also holds the application form, whose OPTIONS ("Spanish or Portuguese",
    # "US citizen / Require sponsorship") are not requirements. The form is judged by formgate (required questions only).
    fake = Job(source="page", company="", title=title, location="Remote", url=url, apply_url=url, description=text,
               external_id="page", remote=True)
    status, why = location_status(fake, tg)
    if status == "no":
        return "fail", [f"company page: {why}"]
    return ("ok" if status == "ok" else "ok_unclear"), []


def _scan_here(page, url: str, out) -> list[dict]:
    """formscan on the already-open page (a second sync_playwright inside this one is not allowed)."""
    from .formscan import open_form, scan_page

    open_form(page, url)
    fields = scan_page(page)
    public = [{k: v for k, v in f.items() if k not in ("id", "options")} | (
        {"options": f.get("option_labels") or f.get("options")} if f.get("options") else {}) for f in fields]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"url": url, "form_url": page.url, "fields": public}, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    return public


def verify_rows(rows, all_rows, deep: bool = True, log=print) -> dict:
    """Run the layer on job rows. Returns {id: (verdict, reasons)}. deep=False skips network checks."""
    from .formgate import check as form_check
    from .resolve import find_on_ats

    tg, never = targets(), base_cv()["never_claim"]
    others = [r for r in all_rows if r["status"] in ("applied", "interview", "offer")]
    out, need_net = {}, []
    for r in rows:
        job = _row_job(r)
        reasons = text_checks(job, tg, never)
        if not reasons and (d := is_duplicate(r, others)):
            reasons = [d]
        if reasons:
            out[r["id"]] = ("discarded", reasons)
        else:
            need_net.append(r)
            others.append(r)  # rows come in score order: a later copy of the same posting is a duplicate
    if not deep:
        for r in need_net:
            out[r["id"]] = ("unverified", ["network checks not run"])
        return out

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=True)
        page = browser.new_page()
        for r in need_net:
            url = r["apply_url"] or r["url"]
            if any(a in url for a in AGGREGATORS):
                found = find_on_ats(r["company"], r["title"], 0.85)
                if not found:
                    out[r["id"]] = ("unverified", ["only an aggregator link; company page not found"])
                    continue
                url = found
            status, reasons = page_checks(page, url, r["title"], tg)
            if status in ("closed", "fail"):
                out[r["id"]] = ("discarded", reasons)
                continue
            if status == "error":
                out[r["id"]] = ("unverified", reasons)
                continue
            loc_confirmed = status == "ok" or location_status(_row_job(r), tg)[0] == "ok"
            try:
                fields = [] if r["id"].startswith("getonboard") else _scan_here(page, url, app_dir(r["id"]) / "form.json")
                flags = form_check(fields, tg)
            except Exception as e:
                out[r["id"]] = ("unverified", [f"form not readable: {str(e)[:60]}"])
                continue
            if flags:
                out[r["id"]] = ("discarded", flags[:1])
            elif loc_confirmed:
                out[r["id"]] = ("verified", [f"company page + form OK ({urlparse(url).netloc})"])
            else:  # a bare "Remote" from a US company is usually US-only: the candidate decides, nothing is auto-packed
                out[r["id"]] = ("location_unclear", ["page + form OK, but no home-region/worldwide location stated"])
            if out[r["id"]][0] == "verified" and url != r["apply_url"]:
                db.connect().execute("UPDATE jobs SET apply_url = ? WHERE id = ?", (url, r["id"])).connection.commit()
            log(f"  {out[r['id']][0]:13} {r['company'][:22]:22} {r['title'][:40]}")
        browser.close()
    return out


def save(results: dict) -> None:
    con = db.connect()
    for jid, (verdict, reasons) in results.items():
        con.execute("UPDATE jobs SET verdict = ?, verdict_reasons = ?, verified_at = ? WHERE id = ?",
                    (verdict, "; ".join(reasons), db.now(), jid))
    con.commit()
