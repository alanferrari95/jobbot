"""Recruiter leads: hiring posts that ask to email or message someone instead of applying on a board.

Nothing here scrapes LinkedIn or sends messages. Leads come from:
- HN "Who is hiring" jobs that give an email,
- posts you paste in the dashboard (e.g. from the LinkedIn post searches below).
You send every email/DM yourself; the dashboard only drafts it.
"""

from __future__ import annotations

import json
import re
from urllib.parse import quote

from . import db

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
LINK = re.compile(r"https?://[^\s<>\"')]+")
HOW = [
    (r"\bdm\b|direct message|message me|send me a message|escr[ií]beme|mandame (un )?mensaje", "DM al autor"),
    (r"comment|comenta", "comentar el post"),
    (r"send (your )?(cv|resume)|email (me|us)|env[ií]a(me|nos)? (tu )?cv|mand[aá] (tu )?cv", "enviar CV por email"),
    (r"apply (here|at|via)|link in (the )?comments", "link de postulación"),
]

POST_SEARCHES = [
    "hiring machine learning engineer remote",
    "hiring ML engineer LATAM remote",
    "hiring AI engineer remote contractor",
    "looking for MLOps engineer remote",
    "hiring LLM engineer remote worldwide",
    "buscamos machine learning engineer remoto",
    "estamos contratando data scientist remoto LATAM",
    "hiring forward deployed engineer AI remote",
]

MAX_WORDS = 170


def linkedin_post_searches() -> list[dict]:
    """Search URLs you open yourself (last week's posts, newest first). Queries come from targets.yaml
    `post_searches` when set, else the generic ones above; LinkedIn takes quotes, OR, NOT and parentheses."""
    from .config import targets

    queries = targets().get("post_searches") or POST_SEARCHES
    return [{"q": q, "url": "https://www.linkedin.com/search/results/content/?keywords="
             + quote(q) + "&datePosted=%22past-week%22&sortBy=%22date_posted%22"} for q in queries]


def extract(text: str) -> dict:
    emails = sorted(set(e.rstrip(".") for e in EMAIL.findall(text)))
    links = [l for l in dict.fromkeys(LINK.findall(text)) if "linkedin.com/feed" not in l][:5]
    how = [label for pattern, label in HOW if re.search(pattern, text, re.I)]
    if emails and "enviar CV por email" not in how:
        how.append("enviar CV por email")
    return {"emails": emails, "links": links, "how": how}


def add(source: str, text: str, url: str = "", author: str = "") -> int | None:
    info = extract(text)
    con = db.connect()
    cur = con.execute(
        "INSERT OR IGNORE INTO leads (source, author, url, text, emails, links, how, created, updated) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (source, author, url or None, text, json.dumps(info["emails"]), json.dumps(info["links"]),
         ", ".join(info["how"]), db.now(), db.now()))
    con.commit()
    return cur.lastrowid if cur.rowcount else None


def from_hn_jobs() -> int:
    """HN jobs already in the DB that ask to apply by email become leads."""
    con = db.connect()
    added = 0
    for r in con.execute("SELECT * FROM jobs WHERE source = 'hn' AND status = 'new'"):
        if EMAIL.search(r["description"]) and add("hn", r["description"], r["url"], r["company"]):
            added += 1
    return added


def draft_message(lead: dict) -> dict:
    """Short outreach message from profile/answers.yaml `outreach: {subject, body}` (your own words, real facts only),
    or a minimal generic one built from the contact details in base_cv.yaml."""
    from .config import answers, base_cv

    o = answers().get("outreach") or {}
    c = base_cv()["contact"]
    sign = "\n".join(x for x in (c["name"], (c.get("linkedin") or "").replace("https://www.", "")) if x)
    body = o.get("body") or ("Hi,\n\nI saw your post about the role and I'd like to be considered.\n\n"
                             f"CV attached. Happy to jump on a quick call.\n\nBest,\n{sign}")
    subject = o.get("subject") or "Application"
    emails = json.loads(lead.get("emails") or "[]")
    mailto = (f"mailto:{emails[0]}?subject={quote(subject)}&body={quote(body)}" if emails else None)
    return {"subject": subject, "body": body, "mailto": mailto}
