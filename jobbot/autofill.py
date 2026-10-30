"""Open the original application forms, fill every question we have an answer for, and STOP before submitting.

You review each tab and click Submit yourself. This script never clicks a submit button and never
touches CAPTCHAs. Demographic (EEO) questions are only answered with a "decline to answer" option.

Answers come from, in order:
1. applications/<id>/answers.yaml `labels:` exact question label -> answer (written by Claude Code from form.json)
2. personal data (name, email, phone, links…) in profile/answers.yaml
3. applications/<id>/answers.yaml `questions:` and profile/answers.yaml regex rules
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import base_cv_stem, browser_profile_dir, fixed_cv
from .formscan import open_form, scan_page

EEO = re.compile(r"gender|race|ethnic|veteran|disabilit|hispanic|latino|sexual|orientation|pronoun|transgender", re.I)
DECLINE = re.compile(r"decline|prefer not|don.?t wish|do not wish|not to (say|answer|disclose)|choose not|prefiero no", re.I)
RESUME = re.compile(r"resume|résumé|\bcv\b|curr[ií]cul", re.I)

PERSONAL_FIELDS = [
    (r"^first ?name|given name|preferred first|^nombre$", "first_name"),
    (r"^last ?name|family name|surname|apellido", "last_name"),
    (r"^(full )?name\b|^your name|nombre completo", "full_name"),
    (r"e-?mail|correo", "email"),
    (r"phone|mobile|tel[eé]fono|celular", "phone"),
    (r"linkedin", "linkedin"),
    (r"github", "github"),
    (r"website|portfolio|personal (site|url)|sitio web", "website"),
    (r"^location|^city|current location|where are you (located|based)|ubicaci[oó]n|ciudad", "location"),
    (r"current (company|employer)|^company$|empresa actual", "current_company"),
    (r"current (title|role|position)|cargo actual", "current_title"),
]


def upload_name() -> str:
    """The file name the recruiter sees: the fixed CV's own name, else the general CV's (`output_stem`).
    Never the local cv.pdf, which says nothing about who sent it."""
    fixed = fixed_cv()
    return fixed.name if fixed else f"{base_cv_stem()}.pdf"


def _norm(s: str) -> str:
    return re.sub(r"[\s*?:]+$", "", re.sub(r"\s+", " ", s or "")).strip().lower()


def answer_for(label: str, job_answers: dict, answers: dict, cover: str | None):
    labels = {_norm(k): v for k, v in (job_answers.get("labels") or {}).items()}
    if _norm(label) in labels:
        return labels[_norm(label)]
    for pattern, key in PERSONAL_FIELDS:
        if re.search(pattern, label, re.I):
            return answers["personal"].get(key)
    if cover and re.search(r"cover letter|carta de presentaci", label, re.I):
        return cover
    for q in (job_answers.get("questions") or []) + answers.get("questions", []):
        if re.search(q["match"], label, re.I) and not q.get("confirm"):
            return q["answer"]
    return None


def _words(s: str) -> list[str]:
    return re.findall(r"\w+", s.lower())


NEGATION = {"not", "no", "don", "doesn", "never", "non"}


def _pick(options: list[str], value) -> str | None:
    """Exact text first; then an option whose words contain all of the answer's words, or the other way round.
    Whole words only: "No" must not match "Norway", and "Montevideo, Uruguay" should match
    "Montevideo, Montevideo Department, Uruguay". Both sides must agree on negation: "I am not located in the
    EEA/UK" must not pick an option shown as "I am located"."""
    wanted = [str(v).strip().lower() for v in (value if isinstance(value, list) else [value])]
    for w in wanted:
        for o in options:
            if o.strip().lower() == w:
                return o
    for w in wanted:
        ww = set(_words(w))
        for o in options:
            ow = set(_words(o))
            if ww and ow and (ww <= ow or ow <= ww) and bool(ww & NEGATION) == bool(ow & NEGATION):
                return o
    return None


def _menu_options(page, el):
    """The options of THIS combobox's open menu. Greenhouse also keeps the phone field's country list
    ("Afghanistan+93", ...) as [role=option] in the page, so a page-wide query picked countries for every question."""
    for attr in ("aria-controls", "aria-owns"):
        if (lid := el.get_attribute(attr)):
            return page.locator(f'[id="{lid}"] [role=option]')
    return page.locator("[role=listbox]:visible [role=option]")


