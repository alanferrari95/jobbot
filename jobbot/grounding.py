"""The grounding gate: checks that what the agent wrote is true, complete and ATS-readable before applying.

The agent (Claude Code, following .claude/skills/tailor-cv) writes three things per job: cv.yaml (a tailoring
spec over profile/base_cv.yaml), answers.yaml (form answers) and optionally cover_letter.md. This module checks
them deterministically, outside the model:

1. Spec: every bullet is referenced by id (the agent cannot write new bullets), every skill item is copied from
   the profile, and the free text (title, summary) contains no number, no `never_claim` technology and no known
   technology that the profile doesn't mention.
2. Answers and cover letter: same number and technology rules. Numbers may also come from the job posting
   ("your Series B in 2024"), technologies may not. A pay expectation is a decision, not a fact: its new numbers
   are a warning to confirm.
3. Rendered PDF: current, at most 2 pages, text-extractable, with contact details and the standard sections.
4. Keyword coverage against the posting (warnings only).

Errors block `jobbot apply` (and the dashboard's prefill); warnings are shown for review.
"""

from __future__ import annotations

import re
from pathlib import Path

import pymupdf
import yaml

from .cv import resolve
from .match import tech_pattern

# Keywords looked for in job descriptions (besides base skills and never_claim terms).
VOCAB = [
    "Python", "SQL", "PyTorch", "TensorFlow", "Keras", "scikit-learn", "XGBoost", "LLM", "LLMs", "RAG",
    "LangChain", "LlamaIndex", "vector database", "embeddings", "fine-tuning", "prompt engineering",
    "evaluation", "MLOps", "CI/CD", "Docker", "Terraform", "GCP", "Google Cloud", "Vertex AI", "BigQuery",
    "Azure", "FastAPI", "computer vision", "OCR", "NLP", "transformers", "Hugging Face", "A/B testing",
    "statistics", "monitoring", "observability", "feature store", "Kubeflow", "Cloud Run", "Pub/Sub",
    "generative AI", "GenAI", "agents", "document", "pandas", "NumPy", "Git", "production", "deployment",
]

# Concrete technologies. One of these in free text that the profile never mentions is an ungrounded claim.
TECH_TERMS = [
    "Python", "SQL", "Scala", "Java", "JavaScript", "TypeScript", "Go", "Golang", "Rust", "C++", "C#", ".NET",
    "PyTorch", "TensorFlow", "Keras", "JAX", "scikit-learn", "XGBoost", "LightGBM", "CatBoost", "ONNX", "TensorRT",
    "vLLM", "Hugging Face", "LangChain", "LangGraph", "LlamaIndex", "LangSmith", "DSPy", "DeepEval", "TruLens",
    "Weights & Biases", "MLflow", "Kubeflow", "Airflow", "Prefect", "Dagster", "dbt", "Spark", "PySpark", "Ray",
    "Dask", "Kafka", "Flink", "Databricks", "Snowflake", "BigQuery", "Redshift", "PostgreSQL", "MySQL", "MongoDB",
    "Redis", "Elasticsearch", "OpenSearch", "Pinecone", "Weaviate", "Qdrant", "Milvus", "Chroma", "pgvector",
    "FAISS", "Docker", "Kubernetes", "K8s", "Helm", "Terraform", "Pulumi", "Ansible", "AWS", "SageMaker", "Bedrock",
    "GCP", "Vertex AI", "Cloud Run", "Azure", "Azure ML", "FastAPI", "Flask", "Django", "React",
    "Node.js", "GraphQL", "Playwright", "Selenium", "OpenCV", "YOLO", "Tesseract", "spaCy", "NLTK", "Gemini",
    "GPT-4", "Claude", "Llama", "Mistral", "Grafana", "Prometheus", "Datadog", "GitHub Actions", "Jenkins",
]

REQUIRED_SECTIONS = ["SUMMARY", "EXPERIENCE", "SKILLS", "EDUCATION"]
PAY_QUESTION = re.compile(r"salary|compensation|pay|rate|sueldo|salario|remuneraci|pretensi", re.I)
MIN_PRIMARY_BULLETS = 4


def _numbers(text: str) -> set[str]:
    """Numbers in a text, with thousands separators removed ("6,000" and "6.000" -> "6000").
    Digits glued to letters before them are names, not claims (K8s, YOLOv11, GPT4)."""
    return {re.sub(r"[.,](?=\d{3}\b)", "", n) for n in re.findall(r"(?<![A-Za-z\d.,])\d+(?:[.,]\d+)*", text)}


def _has(term: str, text: str) -> bool:
    return bool(re.search(rf"(?<![\w-]){re.escape(term)}(?![\w-])", text, re.I))


