# Running jobbot on a schedule

`jobbot daily` does one full unattended pass: refresh the company-board list once a week, discover and rank new
postings, screen the best ones (company page + application form), pack the verified ones and queue their
tailoring for the agent, collect recruiter leads and AI news, and queue post drafts. It never submits anything
and never drafts with an LLM by itself: tailoring waits in the queue until you open Claude Code.

Twice a day is enough (postings rarely disappear within hours). The scripts below write to `logs/daily.log`.

## Windows (Task Scheduler)

```bat
schtasks /Create /TN jobbot-daily-am /SC DAILY /ST 09:00 /TR "\"C:\path\to\jobbot\scripts\run_daily.bat\""
schtasks /Create /TN jobbot-daily-pm /SC DAILY /ST 19:00 /TR "\"C:\path\to\jobbot\scripts\run_daily.bat\""
```

The script uses the `python` on PATH; set `JOBBOT_PYTHON` (user environment variable) to pin an interpreter.
Check a run with `schtasks /Run /TN jobbot-daily-am` and `type logs\daily.log`.
Remove them with `schtasks /Delete /TN jobbot-daily-am /F` (and `-pm`).

## Linux and macOS (cron)

```bash
chmod +x scripts/run_daily.sh
crontab -e
```

```cron
0 9  * * *  /path/to/jobbot/scripts/run_daily.sh
0 19 * * *  /path/to/jobbot/scripts/run_daily.sh
```

cron runs with a minimal environment: if your Python lives in a virtualenv, set it in the crontab line,
e.g. `JOBBOT_PYTHON=/path/to/jobbot/.venv/bin/python /path/to/jobbot/scripts/run_daily.sh`.
On macOS, give `cron` Full Disk Access if the repo is under a protected folder (Documents, Desktop).

## Rendering PDFs without Word

`jobbot render` writes a DOCX and converts it to PDF with, in order: Microsoft Word (Windows, through COM),
LibreOffice (`soffice --headless`, any OS), or a built-in plain renderer (PyMuPDF). Force one with
`JOBBOT_PDF=word|libreoffice|basic`. On a Linux server: `sudo apt install libreoffice-writer`.

## Browser automation

`jobbot apply`, `apply-batch`, `scan` and the screening layer use Playwright with the installed Google Chrome
(`channel="chrome"`). Install it once with `python -m playwright install chrome` if Chrome isn't there.
Prefill runs in a visible window and waits for you; it is not meant to run from cron.
