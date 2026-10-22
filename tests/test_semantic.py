"""Semantic matching with the embedding model mocked: chunking, caching, title fit and CV fit."""

from __future__ import annotations

import hashlib

import numpy as np
import pytest

from jobbot import semantic


class FakeModel:
    """Embeds by keyword: dimension k is 1 when the text mentions TOPICS[k]. Counts what it is asked to embed."""

    TOPICS = ["rag", "evaluation", "forecast", "vision", "sales", "python"]

    def __init__(self):
        self.calls = []

    def embed(self, texts, batch_size=64):
        self.calls.append(list(texts))
        for t in texts:
            v = np.zeros(semantic.DIM, dtype=np.float32)
            for k, word in enumerate(self.TOPICS):
                v[k] = float(word in t.lower())
            v[-1] = 0.1  # never all-zero
            yield v


@pytest.fixture
def model(monkeypatch):
    m = FakeModel()
    monkeypatch.setattr(semantic, "_model", lambda: m)
    return m


def test_chunks_keep_requirements_and_drop_boilerplate():
    text = ("We build RAG systems for clinics.\n- 5+ years of Python and evaluation of LLM outputs\n"
            "We offer great benefits and paid time off.\nShort.\nEqual opportunity employer and proud of it.")
    assert semantic.chunks(text) == ["We build RAG systems for clinics.",
                                     "5+ years of Python and evaluation of LLM outputs"]


def test_cv_chunks_cover_bullets_with_their_context(base):
    chunks = semantic.cv_chunks(base)
    assert chunks[0] == base["title"]
    assert any(c.endswith("(Adventure Works (retail))") and "retrieval-augmented" in c for c in chunks)
    assert any(c.startswith("MLOps & cloud:") for c in chunks)


def test_embeddings_are_normalized_and_cached(model):
    first = semantic.embed(["rag and python", "sales"])
    assert np.allclose(np.linalg.norm(first, axis=1), 1, atol=1e-5)
    again = semantic.embed(["sales", "rag and python", "vision"])
    assert [sorted(c) for c in model.calls] == [["rag and python", "sales"], ["vision"]]  # only new text hits the model
    assert np.allclose(again[1], first[0])


def test_fastembed_cache_keys_are_plain_text_hashes(monkeypatch):
    monkeypatch.setenv("JOBBOT_EMBEDDER", "fastembed")
    assert semantic._key("hello") == hashlib.sha1(b"hello").hexdigest()  # existing caches stay valid
    monkeypatch.setenv("JOBBOT_EMBEDDER", "hash")
    assert semantic._key("hello") != hashlib.sha1(b"hello").hexdigest()


def test_title_fit_takes_the_best_target_role(model):
    fit = semantic.title_fit(["RAG Engineer", "Sales Manager"], ["RAG evaluation engineer", "Python developer"])
    assert fit.shape == (2,) and fit[0] > fit[1]


def test_cv_fit_rewards_postings_the_cv_covers(model, base):
    rag = ("You will build RAG assistants for support teams.\n"
           "You will own evaluation of the RAG answers with a gold set.\n"
           "You will write production Python every day.")
    sales = ("You will run the enterprise sales pipeline.\n"
             "You will negotiate sales contracts with buyers.\n"
             "You will travel to sales conferences every month.")
    good, bad = semantic.cv_fit([rag, sales], base)
    assert good["fit"] > bad["fit"]
    assert good["coverage"] >= bad["coverage"]
    assert good["best"][0][2] >= good["best"][-1][2]


def test_empty_posting_scores_zero(model, base):
    assert semantic.cv_fit([""], base) == [{"fit": 0.0, "coverage": 0.0, "best": []}]


def test_hash_embedder_is_deterministic():
    a = list(semantic.HashEmbedder().embed(["machine learning engineer"]))[0]
    b = list(semantic.HashEmbedder().embed(["machine learning engineer"]))[0]
    assert np.array_equal(a, b) and a.shape == (semantic.DIM,)