def _fill_one(page, f: dict, value, report: dict) -> None:
    el = page.locator(f'[data-jb="{f["id"]}"]')
    typ = f["type"]
    if typ in ("select",):
        choice = _pick(f.get("options", []), value)
        if choice is None:
            raise ValueError(f"no option matches {value!r}")
        el.select_option(label=choice)
    elif typ in ("radio", "checkbox"):
        values = value if isinstance(value, list) else [value]
        for v in values:
            labels = [o["label"] for o in f["options"]]
            choice = _pick(labels, v)  # all options at once, so an exact match wins over a loose earlier one
            if choice is None:
                raise ValueError(f"no option matches {v!r}")
            opt = f["options"][labels.index(choice)]
            target = page.locator(f'[data-jb="{opt["jb"]}"]')
            try:
                target.check(force=True)
            except Exception:
                target.click(force=True)
    elif typ == "combobox":
        # Read the open menu before typing: the menu filters by substring, so a stored answer worded unlike the
        # options ("Full professional proficiency." vs "Full Professional Proficiency: Fluent and...") empties it.
        el.click()
        page.wait_for_timeout(800)
        options = _menu_options(page, el)
        texts = [t.strip() for t in options.all_inner_texts()[:400]]
        choice = _pick(texts, value)
        if choice is None:  # long or async lists (cities, schools) only load matches once you type
            first = value[0] if isinstance(value, list) else value
            el.fill(re.sub(r"[.\s]+$", "", str(first)))
            page.wait_for_timeout(1500)
            options = _menu_options(page, el)
            texts = [t.strip() for t in options.all_inner_texts()[:50]]
            choice = _pick(texts, value)
        if choice is None:
            el.press("Escape")  # an open menu covers the next fields and their clicks time out
            raise ValueError(f"no option matches {value!r} (saw: {texts[:5]})")
        options.nth(texts.index(choice)).click()
    else:
        el.fill(str(value))
    report["filled"].append(f["label"])


def _accept_cookies(page) -> None:
    """Accept the cookie banner in the dedicated jobbot browser profile (Get on Board blocks the form until you do).
    This is the only consent jobbot clicks, and only on the job board you are applying through."""
    btn = page.get_by_role("button", name=re.compile("Aceptar cookies|Accept (all )?cookies", re.I)).or_(
        page.get_by_role("link", name=re.compile("Aceptar cookies|Accept (all )?cookies", re.I))).first
    try:
        if btn.count() and btn.is_visible():
            btn.click()
            page.wait_for_timeout(800)
    except Exception:
        pass


