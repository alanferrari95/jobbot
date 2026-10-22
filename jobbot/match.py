"""Filtering and scoring of jobs against the candidate's profile (profile/targets.yaml + base_cv.yaml)."""

from __future__ import annotations

import re
from functools import lru_cache

from .sources import Job

# Where the candidate works from, as a regex. Postings that open the role to this region pass the location and
# eligibility gates. Override with targets.yaml `home_region`.
DEFAULT_HOME_REGION = r"argentin\w*|latam|latin america|am[eé]rica latina|south america"


def home_region(tg: dict | None) -> str:
    return (tg or {}).get("home_region") or DEFAULT_HOME_REGION


def _any(patterns: list[str], text: str) -> str | None:
    for p in patterns:
        if re.search(p, text, flags=re.I):
            return p
    return None


def tech_pattern(term: str) -> re.Pattern:
    """Word-boundary regex for a technology name; short or ambiguous names are case-sensitive."""
    if term == "Go":
        return re.compile(r"\bGo(lang)?\b(?! to\b)(?!-to)")
    escaped = re.escape(term)
    flags = 0 if len(term) <= 4 else re.I
    return re.compile(rf"(?<![\w-]){escaped}(?![\w-])", flags)


HAVE = re.compile(r"GCP|Google Cloud|Vertex|Azure|Python|PyTorch|TensorFlow|Docker|Terraform|scikit", re.I)
OPTIONAL = re.compile(r"\bplus\b|nice[- ]to[- ]have|bonus|preferred|desirable|deseable|ideally|or equivalent|"
                      r"familiarity|exposure", re.I)


def _really_required(text: str, m: re.Match, term: str) -> bool:
    """False when the tech is only an alternative ("GCP, AWS or Azure") or a nice-to-have."""
    window = text[max(0, m.start() - 70): m.end() + 70]
    if OPTIONAL.search(window):
        return False
    if HAVE.search(window) and re.search(r"\bor\b|/|\bo\b", window):  # "GCP, AWS or Azure"; not plain lists
        return False
    if term == "Go" and not (m.group(0).lower() == "golang"
                             or re.search(r"(in|with|using|proficien\w*)\s+Go\b", window)):
        return False  # "Go deep on...", "Go live" etc.
    return True


def missing_required(text: str, never_claim: list[str]) -> list[str]:
    out = []
    for t in never_claim:
        if any(_really_required(text, m, t) for m in tech_pattern(t).finditer(text)):
            out.append(t)
    return out


def _money(s: str) -> list[float]:
    vals = []
    for num, k in re.findall(r"\$?\s?(\d{1,3}(?:[,.]\d{3})+|\d+(?:\.\d+)?)\s*([kK])?", s):
        v = float(num.replace(",", "").replace(".", "") if re.search(r"[,.]\d{3}$", num) else num)
        vals.append(v * 1000 if k else v)
    return [v for v in vals if v >= 10]


def salary_max_usd_year(job: Job) -> float | None:
    """Best-effort annual USD maximum from structured salary data (None when unknown)."""
    ex = job.extra or {}
    if job.source == "getonboard":
        lo, hi = (ex.get("salary") or [None, None])[:2]
        top = hi or lo
        return float(top) * 12 if top else None  # Get on Board publishes monthly USD
    if job.source in ("himalayas", "remoteok", "indeed", "torre"):
        lo, hi = ([*(ex.get("salary") or []), None, None])[:2]
        try:
            top = float(hi or lo or 0)
        except (TypeError, ValueError):
            top = 0
        if not top or (ex.get("currency") or "USD") != "USD":
            return None
        period = (ex.get("salary_period") or "annual").lower()
        year = top * 2000 if period.startswith("hour") else top * 12 if period.startswith("month") else top
        return year if 5000 <= year <= 400000 else None  # outside this range the feed's units are wrong
    text = ex.get("compensation") or ex.get("salary") or ""
    if not isinstance(text, str) or not text or ("$" not in text and "USD" not in text.upper()):
        return None
    vals = _money(text)
    if not vals:
        return None
    top = max(vals)
    if re.search(r"/\s?h(ou)?r|hourly|per hour", text, re.I):
        return top * 2000
    if re.search(r"/\s?mo|month", text, re.I):
        return top * 12
    return top if top >= 10000 else None


@lru_cache(maxsize=8)
def open_to_us(region: str) -> re.Pattern:
    """Postings whose location names other countries but whose text explicitly opens the role to the
    candidate's region or to anywhere."""
    return re.compile(rf"anywhere in the world|(candidates|applicants|talent|people|hire|hiring|"
                      rf"based|located|living|work(ing)?) (from|in) (anywhere|{region})|"
                      rf"({region})[- ]based|remote[- ]({region})", re.I)
