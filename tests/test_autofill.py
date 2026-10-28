"""Matching an answer to a form's options (the browser part is checked by hand against real forms)."""

from __future__ import annotations

from jobbot.autofill import _pick


def test_exact_option_first():
    assert _pick(["Yes", "No"], "No") == "No"
    assert _pick(["Remote", "Remote - LATAM"], "remote - latam") == "Remote - LATAM"


def test_whole_words_only():
    countries = ["Norway", "Lebanon", "Sudan"]
    assert _pick(countries, "No") is None  # "no" is inside "Norway" and "Lebanon", but not as a word
    assert _pick(["Yes", "No"], "No, I will not need sponsorship") == "No"


def test_all_words_of_the_answer():
    places = ["Montevideo, Montevideo Department, Uruguay", "Montevideo, Minnesota, United States"]
    assert _pick(places, "Montevideo, Uruguay") == "Montevideo, Montevideo Department, Uruguay"
    assert _pick(places, "Punta del Este, Uruguay") is None


def test_list_answers_take_the_first_that_matches():
    assert _pick(["Python", "Go", "Rust"], ["Java", "python"]) == "Python"
