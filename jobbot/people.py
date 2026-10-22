"""LinkedIn search links for people to follow at a company. Nothing is scraped: you open them."""

from urllib.parse import quote

SEARCHES = [
    ("Technical recruiters", "{company} technical recruiter"),
    ("Talent / recruiting", "{company} talent acquisition"),
    ("ML / AI leads", "{company} head of machine learning"),
    ("ML engineering managers", "{company} machine learning engineering manager"),
    ("ML engineers (peers, referrals)", "{company} machine learning engineer"),
]


def people_md(company: str) -> str:
    lines = [f"# People to follow at {company}", "",
             "Open each link while logged in, and follow or connect with 2–3 relevant people.",
             "Send each connection note yourself; nothing is automated on LinkedIn.", ""]
    for label, q in SEARCHES:
        url = "https://www.linkedin.com/search/results/people/?keywords=" + quote(q.format(company=company))
        lines.append(f"- [{label}]({url})")
    lines.append(f"- [Company page]({'https://www.linkedin.com/search/results/companies/?keywords=' + quote(company)})")
    return "\n".join(lines) + "\n"