REMOTE_WORDS = re.compile(r"remote|remoto|telecommute|full[- ]?time|part[- ]?time|async|contract|_?local|unspecified|"
                          r"[\s,/|:;·()-]+", re.I)


def location_status(job: Job, targets: dict) -> tuple[str, str]:
    """('ok' | 'unclear' | 'no', reason). Roles open worldwide or to targets.yaml `location_ok` places fit."""
    loc = job.location or ""
    head = job.description[:2500]
    remote = job.remote or re.search(r"remote|remoto|telecommute", loc, re.I) or (
        job.source == "hn" and "remote" in job.title.lower())
    if not remote:
        return "no", "not remote"
    if _any(targets["location_block"], loc + "\n" + head):
        return "no", "restricted region"
    # Benefit/location blocks at the END of postings ("Work Environment: Fully remote, U.S.-based").
    if re.search(r"(work environment|location|remote)[^.\n]{0,40}\b(u\.s\.|us|usa)[- ](based|only)|"
                 r"\bremote[ \t]*[-–:][ \t]*(usa|u\.s\.a?|us|united states)\b(?![ \t]*(or|and|/|&|,))|"
                 r"(work|remote|flexible)[^.\n]{0,40}across the (u\.s\.|us\b|united states)|u\.s\. core business hours",
                 job.description, re.I):
        return "no", "US-only (stated later in the posting)"
    if _any(targets["location_ok"], loc):
        return "ok", f"location ok ({loc})"
    rest = REMOTE_WORDS.sub(" ", loc).strip()
    if rest:  # names specific places that are not LATAM/Americas ("Remote (USA)", "Berlin or remote")
        return "no", f"remote only in: {loc}"
    if open_to_us(home_region(targets)).search(head):  # a bare "Remote" whose text opens it to us / the world
        return "ok", f"posting open to your region/anywhere ({loc or 'n/a'})"
    return "unclear", f"location unclear ({loc or 'n/a'})"


# --- Gates adapted from MadsLorentzen/ai-job-search (eligibility / language). The defaults fit a candidate who
# speaks English and Spanish and works remotely from LATAM; targets.yaml `other_languages` / `home_region` change them.
OTHER_LANGS = (r"portuguese|portugu[eéê]s|german|deutsch|alem[aá]n|french|fran[cç]ais|franc[eé]s|italian|italiano|dutch|"
               r"polish|japanese|mandarin|chinese|korean|hebrew|arabic|russian|turkish|swedish|danish|norwegian|finnish|"
               r"czech|hungarian|romanian|greek|ukrainian|hindi|vietnamese|thai|indonesian")


@lru_cache(maxsize=8)
def lang_required(other: str) -> re.Pattern:
    """A posting that requires one of the languages in `other` (a regex of languages the candidate doesn't speak)."""
    return re.compile(
        rf"(fluent|fluency|native|proficien\w*|advanced|avanzado|business[- ]level|professional[- ]level|c1|c2|required|"
        rf"must|mandatory|excluyente|indispensable|obligatorio|fluente|nativo)\W+(?:[\w/()-]+\W+){{0,4}}({other})\b|"
        rf"\b({other})\W+(?:[\w/()-]+\W+){{0,3}}(required|is a must|fluency|fluent|native|mandatory|excluyente|"
        rf"indispensable|obligatorio|avanzado|advanced|c1|c2|fluente)", re.I)
PORTUGUESE_TEXT = re.compile(r"\b(você|não|vaga|experiência|conhecimento|desenvolvimento|requisitos|benefícios|"
                             r"trabalho|equipe)\b", re.I)
ELIGIBILITY = re.compile(
    r"(authori[sz]ed|eligible|legally able|right|authori[sz]ation|permit(ted)?) to work in (the )?(us|u\.s\.|usa|united states|"
    r"uk|united kingdom|canada|eu\b|european union|brazil|brasil|mexico|m[eé]xico|colombia|india|australia)|"
    r"\bu\.?s\.? (citizen|citizenship|person)|green card|security clearance|"
    r"(must|should|need to|required to) (currently )?(be )?(based|located|residing|reside|live|living) (in|within) (the )?"
    r"(us|u\.s\.|usa|united states|uk|canada|brazil|brasil|mexico|m[eé]xico|colombia|chile|per[uú]|uruguay|costa rica|"
    r"india|europe|eu\b|emea|portugal|poland|spain|germany)|"
    r"(debe|deber[aá]s|excluyente) (residir|vivir|estar radicad[oa]) en (chile|m[eé]xico|colombia|per[uú]|uruguay|brasil|espa[nñ]a)",
    re.I)
