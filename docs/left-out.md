# What is left out of this repository, on purpose

jobbot runs from a working folder that also holds my real data. The repository's `.gitignore` ignores everything
by default and only lists what is public, so none of the following can be committed by accident.

| Left out | Why |
|---|---|
| `profile/` (my real `base_cv.yaml`, `answers.yaml` with salary expectations and phone, `targets.yaml`, `verify_testset.yaml`, `companies_auto.yaml`, `posts.yaml`) | Personal data. The repo ships a fictional profile with the same format in `examples/profile/`. |
| `applications/` (tracker database, one folder per job with tailored CVs, answers, form scans) | Personal data and third-party postings. |
| `cv/`, `linkedin/`, `prep/`, `data/`, `logs/` | My CVs, LinkedIn drafts, interview preparation, documents and run logs. |
| `.browser-profile/` | The Chrome profile used for prefill: it holds cookies and logged-in sessions. |
| `.models/` | The downloaded embedding model (~240 MB); fastembed fetches it on first use. |
| `HANDOFF.md` and my local launchers | Notes about my job search, and Windows launchers with machine-specific paths (generic ones are in `scripts/`). |
| The screening test set (`verify_testset.yaml`) | It lists the real jobs I applied to and rejected. `jobbot verify-test` runs against your own. |
| Real postings in the demo | The demo uses fictional companies on `example.com`; only the parser tests use trimmed real API responses. |
| My local job sources (`profile/local_sources.py`) | One of them scrapes a job board's HTML, which its terms don't allow; it is not part of the public code. |
| API keys and model weights | `jobbot draft` can use Gemini, Ollama or any OpenAI-compatible server, but you bring the key or the GPU. The default drafter needs neither. |