def _mentions(term: str, text: str) -> bool:
    """Whole-word, case-sensitive match: technology names are proper nouns ("Ray", "Spark" vs "spark")."""
    if term == "Go":
        return bool(tech_pattern(term).search(text))
    return bool(re.search(rf"(?<![\w-]){re.escape(term)}(?![\w])", text))


def job_keywords(description: str, base: dict) -> list[str]:
    vocab = list(VOCAB) + list(base["never_claim"])
    for s in base["skills"]:
        for item in s["items"]:
            vocab.append(re.sub(r"\s*\(.*\)", "", item))
    seen, out = set(), []
    never = {t.lower() for t in base["never_claim"]}
    for term in vocab:
        key = term.lower()
        found = tech_pattern(term).search(description) if key in never else _has(term, description)
        if key not in seen and found:
            seen.add(key)
            out.append(term)
    return out


def profile_text(base: dict, profile_answers: dict | None = None) -> str:
    """Everything the candidate has stated as true: the base CV plus the standard form answers."""
    return yaml.safe_dump(base, allow_unicode=True) + yaml.safe_dump(profile_answers or {}, allow_unicode=True)


def check_text(where: str, text: str, base: dict, grounded: str, extra_numbers: str = "") -> list[str]:
    """Errors for numbers and technologies in `text` that the profile (`grounded`) doesn't back."""
    errors = []
    for n in sorted(_numbers(text) - _numbers(grounded) - _numbers(extra_numbers)):
        errors.append(f"{where}: number {n!r} does not appear in the profile")
    never = {t.lower() for t in base["never_claim"]}
    for t in base["never_claim"]:
        if tech_pattern(t).search(text):
            errors.append(f"{where}: mentions {t}, which is in never_claim (not used by the candidate)")
    for t in TECH_TERMS:
        if t.lower() not in never and _mentions(t, text) and not _has(t, grounded):
            errors.append(f"{where}: mentions {t}, which the profile doesn't mention")
    return errors


def check_spec(base: dict, spec: dict, grounded: str) -> tuple[dict | None, list[str], list[str]]:
    """Resolve a tailoring spec and check it. Returns (resolved cv or None, errors, warnings)."""
    try:
        cv = resolve(base, spec)
    except KeyError as e:
        return None, [f"cv.yaml: {e}"], []
    errors: list[str] = []
    warnings: list[str] = []
    base_items = {i for s in base["skills"] for i in s["items"]}
    for s in cv["skills"]:
        for item in s["items"]:
            if item not in base_items:
                errors.append(f"skill not in base_cv.yaml: {item!r}")
    errors += check_text("title/summary", f"{cv['title']}\n{cv['summary']}", base, grounded)
    if len(cv["summary"]) > 900:
        warnings.append(f"summary is long ({len(cv['summary'])} chars); aim for < 900")
    primary, base_primary = cv["experience"][0], base["experience"][0]
    have = sum(len(g["bullets"]) for g in primary["groups"])
    if have < min(MIN_PRIMARY_BULLETS, sum(len(g["bullets"]) for g in base_primary["groups"])):
        warnings.append(f"{primary['company']} has fewer than {MIN_PRIMARY_BULLETS} bullets; it is the strongest role")
    return cv, errors, warnings


def _answer_texts(data) -> list[tuple[str, str]]:
    """(question, answer) pairs from a per-job answers.yaml (labels/questions format or the old list format)."""
    if isinstance(data, list):
        data = {"questions": data}
    data = data or {}
    out = [(str(k), v) for k, v in (data.get("labels") or {}).items()]
    out += [(str(q.get("match", "")), q.get("answer")) for q in data.get("questions") or []]
    flat = []
    for q, a in out:
        for v in a if isinstance(a, list) else [a]:
            if v not in (None, ""):
                flat.append((q, str(v)))
    return flat


def check_answers(app_dir: Path, base: dict, grounded: str, description: str) -> tuple[list[str], list[str]]:
    """(errors, warnings). A pay expectation is the candidate's decision, not a fact: its numbers are shown as a
    warning to confirm instead of failing the gate. Technologies in it are still checked."""
    errors, warnings = [], []
    f = app_dir / "answers.yaml"
    if f.exists():
        for q, a in _answer_texts(yaml.safe_load(f.read_text(encoding="utf-8"))):
            where = f"answers.yaml [{q[:40]}]"
            if PAY_QUESTION.search(q):
                if new := sorted(_numbers(a) - _numbers(grounded)):
                    warnings.append(f"{where}: pay expectation {', '.join(new)}: confirm it is what you want to ask")
                errors += check_text(where, a, base, grounded, extra_numbers=a)
            else:
                errors += check_text(where, a, base, grounded, extra_numbers=description + "\n" + q)
    f = app_dir / "cover_letter.md"
    if f.exists():
        errors += check_text("cover_letter.md", f.read_text(encoding="utf-8"), base, grounded, extra_numbers=description)
    return errors, warnings