PAY_LOCAL = re.compile(r"(salario|sueldo|pago|remuneraci[oó]n|payment|paid|compensation|salary)\W+(?:[\w$.,/-]+\W+){0,6}"
                       r"(en pesos|pesos argentinos|\bars\b|in pesos|\bbrl\b|reais|\bmxn\b|\bclp\b|\bcop\b)", re.I)
PAY_USD = re.compile(r"(paid|pago|payment|salary|salario|compensation|cobr\w+|facturaci[oó]n)\W+(?:[\w$.,/-]+\W+){0,4}"
                     r"(in|en) (usd|u\$s|us\$|d[oó]lares|dollars|dollar)|\b(usd|u\$s)\b[^.\n]{0,40}(contractor|contract|monthly|mensual)",
                     re.I)
TEXT_RANGE = re.compile(
    r"(?:usd|us\$|u\$s|\$)\s?(\d[\d.,]*)\s?(k)?\s*(?:usd)?\s*(?:-|–|—|to|a|hasta)\s*(?:usd|us\$|u\$s|\$)?\s?(\d[\d.,]*)\s?(k)?\s*"
    r"(?:usd)?\s*(?:/|per|por|al|a|an|each|every)\s?(month|mes|mo\b|monthly|year|a[nñ]o|yr|annum|annual|hour|hora|hr|h\b)",
    re.I)


def _num(s: str, k: str | None) -> float:
    s = s.rstrip(".,")
    if re.search(r"[.,]\d{3}$", s):
        s = re.sub(r"[.,]", "", s)
    else:
        s = s.replace(",", "")
    try:
        v = float(s)
    except ValueError:
        return 0.0
    return v * 1000 if k else v


def text_salary_max_usd_year(text: str) -> float | None:
    """Highest USD range written in the posting text ("USD 2,500 - 3,000 / month"), annualized."""
    best = None
    for m in TEXT_RANGE.finditer(text):
        hi = max(_num(m.group(1), m.group(2)), _num(m.group(3), m.group(4)))
        unit = m.group(5).lower()
        year = hi * 2000 if unit.startswith(("h", "hora")) else hi * 12 if unit.startswith(("m", "mes")) else hi
        if 5000 <= year <= 400000:
            best = max(best or 0, year)
    return best


def gates(job: Job, tg: dict | None = None) -> tuple[str | None, list[str], float]:
    """Hard gate (reason or None), soft notes and a score adjustment for payment currency."""
    text = f"{job.title}\n{job.description}"
    if (m := lang_required((tg or {}).get("other_languages") or OTHER_LANGS).search(text)):
        return f"requires a language you don't speak: \"{m.group(0)[:60]}\"", [], 0
    if len(PORTUGUESE_TEXT.findall(text[:3000])) >= 6 and not re.search("portug", (tg or {}).get("languages", ""), re.I):
        return "posting written in Portuguese (Brazil role)", [], 0
    for m in ELIGIBILITY.finditer(text):
        window = text[max(0, m.start() - 120): m.end() + 120]
        if not re.search(rf"{home_region(tg)}|anywhere|worldwide", window, re.I):
            return f"eligibility: \"{m.group(0)[:70]}\"", [], 0
    notes, adj = [], 0.0
    if PAY_LOCAL.search(text) and not PAY_USD.search(text):
        notes.append("pays in local currency")
        adj -= 1.5
    elif PAY_USD.search(text):
        notes.append("pays in USD")
        adj += 0.75
    return None, notes, adj


TITLE_BROAD = re.compile(r"\b(ai|a\.i\.|ml|llms?|genai|nlp|mlops|machine|learning|data|scien\w*|research\w*|vision|"
                         r"model\w*|agent\w*|intelligence|inteligencia|datos|deep|neural|forward deployed|"
                         r"prompt|rag|algorithm\w*|analytics?)\b", re.I)