def _fill_getonboard(page, apply_url: str, cv_pdf: Path, job_answers: dict, answers: dict) -> dict:
    """Get on Board: step 1 (experience, education, English) -> step 2 (CV, salary, why). Stops before
    "Revisar y enviar" (review and send): you review the preview and send it yourself.
    Step 1 texts come from profile/answers.yaml `getonboard: {experience_html, education_html, english_level}`."""
    report = {"filled": [], "needs_you": [], "skipped_eeo": [], "file": None}
    gob = answers.get("getonboard") or {}
    labels = {_norm(k): v for k, v in (job_answers.get("labels") or {}).items()}
    page.goto(apply_url.rstrip("/") + "/applications/new" if "/applications" not in apply_url else apply_url,
              wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    _accept_cookies(page)

    if "step=basic" not in page.url and page.locator("trix-editor").count():
        if not gob.get("experience_html"):
            report["needs_you"].append("step 1: add getonboard.experience_html to profile/answers.yaml, or fill it in")
            return report
        page.evaluate("""([exp, edu]) => {
            const eds = [...document.querySelectorAll('trix-editor')];
            const find = k => eds.find(x => (x.getAttribute('input') || x.id || '').includes(k));
            (find('professional') || eds[0]).editor.loadHTML(exp);
            (find('academic') || eds[1]).editor.loadHTML(edu);
        }""", [gob["experience_html"], gob.get("education_html", "")])
        report["filled"] += ["Tell us about your experience", "Tell us about your education"]
        pick = page.get_by_text(re.compile("Selecciona tu nivel de ingl")).first
        if pick.count():
            pick.click()
            page.wait_for_timeout(600)
            level = gob.get("english_level", "Avanzado (C1)")  # option text on the (Spanish) site
            page.get_by_text(level).first.click()
            report["filled"].append(f"English level: {level}")
        page.locator("#submit-btn").click()  # "Siguiente" = next step, not the final submission
        page.wait_for_timeout(3500)
    elif "step=basic" not in page.url and page.locator("#submit-btn").count():
        # A saved draft already has step 1 filled in (no empty editors): just go to the next step.
        page.locator("#submit-btn").click()
        page.wait_for_timeout(3500)

    if "step=basic" not in page.url:
        report["needs_you"].append("did not reach step 2: check step 1 in the tab")
        return report

    # The CV is the one saved in your Get on Board profile (the general one); uploading another opens a modal.
    report["file"] = "CV from your Get on Board profile (general)"
    salary = (next((v for k, v in labels.items() if "sueldo" in k or "salary" in k), None)
              or (answers.get("salary") or {}).get("ask_usd_month"))
    if salary and page.locator("input[name='job_application[expected_salary]']").count():
        page.locator("input[name='job_application[expected_salary]']").fill(str(salary))
        report["filled"].append(f"Salary: USD {salary}/month")
    why = next((v for k, v in labels.items() if "por qué te interesa" in k or "why" in k), None)
    box = page.locator("textarea[name='job_application[reason_to_apply]']")
    if why and box.count():
        box.fill(str(why)[:1000])
        report["filled"].append("Why are you interested...?")
    elif box.count():
        report["needs_you"].append("Why are you interested...? (no answer prepared)")
        return report

    # Step 3 (only some jobs): the company's own questions. "Siguiente" (next) here only moves on; the final
    # submission is on the preview page, which you do yourself.
    page.locator("#submit-btn").click()
    page.wait_for_timeout(3500)
    if "step=questions" in page.url:
        # Each question is a block: its text, then checkboxes (option text next to each box) or a textarea.
        blocks = page.evaluate("""() => {
            const txt = n => (n && (n.innerText || n.textContent) || '').replace(/\\s+/g, ' ').trim();
            const out = [];
            document.querySelectorAll('input[type=checkbox], textarea').forEach((el, i) => {
                if (!el.offsetParent && el.type !== 'checkbox') return;
                el.dataset.jbq = 'q' + i;
                let block = el.parentElement;
                while (block && !/\\?|\\.$/.test(txt(block.querySelector('h1,h2,h3,h4,h5,label,p,strong,legend,div')))) block = block.parentElement;
                let q = '', n = el;
                for (let k = 0; k < 8 && n; k++, n = n.parentElement) {
                    const cand = [...n.querySelectorAll('h1,h2,h3,h4,h5,strong,legend,p,label,div')]
                        .map(txt).find(t => t.length > 15 && (t.endsWith('?') || t.endsWith('.')) && !t.startsWith('Realic') && !t.startsWith('Cre') && !t.startsWith('Orquest') && !t.startsWith('Integr') && !t.startsWith('Evalu') && !t.startsWith('Implement'));
                    if (cand) { q = cand; break; }
                }
                const opt = el.type === 'checkbox' ? (txt(el.closest('label')) || txt(el.parentElement)) : '';
                out.push({ id: 'q' + i, type: el.type === 'checkbox' ? 'checkbox' : 'textarea', question: q, option: opt });
            });
            return out;
        }""")
        wanted = {_norm(k): v for k, v in labels.items()}
        done = set()
        for b in blocks:
            value = next((v for k, v in wanted.items() if k and (k in _norm(b["question"]) or _norm(b["question"]) in k)), None)
            if b["type"] == "checkbox":
                if isinstance(value, list) and any(_norm(o) == _norm(b["option"]) or _norm(o) in _norm(b["option"]) for o in value):
                    page.locator(f'[data-jbq="{b["id"]}"]').check(force=True)
                    report["filled"].append(f"✓ {b['option'][:60]}")
                done.add(b["question"]) if value is not None else None
            elif isinstance(value, str) and value:
                page.locator(f'[data-jbq="{b["id"]}"]').fill(value)
                report["filled"].append(b["question"][:60])
                done.add(b["question"])
        for q in {b["question"] for b in blocks} - done:
            report["needs_you"].append(f"Company question: {q[:90]}")
        report["stopped_at"] = "questions: review them and click 'Siguiente: Revisar y enviar'"
    else:
        report["stopped_at"] = "preview: review it and send it"
    return report


def _fill_page(page, apply_url: str, cv_pdf: Path, job_answers: dict, answers: dict, cover: str | None) -> dict:
    if "getonbrd.com" in apply_url:
        return _fill_getonboard(page, apply_url, cv_pdf, job_answers, answers)
    report = {"filled": [], "needs_you": [], "skipped_eeo": [], "file": None}
    open_form(page, apply_url)

    # 1. Resume first: some ATS re-render the form after an upload. Never use "Autofill from resume" inputs.
    fields = scan_page(page)
    files = [f for f in fields if f["type"] == "file" and not re.search(r"autofill", f["label"], re.I)]
    target = next((f for f in files if RESUME.search(f["label"])), files[0] if files else None)
    if target:
        page.locator(f'[data-jb="{target["id"]}"]').set_input_files(
            {"name": upload_name(), "mimeType": "application/pdf",
             "buffer": cv_pdf.read_bytes()})
        report["file"] = target["label"]
        page.wait_for_timeout(3000)

    # 2. Every other question, found again after the upload.
    fields = scan_page(page)
    text_types = {"text", "email", "tel", "url", "number", "textarea"}
    for f in fields:
        if f["type"] == "file":
            continue
        label = f["label"]
        if not label:
            continue
        try:
            if EEO.search(label):
                opts = [o["label"] if isinstance(o, dict) else o for o in f.get("options", [])]
                decline = next((o for o in opts if DECLINE.search(o)), None)
                if decline:
                    _fill_one(page, f, decline, report)
                else:
                    report["skipped_eeo"].append(label)
                continue
            if f["type"] in text_types and page.locator(f'[data-jb="{f["id"]}"]').input_value():
                continue
            value = answer_for(label, job_answers, answers, cover)
            if value in (None, ""):
                if f.get("required") or not re.match(r"if (other|yes|no)|si (otro|elegiste)", label, re.I):
                    report["needs_you"].append(label + ("  (required)" if f.get("required") else ""))
                continue
            _fill_one(page, f, value, report)
        except Exception as e:
            report["needs_you"].append(f"{label}  ({e.__class__.__name__}: {str(e)[:60]})")

    # 3. Re-check text fields after the page settles and refill anything a re-render cleared.
    page.wait_for_timeout(1500)
    for f in scan_page(page):
        if f["type"] in text_types and f["label"] in report["filled"]:
            el = page.locator(f'[data-jb="{f["id"]}"]')
            if not el.input_value():
                value = answer_for(f["label"], job_answers, answers, cover)
                if value:
                    el.fill(str(value))

    page.add_style_tag(content="button[type=submit], input[type=submit]"
                               " { outline: 4px solid #d93025 !important; }")
    return report


def _launch(pw, headless: bool):
    return pw.chromium.launch_persistent_context(
        user_data_dir=str(browser_profile_dir()), channel="chrome", headless=headless,
        viewport={"width": 1280, "height": 900})


def run(apply_url: str, cv_pdf: Path, answers: dict, cover_letter: str | None = None,
        headless: bool = False, interactive: bool = True, job_answers: dict | None = None) -> dict:
    """One application: prefill it and wait for you to review and submit."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = _launch(pw, headless)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        report = _fill_page(page, apply_url, cv_pdf, job_answers or {}, answers, cover_letter)
        print("\n=== Form filled. Nothing was submitted. ===")
        print(f"Filled ({len(report['filled'])}): " + "; ".join(report["filled"]))
        print("Needs you:\n  - " + "\n  - ".join(report["needs_you"] or ["nothing"]))
        print("\nReview every field in the browser and click Submit yourself.")
        if interactive:
            input("Press Enter here when you're done (the browser will close)... ")
            report["submitted"] = input("Did you submit the application? [y/N] ").strip().lower() == "y"
            ctx.close()
        else:
            ctx.wait_for_event("close", timeout=0)
            report["submitted"] = None
    return report


def open_batch(items: list[dict], answers: dict, on_filled) -> None:
    """Prefill every ready application, each in its own tab, then wait until you close the browser.
    items: [{id, url, cv_pdf, job_answers, cover}]. Nothing is submitted."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx = _launch(pw, headless=False)
        first = ctx.pages[0] if ctx.pages else None
        for n, it in enumerate(items):
            page = first if (n == 0 and first) else ctx.new_page()
            try:
                report = _fill_page(page, it["url"], Path(it["cv_pdf"]), it.get("job_answers") or {},
                                    answers, it.get("cover"))
            except Exception as e:
                report = {"error": str(e), "filled": [], "needs_you": ["page failed to load: open it manually"]}
            on_filled(it["id"], report)
        ctx.wait_for_event("close", timeout=0)
