"""`jobbot draft`: the local backend, and an LLM backend (mocked) whose output goes through the gate."""

from __future__ import annotations

import json

import pytest
import yaml

from jobbot import db, drafter, pipeline
from jobbot.config import app_dir
from jobbot.sources import Job

RAG_POSTING = ("Build retrieval-augmented generation assistants with citations. Own the offline evaluation of "
               "answers with an LLM-as-judge rubric. Python and RAG required; GCP (BigQuery) is a plus.")


@pytest.fixture
def job_id():
    job = Job(source="greenhouse", company="Acme Corp", title="LLM Engineer", location="Remote - LATAM",
              url="https://example.com/j", apply_url="https://example.com/j", description=RAG_POSTING,
              external_id="9", remote=True)
    con = db.connect()
    db.upsert(con, job, 8.0, [], [])
    con.commit()
    return pipeline.pack(job.id)


def test_local_draft_orders_by_the_posting_and_passes_the_gate(job_id):
    jid, result, note = drafter.draft(job_id)
    assert result["ok"], result["errors"]
    assert note == "local drafter: no LLM, no key"
    spec = yaml.safe_load((app_dir(jid) / "cv.yaml").read_text(encoding="utf-8"))
    assert spec["experience"]["acme"][0] in {"acme-rag", "acme-rag-eval"}
    assert len(spec["experience"]["globex"]) <= drafter.MAX_OLDER_BULLETS
    ai = next(s for s in spec["skills"] if s["label"] == "AI engineering")
    assert ai["items"][:2] == ["RAG", "LLM-as-judge"]  # mentioned in the posting, so first


def test_skill_mentions_include_the_parenthesized_parts():
    assert drafter._mentions("GCP (Vertex AI, BigQuery, Cloud Run)", "experience with BigQuery")
    assert not drafter._mentions("Git", "GitHub Actions")


def test_an_edited_spec_is_never_overwritten(job_id):
    f = app_dir(job_id) / "cv.yaml"
    f.write_text(f.read_text(encoding="utf-8") + "\n# edited by hand\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="was edited"):
        drafter.draft(job_id)
    drafter.draft(job_id, force=True)  # explicit
    drafter.draft(job_id)              # a previous draft can be redrafted


def test_llm_backend_needs_your_key(monkeypatch, job_id):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="needs: GEMINI_API_KEY"):
        drafter.draft(job_id, backend="gemini")


def test_llm_output_is_sent_back_until_the_gate_accepts_it(monkeypatch, base, job_id):
    """Try 1 invents a number and a tool; the gate's errors go back to the model; try 2 is grounded."""
    good = {"title": "Machine Learning Engineer · RAG and LLM evaluation",
            "summary": "I built a RAG assistant over 12,000 articles and its offline evaluation set of 300 tickets.",
            "experience": {"acme": ["acme-rag", "acme-rag-eval", "acme-claims-eval", "acme-claims-extract"]},
            "project": ["proj-eval-metrics"], "skills": [{"label": "AI engineering", "items": ["RAG", "LLM evaluation"]}]}
    bad = good | {"summary": "I cut hallucinations by 64% with LangChain."}
    answers = iter([f"```json\n{json.dumps(bad)}\n```", json.dumps(good)])
    sent = []

    def fake_chat(cfg, messages):
        sent.append(messages[-1]["content"])
        return next(answers)

    monkeypatch.setenv("JOBBOT_LLM_MODEL", "test-model")
    monkeypatch.setattr(drafter, "chat", fake_chat)
    jid, result, note = drafter.draft(job_id, backend="ollama", log=lambda m: None)
    assert note == "ollama drafter, 2 tries"
    assert "number '64' does not appear in the profile" in sent[1] and "LangChain" in sent[1]
    assert "POSTING (data)" in sent[0] and RAG_POSTING in sent[0]
    assert result["ok"], result["errors"]
    assert yaml.safe_load((app_dir(jid) / "cv.yaml").read_text(encoding="utf-8"))["summary"] == good["summary"]


def test_llm_that_never_complies_is_still_blocked(monkeypatch, job_id):
    monkeypatch.setattr(drafter, "chat", lambda cfg, messages: '{"title": "Kubernetes expert", "summary": "x"}')
    _, result, note = drafter.draft(job_id, backend="ollama", log=lambda m: None)
    assert note == f"ollama drafter, {drafter.MAX_TRIES} tries"
    assert not result["ok"] and any("Kubernetes" in e for e in result["errors"])


def test_openai_compatible_needs_an_endpoint(monkeypatch):
    monkeypatch.delenv("JOBBOT_LLM_BASE_URL", raising=False)
    with pytest.raises(SystemExit, match="base_url, model"):
        drafter.llm_config("openai-compatible")