def _stage1(job: Job, tg: dict, never_claim: list[str]) -> tuple[bool, list[str], list[str], str]:
    """Cheap filters. Returns (pass, reasons, missing, location_status)."""
    title = job.title
    if (p := _any(tg["title_exclude"], title)):
        return False, [f"title excluded ({p})"], [], ""
    if not (TITLE_BROAD.search(title) or _any(tg["title_include"], title)):
        return False, ["title not ML/AI"], [], ""
    if (p := _any(tg.get("title_exclude_seniority", []), title)):
        first_lead = re.search(r"lead|l[ií]der", p) and re.search(tg.get("lead_ok_if", "$^"), job.description, re.I)
        if not first_lead:
            return False, [f"seniority/research role ({re.search(p, title, re.I).group(0)})"], [], ""
    status, why = location_status(job, tg)
    if status == "no":
        return False, [why], [], status
    hard, _, _ = gates(job, tg)
    if hard:
        return False, [hard], [], status
    missing = missing_required(f"{title}\n{job.description}", never_claim)
    if len(missing) > tg.get("max_missing_required", 1):
        return False, [f"asks for {len(missing)} techs you haven't used: {', '.join(missing)}"], missing, status
    top = salary_usd_year(job)
    if top is not None and top < tg.get("salary_floor_usd_year", 0):
        return False, [f"pays at most ~USD {top:,.0f}/year (~{top / 12:,.0f}/month)"], missing, status
    return True, [why], missing, status


def salary_usd_year(job: Job) -> float | None:
    """Structured salary when the source has it, else a USD range written in the posting text."""
    return salary_max_usd_year(job) or text_salary_max_usd_year(job.description)


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def evaluate_many(jobs: list[Job], tg: dict, base: dict) -> list[tuple[bool, float, list[str], list[str]]]:
    """(keep, score, reasons, missing) per job. Score ~0-10: semantic CV fit (0-5), title fit (0-2),
    location (0-1.5), keywords (0-1), seniority, salary; minus a penalty per required tech the candidate lacks."""
    from . import semantic

    never_claim = base["never_claim"]
    out: list = [None] * len(jobs)
    stage = []
    for i, j in enumerate(jobs):
        ok, reasons, missing, loc = _stage1(j, tg, never_claim)
        if ok:
            stage.append((i, reasons, missing, loc))
        else:
            out[i] = (False, 0.0, reasons, missing)

    tf = semantic.title_fit([jobs[i].title for i, *_ in stage], tg["target_roles"])
    stage2 = []
    for (i, reasons, missing, loc), t in zip(stage, tf):
        if t < tg.get("min_title_fit", 0.5) and not _any(tg["title_include"], jobs[i].title):
            out[i] = (False, 0.0, [f"title too far from target roles ({t:.2f})"], missing)
        else:
            stage2.append((i, reasons, missing, loc, float(t)))

    fits = semantic.cv_fit([f"{jobs[i].title}. {jobs[i].description}" for i, *_ in stage2], base)
    for (i, reasons, missing, loc, t), f in zip(stage2, fits):
        j = jobs[i]
        text = f"{j.title}\n{j.description}".lower()
        hits = [k for k in tg["boost"] if re.search(rf"\b{re.escape(k)}\b", text)]
        score = (5 * _clamp((f["fit"] - tg.get("cv_fit_floor", 0.35)) / tg.get("cv_fit_span", 0.3))
                 + 2 * _clamp((t - 0.45) / 0.4)
                 + (1.5 if loc == "ok" else 0)
                 + min(1.0, 0.1 * sum(tg["boost"][k] for k in hits))
                 + (0.5 if re.search(r"\bsenior\b|\bsr\.?\b|\bssr\b|semi ?senior", j.title, re.I) else 0)
                 - tg["penalty_per_missing_required"] * len(missing))
        reasons = reasons + [f"CV fit {f['fit']:.2f} (covers {f['coverage']:.0%} of the posting)", f"title fit {t:.2f}"]
        if hits:
            reasons.append("matches: " + ", ".join(hits[:10]))
        if missing:
            reasons.append("asks for: " + ", ".join(missing))
        top = salary_usd_year(j)
        if top is not None:
            reasons.append(f"pays up to ~USD {top:,.0f}/year (~{top / 12:,.0f}/month)")
            score += 1 if top >= 80000 else 0.5
        _, pay_notes, pay_adj = gates(j, tg)
        reasons += pay_notes
        score += pay_adj
        out[i] = (score >= tg["min_score"], round(score, 2), reasons, missing)
    return out


def evaluate(job: Job, targets: dict, never_claim: list[str] | None = None, base: dict | None = None
             ) -> tuple[bool, float, list[str], list[str]]:
    """Single-job version of evaluate_many (used by the guardrail)."""
    if base is None:
        from .config import base_cv
        base = base_cv()
    return evaluate_many([job], targets, base)[0]
