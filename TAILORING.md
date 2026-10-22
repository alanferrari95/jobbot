# Tailoring playbook

The playbook the coding agent follows to tailor a CV and its form answers lives in
[.claude/skills/tailor-cv/SKILL.md](.claude/skills/tailor-cv/SKILL.md), as a Claude Code skill:

1. **Drafter**: reorders bullet ids, rewrites the title and summary, writes form answers, using only facts from
   `profile/base_cv.yaml` and `profile/answers.yaml`.
2. **Reviewer**: an independent pass (a subagent that sees the posting and the output, not the drafter's
   reasoning) reviews it as the hiring manager: eligibility gates, 6-second scan, grounding, keywords, answers.
3. **Gate**: `python -m jobbot verify <id>` ([jobbot/grounding.py](jobbot/grounding.py)) blocks anything the
   profile doesn't back, deterministically.

Any other agent can follow the same file: it is plain Markdown with no Claude-specific steps besides the subagent.
