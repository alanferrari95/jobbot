"""Frontier-AI news for the LinkedIn post queue: official blogs (RSS/Atom) + top AI stories on Hacker News."""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

from .sources import html_to_text

HEADERS = {"User-Agent": "Mozilla/5.0 jobbot/0.1 (personal news reader)"}
ATOM = "{http://www.w3.org/2005/Atom}"

FEEDS = {
    "OpenAI": "https://openai.com/news/rss.xml",
    "Google DeepMind": "https://deepmind.google/blog/rss.xml",
    "Google Research": "https://research.google/blog/rss/",
    "Google AI": "https://blog.google/technology/ai/rss/",
    "Hugging Face": "https://huggingface.co/blog/feed.xml",
    "Microsoft Research": "https://www.microsoft.com/en-us/research/feed/",
    "Simon Willison": "https://simonwillison.net/atom/everything/",
    "Import AI": "https://importai.substack.com/feed",
    "Latent Space": "https://www.latent.space/feed",
    "GitHub Blog (AI)": "https://github.blog/ai-and-ml/feed/",
}

AI_TERMS = re.compile(
    r"\b(ai|llm|llms|gpt|claude|gemini|llama|mistral|qwen|deepseek|model|agent|agents|transformer|"
    r"diffusion|inference|open[- ]?source|benchmark|reasoning|rag|mcp|anthropic|openai|deepmind)\b", re.I)


def _date(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return parsedate_to_datetime(s)
    except Exception:
        pass
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _text(el, *names) -> str:
    for n in names:
        x = el.find(n)
        if x is not None and (x.text or "").strip():
            return x.text.strip()
    return ""


def fetch_feed(source: str, url: str, since: datetime) -> list[dict]:
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for it in root.findall(".//item") + root.findall(f".//{ATOM}entry"):
        title = _text(it, "title", f"{ATOM}title")
        link = _text(it, "link")
        if not link:
            le = it.find(f"{ATOM}link")
            link = le.get("href", "") if le is not None else ""
        when = _date(_text(it, "pubDate", f"{ATOM}published", f"{ATOM}updated"))
        if not title or not link or (when and when < since):
            continue
        summary = html_to_text(_text(it, "description", f"{ATOM}summary", f"{ATOM}content"))[:1200]
        out.append({"source": source, "title": title, "url": link, "summary": summary,
                    "published": when.isoformat() if when else "", "points": 0})
    return out


def fetch_hn(since: datetime, min_points: int = 150) -> list[dict]:
    r = requests.get("https://hn.algolia.com/api/v1/search", headers=HEADERS, timeout=20, params={
        "tags": "story", "numericFilters": f"created_at_i>{int(since.timestamp())},points>{min_points}",
        "hitsPerPage": 100})
    r.raise_for_status()
    out = []
    for h in r.json()["hits"]:
        if not AI_TERMS.search(h.get("title") or ""):
            continue
        out.append({"source": "Hacker News", "title": h["title"],
                    "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
                    "summary": f"{h['points']} points, {h.get('num_comments', 0)} comments. "
                               f"Discussion: https://news.ycombinator.com/item?id={h['objectID']}",
                    "published": h.get("created_at", ""), "points": h["points"]})
    return out


def fetch_all(days: int = 3) -> tuple[list[dict], list[str]]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    items, errors = [], []
    for source, url in FEEDS.items():
        try:
            items.extend(fetch_feed(source, url, since))
        except Exception as e:
            errors.append(f"{source}: {e}")
        time.sleep(0.2)
    try:
        items.extend(fetch_hn(since))
    except Exception as e:
        errors.append(f"HN: {e}")
    return items, errors
