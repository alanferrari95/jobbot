# LinkedIn post rules (for the coding agent)

Goal: show the candidate as someone who understands frontier AI and ships it to production.
All posts are in English. The candidate approves every post in the dashboard before it goes out; nothing is
published automatically, and jobbot never logs in to LinkedIn.

## Two kinds of post

### `frontier`: the "written by an AI agent" series
- Topic: a new frontier-AI release, paper, benchmark or open-source project from the last few days
  (queue: `python -m jobbot tasks`, news in the dashboard).
- **Open and read the source** before writing. Every fact must come from the source; if something can't be
  verified, leave it out. Never invent numbers, quotes or dates.
- Structure: a first line that stands alone as a hook; what happened (2–3 lines); why it matters for people who put
  AI in production (cost, evaluation, reliability, MLOps); one takeaway or question. 120–220 words.
- Always end with a disclosure, for example:
  `🤖 This post was researched and drafted by Claude Code, an AI agent I set up to follow frontier AI for me. I review and approve every post.`
- Source link: in `sources` (the candidate puts it in the first comment). Add 3–5 hashtags at the end.

### `personal`: the candidate's voice
- Only the candidate's real experience: `profile/base_cv.yaml`, the topics in `profile/posts.yaml`
  (`personal_ideas`), and their public repositories.
- First person, concrete, one lesson per post. No client-confidential details: use "an energy company", "an insurer".
- 120–250 words, and no AI disclosure (the candidate edits it and makes it theirs).

## Style
- Short paragraphs, no walls of text, no "In today's fast-paced world".
- At most 2 emojis (the 🤖 in the disclosure included).
- No clickbait claims ("this changes everything"). An expert tone, but a humble one.

## How to save a draft

Write the text to `applications/posts/<slug>.md` and run:

```bash
python -m jobbot post-add --kind frontier --topic "<short topic>" --file applications/posts/<slug>.md --source <url> [--news <news_id>]
```
