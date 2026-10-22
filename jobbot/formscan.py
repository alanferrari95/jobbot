"""Read-only scan of an application form: every question with its type, options and whether it is required.

The result (applications/<id>/form.json) lets Claude Code write an exact answer for every label
before you open the form. Nothing is typed or submitted here.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .config import browser_profile_dir

APPLY_BUTTONS = [
    "a:has-text('Apply for this job')", "button:has-text('Apply for this job')",
    "a:has-text('Apply now')", "button:has-text('Apply now')",
    "a:has-text('Postular')", "button:has-text('Postular')", "a:has-text('Postúlate')",
    "a:has-text('Aplicar')", "button:has-text('Aplicar')",
    "a:has-text('Apply')", "button:has-text('Apply')",
]

# Groups radios/checkboxes by question; tags every control with data-jb so the filler can find it again.
SCAN_JS = r"""
() => {
  const txt = n => (n && (n.innerText || n.textContent) || '').replace(/\s+/g, ' ').trim();
  const visible = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el);
    return (r.width > 0 || r.height > 0 || el.type === 'file') && s.visibility !== 'hidden' && s.display !== 'none'; };
  const labelOf = el => {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (txt(l)) return txt(l); }
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    const lb = el.getAttribute('aria-labelledby');
    if (lb) { const t = lb.split(' ').map(i => txt(document.getElementById(i))).join(' ').trim(); if (t) return t; }
    const fs = el.closest('fieldset'); if (fs && txt(fs.querySelector('legend'))) return txt(fs.querySelector('legend'));
    let n = el.parentElement;
    for (let i = 0; i < 6 && n; i++, n = n.parentElement) {
      const l = n.querySelector('label, legend, [class*="label" i], [class*="question" i], [class*="title" i]');
      if (l && txt(l) && !l.contains(el)) return txt(l);
    }
    return el.getAttribute('placeholder') || el.name || '';
  };
  const groupOf = el => el.closest('fieldset, [role=radiogroup], [role=group], [class*="question" i], [class*="field" i]') || el.parentElement;
  const out = [], groups = new Map();
  let i = 0;
  document.querySelectorAll('input, textarea, select, [role=combobox]').forEach(el => {
    const tag = el.tagName;
    let type = tag === 'SELECT' ? 'select' : tag === 'TEXTAREA' ? 'textarea' : (el.getAttribute('role') === 'combobox' && tag !== 'INPUT') ? 'combobox' : (el.type || 'text');
    if (tag === 'INPUT' && el.getAttribute('role') === 'combobox') type = 'combobox';
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(type) || !visible(el)) return;
    el.dataset.jb = el.dataset.jb || ('f' + (i++));
    const required = el.required || el.getAttribute('aria-required') === 'true';
    if (type === 'radio' || type === 'checkbox') {
      const g = groupOf(el);
      if (!groups.has(g)) {
        const q = { id: el.dataset.jb, label: labelOf(g.querySelector('input') === el ? g : el), type, options: [], required };
        const legend = g.querySelector('legend, [class*="label" i], [class*="question" i]');
        if (legend && txt(legend)) q.label = txt(legend);
        groups.set(g, q); out.push(q);
      }
      const q = groups.get(g);
      const opt = (el.id && txt(document.querySelector(`label[for="${CSS.escape(el.id)}"]`))) || txt(el.closest('label')) || txt(el.parentElement) || el.value;
      q.options.push({ label: opt, jb: el.dataset.jb });
      q.required = q.required || required;
      return;
    }
    const f = { id: el.dataset.jb, label: labelOf(el), type, required };
    if (type === 'select') f.options = [...el.options].map(o => o.text.trim()).filter(t => t && !/^(select|--|choose|seleccion)/i.test(t));
    out.push(f);
  });
  out.forEach(f => { if (/\*\s*$/.test(f.label)) f.required = true; f.label = f.label.replace(/\s*\*\s*$/, '').slice(0, 300); });
  return out;
}
"""


def open_form(page, url: str) -> None:
    """Go to the posting and, if the form is behind an Apply/Postular button, open it."""
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)
    if page.locator("input[type=file], textarea").count():
        return
    for sel in APPLY_BUTTONS:
        btn = page.locator(sel).first
        try:
            if btn.count() and btn.is_visible():
                btn.click()
                page.wait_for_timeout(3000)
                return
        except Exception:
            continue


def scan_page(page) -> list[dict]:
    fields = page.evaluate(SCAN_JS)
    for f in fields:
        if f.get("options") and isinstance(f["options"][0], dict):
            f["option_labels"] = [o["label"] for o in f["options"]]
    return fields


def scan(url: str, out: Path, headless: bool = True, needs_login: bool = False) -> dict:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        if needs_login:  # sites like Get on Board need your saved session (`jobbot login`)
            ctx = pw.chromium.launch_persistent_context(user_data_dir=str(browser_profile_dir()),
                                                        channel="chrome", headless=headless)
        else:
            ctx = pw.chromium.launch(channel="chrome", headless=headless).new_context()
        page = ctx.new_page()
        try:
            open_form(page, url)
            fields = scan_page(page)
            final_url = page.url
        finally:
            ctx.close()
    public = [{k: v for k, v in f.items() if k not in ("id", "options")} | (
        {"options": f.get("option_labels") or f.get("options")} if f.get("options") else {}) for f in fields]
    data = {"url": url, "form_url": final_url, "scanned_at": time.strftime("%Y-%m-%d %H:%M"), "fields": public}
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return data
