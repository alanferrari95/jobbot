"""Hard gates on the application FORM itself (not the posting text).

Aggregators often label a role "worldwide" while the company's form asks for Indian citizenship, a salary in
EGP, or US work authorization. A required question like that means the role is not open to a candidate who
works remotely from their home region (targets.yaml `home_region`) and is paid in USD.
"""

from __future__ import annotations

import re

from .match import home_region

FOREIGN = (r"india|pakistan|egypt|nigeria|kenya|philippines|brazil|brasil|mexico|m[eé]xico|colombia|chile|peru|"
           r"united states|u\.s\.|usa|\bus\b|canada|uk|united kingdom|germany|poland|portugal|south africa|vietnam|"
           r"indonesia|bangladesh|turkey|saudi|uae|israel|australia")
GATES = [
    (re.compile(rf"(citizen|national|nationality) of ({FOREIGN})|({FOREIGN})[^?]{{0,30}}citizen", re.I),
     "asks for citizenship of another country"),
    (re.compile(rf"(currently )?(based|located|residing|reside|live|living) in ({FOREIGN})", re.I),
     "asks if you live in another country"),
    (re.compile(r"\b(inr|lakh|lakhs|ctc|egp|brl|pkr|php|ngn|kes|mxn|cop|idr|vnd|eur|gbp|pln|chf)\b|in reais|en reales|"
                r"in euros?\b|€|£", re.I),
     "salary in a non-USD currency (INR/EGP/BRL/EUR/GBP/PLN...)"),
    (re.compile(r"live legally in (europe|the eu|the uk)|right to work in (europe|the eu|the uk)", re.I),
     "asks about residence/work rights in Europe"),
    (re.compile(r"us citizen|green card|permanent resident|\bopt\b|security clearance|"
                r"authori[sz]ed to work in the (us|united states|u\.s\.)", re.I),
     "US work authorization"),
    (re.compile(r"portugu[eéê]s|portuguese", re.I), "asks about Portuguese"),
]
OK_ANSWER = r"remote|anywhere|worldwide|no, i|not required|sponsorship"


def check(fields: list[dict], tg: dict | None = None) -> list[str]:
    """Reasons this form is not for the candidate (empty list = no red flags). Only required questions count."""
    ok_answer = re.compile(rf"{home_region(tg)}|{OK_ANSWER}", re.I)
    out = []
    for f in fields:
        if not f.get("required"):
            continue
        text = f.get("label", "")  # options often list countries/languages as mere choices
        for rx, why in GATES:
            m = rx.search(text)
            if not m:
                continue
            opts = " ".join(map(str, f.get("options") or []))
            if why == "US work authorization" and re.search(r"sponsorship|require sponsorship|no", opts, re.I) \
                    and not re.search(r"us citizen.*permanent resident.*opt", opts, re.I):
                continue  # a plain "do you need sponsorship?" (answer: No) is fine
            if why == "asks if you live in another country" and ok_answer.search(f.get("label", "")):
                continue
            out.append(f"{why}: \"{f.get('label', '')[:90]}\"")
            break
    return out
