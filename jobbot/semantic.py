"""Semantic matching between the candidate's CV and job postings (multilingual sentence embeddings).

- title_fit: similarity of the job title to the roles the candidate targets (targets.yaml: target_roles).
- cv_fit: how well the posting's main points are covered by the candidate's real experience. The posting is
  split into sentence-sized chunks; each chunk takes its best match among the CV chunks (bullets, summary,
  skills); cv_fit is the mean of the top chunks. coverage is the share of chunks with a good match.

The model runs locally through fastembed/ONNX (no torch, no API). Chunk embeddings are cached in SQLite.
JOBBOT_EMBEDDER=hash swaps in a tiny deterministic hashing embedder (no download): used by the offline demo
and the tests. Its scores are only illustrative.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from functools import lru_cache

import numpy as np

from .config import db_path, models_dir

MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DIM = 384
TOP_K = 8            # job chunks averaged into cv_fit
GOOD_MATCH = 0.55    # chunk similarity counted as "covered"
MAX_CHUNKS = 40


class HashEmbedder:
    """Bag of words + character trigrams hashed into DIM signed buckets. Offline and deterministic."""

    def embed(self, texts, batch_size: int = 64):
        for t in texts:
            v = np.zeros(DIM, dtype=np.float32)
            words = re.findall(r"\w+", t.lower())
            grams = words + [w[i:i + 3] for w in words if len(w) > 3 for i in range(len(w) - 2)]
            for g in grams:
                h = int.from_bytes(hashlib.md5(g.encode("utf-8")).digest()[:4], "little")
                v[h % DIM] += 1.0 if h & 1 << 31 else -1.0
            yield v


def backend() -> str:
    return os.environ.get("JOBBOT_EMBEDDER", "fastembed")


@lru_cache(maxsize=2)
def _load(name: str):
    if name == "hash":
        return HashEmbedder()
    import warnings
    warnings.filterwarnings("ignore")
    from fastembed import TextEmbedding
    return TextEmbedding(MODEL, cache_dir=str(models_dir()))


def _model():
    return _load(backend())


def _key(text: str) -> str:
    # fastembed keys stay sha1(text) so existing caches remain valid; other backends get their own namespace.
    prefix = "" if backend() == "fastembed" else backend() + ":"
    return hashlib.sha1((prefix + text).encode("utf-8")).hexdigest()


def _cache() -> sqlite3.Connection:
    db_path().parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path())
    con.execute("CREATE TABLE IF NOT EXISTS emb (k TEXT PRIMARY KEY, v BLOB)")
    return con


def embed(texts: list[str]) -> np.ndarray:
    """Unit-normalized embeddings, cached by text hash."""
    if not texts:
        return np.zeros((0, DIM), dtype=np.float32)
    keys = [_key(t) for t in texts]
    con = _cache()
    have = {}
    for i in range(0, len(keys), 900):
        chunk = keys[i:i + 900]
        q = f"SELECT k, v FROM emb WHERE k IN ({','.join('?' * len(chunk))})"
        have.update({k: np.frombuffer(v, dtype=np.float32) for k, v in con.execute(q, chunk)})
    todo = sorted({k: t for k, t in zip(keys, texts) if k not in have}.items())
    if todo:
        vecs = np.array(list(_model().embed([t for _, t in todo], batch_size=64)), dtype=np.float32)
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9
        con.executemany("INSERT OR REPLACE INTO emb VALUES (?, ?)", [(k, v.tobytes()) for (k, _), v in zip(todo, vecs)])
        con.commit()
        have.update({k: v for (k, _), v in zip(todo, vecs)})
    con.close()
    return np.stack([have[k] for k in keys])


SENT = re.compile(r"(?<=[.!?;])\s+|\n+|\s[•·▪●-]\s")
BOILER = re.compile(r"equal opportunity|benefits|we offer|perks|salary|compensation|paid time off|pto\b|"
                    r"about (us|the company)|privacy|accommodation|visa|diversity|401\(k\)|insurance plan", re.I)


def chunks(text: str, max_chunks: int = MAX_CHUNKS) -> list[str]:
    """Sentence-sized pieces of a posting that describe the work or the requirements."""
    out = []
    for s in SENT.split(text or ""):
        s = s.strip(" •-·\t")
        n = len(s.split())
        if 5 <= n <= 70 and not BOILER.search(s):
            out.append(s)
        if len(out) >= max_chunks:
            break
    return out


def cv_chunks(base: dict) -> list[str]:
    out = [base["title"]]
    out += [s for s in re.split(r"(?<=\.)\s+", " ".join(base["summary"].split())) if len(s.split()) >= 5]
    for role in base["experience"]:
        for g in role["groups"]:
            ctx = (g.get("client") or role["company"])
            out += [f"{b['text']} ({ctx})" for b in g["bullets"]]
    out += [b["text"] for p in base["projects"] for b in p["bullets"]]
    out += [f"{s['label']}: {', '.join(s['items'])}" for s in base["skills"]]
    out += base.get("certifications", [])
    return out


@lru_cache(maxsize=4)
def _cv_matrix(base_key: str, texts: tuple[str, ...]) -> np.ndarray:
    return embed(list(texts))


def title_fit(titles: list[str], roles: list[str]) -> np.ndarray:
    if not titles:
        return np.zeros(0)
    t = embed([re.sub(r"\s+", " ", x)[:200] for x in titles])
    r = embed(roles)
    return (t @ r.T).max(axis=1)


def cv_fit(descriptions: list[str], base: dict) -> list[dict]:
    """Per posting: {'fit': mean of the top chunk matches, 'coverage': share of well-covered chunks,
    'best': [(job chunk, cv chunk, sim)] for the strongest matches}."""
    cv_texts = tuple(cv_chunks(base))
    cv = _cv_matrix(hashlib.sha1("\n".join(cv_texts).encode()).hexdigest(), cv_texts)
    per_job = [chunks(d) for d in descriptions]
    flat = [c for cs in per_job for c in cs]
    E = embed(flat)
    out, i = [], 0
    for cs in per_job:
        if not cs:
            out.append({"fit": 0.0, "coverage": 0.0, "best": []})
            continue
        S = E[i:i + len(cs)] @ cv.T
        i += len(cs)
        best = S.max(axis=1)
        top = np.sort(best)[::-1][:TOP_K]
        order = np.argsort(-best)[:3]
        out.append({"fit": float(top.mean()), "coverage": float((best >= GOOD_MATCH).mean()),
                    "best": [(cs[j], cv_texts[int(S[j].argmax())], round(float(best[j]), 2)) for j in order]})
    return out