def check_pdf(pdf_path: Path, spec_path: Path, base: dict) -> tuple[list[str], int]:
    """Errors for the rendered PDF, and its page count."""
    if not pdf_path.exists():
        return ["cv.pdf missing: run `jobbot render <id>`"], 0
    errors = []
    if pdf_path.stat().st_mtime < spec_path.stat().st_mtime:
        errors.append("cv.pdf is older than cv.yaml: run `jobbot render <id>` again")
    doc = pymupdf.open(pdf_path)
    text = "\n".join(p.get_text() for p in doc)
    pages = len(doc)
    doc.close()
    if pages > 2:
        errors.append(f"CV has {pages} pages; keep it to 2")
    if len(text) < 1500:
        errors.append("almost no text extracted from the PDF (would fail an ATS)")
    c = base["contact"]
    expected = [("email", c["email"]), ("phone", c.get("phone")),
                ("LinkedIn", re.sub(r"^https?://(www\.)?", "", c.get("linkedin") or "").rstrip("/"))]
    for label, value in expected:
        if value and value not in text:
            errors.append(f"{label} not found in PDF text")
    for sec in REQUIRED_SECTIONS:
        if sec not in text:
            errors.append(f"section {sec} not found in PDF text")
    for t in base["never_claim"]:
        if tech_pattern(t).search(text):
            errors.append(f"PDF mentions {t}, which is in never_claim (not used by the candidate)")
    if re.search(r"\[(CONFIRM|TODO|X|Y)[^\]]*\]", text):
        errors.append("placeholder left in the PDF")
    return errors, pages


def verify(app_dir: Path, base: dict, description: str, profile_answers: dict | None = None) -> dict:
    spec_path, pdf_path = app_dir / "cv.yaml", app_dir / "cv.pdf"
    grounded = profile_text(base, profile_answers)
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8")) or {}
    cv, errors, warnings = check_spec(base, spec, grounded)
    if cv is None:
        return {"ok": False, "errors": errors, "warnings": [], "keywords": {}}
    answer_errors, answer_warnings = check_answers(app_dir, base, grounded, description)
    errors += answer_errors
    warnings += answer_warnings
    pdf_errors, pages = check_pdf(pdf_path, spec_path, base)
    errors += pdf_errors
    if not pages:
        return {"ok": False, "errors": errors, "warnings": warnings, "keywords": {}}
    doc = pymupdf.open(pdf_path)
    text = "\n".join(p.get_text() for p in doc)
    doc.close()

    # Keyword coverage against the job description.
    never = base["never_claim"]
    base_text = yaml.safe_dump(base, allow_unicode=True)
    wanted = job_keywords(description, base)
    covered = [k for k in wanted if _has(k, text)]
    not_claimable = [k for k in wanted if any(k.lower() == t.lower() for t in never)]
    missing_in_cv = [k for k in wanted if k not in covered and k not in not_claimable
                     and _has(k, base_text)]
    gaps = [k for k in wanted if k not in covered and k not in not_claimable and k not in missing_in_cv]
    coverage = len(covered) / max(1, len(wanted) - len(not_claimable))
    if missing_in_cv:
        warnings.append("job keywords you DO have but the CV doesn't show: " + ", ".join(missing_in_cv))
    if coverage < 0.6:
        warnings.append(f"keyword coverage {coverage:.0%} is low")

    return {
        "ok": not errors, "errors": errors, "warnings": warnings,
        "keywords": {"coverage": round(coverage, 2), "covered": covered, "missing_but_true": missing_in_cv,
                     "not_in_profile": gaps, "never_claim_asked": not_claimable},
        "pages": pages,
    }


def report_md(job_id: str, result: dict) -> str:
    k = result.get("keywords", {})
    lines = [f"# Verify: {job_id}", "", f"**Result: {'PASS' if result['ok'] else 'FAIL'}**", ""]
    if result["errors"]:
        lines += ["## Errors", *[f"- {e}" for e in result["errors"]], ""]
    if result["warnings"]:
        lines += ["## Warnings", *[f"- {w}" for w in result["warnings"]], ""]
    if k:
        lines += [
            "## Keywords",
            f"- Coverage: {k['coverage']:.0%}",
            f"- In CV: {', '.join(k['covered']) or '-'}",
            f"- True but not shown: {', '.join(k['missing_but_true']) or '-'}",
            f"- Not in profile (don't add): {', '.join(k['not_in_profile']) or '-'}",
            f"- Asked, never claim: {', '.join(k['never_claim_asked']) or '-'}",
        ]
    return "\n".join(lines) + "\n"
