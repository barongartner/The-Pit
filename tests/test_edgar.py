"""EDGAR feed tests.

The fixture is a real captured response from
``browse-edgar?action=getcurrent&type=4``, saved 2026-07-29. It is 73% noise:
100 entries, of which only 10 are actual Form 4 insider filings and 73 are
424B2 prospectus supplements that SEC returned because its ``type=`` parameter
does a **prefix** match. That contamination is the whole reason these tests
exist, so the fixture is kept exactly as it came off the wire.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from thepit.core.clock import FixedClock
from thepit.feeds.edgar import EdgarNewsFeed

FIXTURE = Path(__file__).parent / "fixtures" / "edgar_form4.atom.xml"


@pytest.fixture
def atom() -> str:
    return FIXTURE.read_text(encoding="utf-8", errors="replace")


@pytest.fixture
def feed() -> EdgarNewsFeed:
    # No HTTP client: _parse_atom is pure and that is the point of testing it
    # directly rather than through a mocked transport.
    return EdgarNewsFeed(http=None, clock=FixedClock(1_800_000_000_000))  # type: ignore[arg-type]


def _accessions_for_form(atom: str, form: str) -> set[str]:
    """Distinct filings, as opposed to distinct <entry> elements."""
    out = set()
    for entry in re.findall(r"<entry>(.*?)</entry>", atom, re.S):
        title = re.search(r"<title>(.*?)</title>", entry, re.S)
        accn = re.search(r"accession-number=([\d-]+)", entry)
        if not title or not accn:
            continue
        t = re.match(r"^(.+?)\s*-\s*(.+?)\s*\((\d{10})\)", title.group(1))
        if t and t.group(1).strip() == form:
            out.add(accn.group(1))
    return out


def _ciks_for_form(atom: str, form: str) -> list[str]:
    out = []
    for entry in re.findall(r"<entry>(.*?)</entry>", atom, re.S):
        m = re.search(r"<title>(.*?)</title>", entry, re.S)
        if not m:
            continue
        t = re.match(r"^(.+?)\s*-\s*(.+?)\s*\((\d{10})\)", m.group(1))
        if t and t.group(1).strip() == form:
            out.append(t.group(3))
    return out


# ---------------------------------------------------------------------------
# The prefix-match bug. This is the test that matters.
# ---------------------------------------------------------------------------


def test_form_4_request_does_not_leak_424b2(atom, feed):
    """SEC's type=4 returns 424B2, 424B5, 40-F and friends. Verified live."""
    ciks = {c: f"SYM{i}" for i, c in enumerate(_ciks_for_form(atom, "424B2"))}
    items, _ = feed._parse_atom(atom, ciks, since_ms=0, want_form="4")
    assert items == [], "424B2 entries leaked into a Form 4 request"


def test_form_4_request_keeps_actual_form_4s(atom, feed):
    """The complement of the test above: filtering must not throw out the baby."""
    ciks = {c: f"SYM{i}" for i, c in enumerate(_ciks_for_form(atom, "4"))}
    assert ciks, "fixture has no Form 4 entries; it is no longer fit for purpose"

    items, _ = feed._parse_atom(atom, ciks, since_ms=0, want_form="4")
    assert items
    assert len(items) == len(_accessions_for_form(atom, "4"))
    assert all(i.headline.startswith("4:") for i in items)
    assert all(i.kind == "filing" for i in items)


def test_one_filing_with_two_parties_becomes_one_item(atom, feed):
    """EDGAR emits one <entry> per party, so a Form 4 appears twice: once for
    the issuer, once for the reporting insider. That is one filing."""
    entries = len(_ciks_for_form(atom, "4"))
    filings = len(_accessions_for_form(atom, "4"))
    assert entries > filings, "fixture no longer exercises the multi-party case"

    ciks = {c: f"SYM{i}" for i, c in enumerate(_ciks_for_form(atom, "4"))}
    items, _ = feed._parse_atom(atom, ciks, since_ms=0, want_form="4")

    assert len(items) == filings
    # And the merge kept both parties' symbols rather than dropping one.
    assert any(len(i.symbols) > 1 for i in items)
    assert all(list(i.symbols) == sorted(set(i.symbols)) for i in items)


def test_amendments_are_kept(atom, feed):
    """`8-K/A` is a genuine revision of the same event, not a different form."""
    assert feed._form_matches("8-K/A", "8-K")
    assert feed._form_matches("8-K", "8-K")
    assert not feed._form_matches("8-K12B", "8-K")
    assert not feed._form_matches("424B2", "4")
    assert not feed._form_matches("40-F", "4")


# ---------------------------------------------------------------------------
# Timestamps. published_ms must be the regulator's instant, not ours.
# ---------------------------------------------------------------------------


def test_published_ms_comes_from_the_atom_updated_field(atom, feed):
    ciks = {c: "X" for c in _ciks_for_form(atom, "4")}
    items, _ = feed._parse_atom(atom, ciks, since_ms=0, want_form="4")

    updated = [
        m for m in re.findall(r"<updated>(.*?)</updated>", atom)
    ]
    assert any(u.startswith("20") for u in updated)

    for i in items:
        # Sanity: a plausible epoch-ms in 2026, not seconds and not nanos.
        assert 1_700_000_000_000 < i.published_ms < 2_000_000_000_000


def test_ingested_ms_is_our_clock_not_theirs(atom, feed):
    ciks = {c: "X" for c in _ciks_for_form(atom, "4")}
    items, _ = feed._parse_atom(atom, ciks, since_ms=0, want_form="4")
    assert all(i.ingested_ms == 1_800_000_000_000 for i in items)
    # And the two are genuinely different fields, which is the point.
    assert any(i.published_ms != i.ingested_ms for i in items)


def test_since_ms_excludes_already_seen_filings(atom, feed):
    ciks = {c: "X" for c in _ciks_for_form(atom, "4")}
    everything, _ = feed._parse_atom(atom, ciks, since_ms=0, want_form="4")
    assert len(everything) > 1

    midpoint = sorted(i.published_ms for i in everything)[len(everything) // 2]
    newer, _ = feed._parse_atom(atom, ciks, since_ms=midpoint, want_form="4")

    assert all(i.published_ms > midpoint for i in newer)
    assert len(newer) < len(everything)


# ---------------------------------------------------------------------------
# Symbol resolution
# ---------------------------------------------------------------------------


def test_only_watchlist_ciks_are_returned(atom, feed):
    all_f4 = _ciks_for_form(atom, "4")
    watched = {all_f4[0]: "AAPL"}
    items, _ = feed._parse_atom(atom, watched, since_ms=0, want_form="4")
    assert len(items) >= 1
    assert all(i.symbols == ("AAPL",) for i in items)


def test_empty_watchlist_returns_nothing(atom, feed):
    items, _ = feed._parse_atom(atom, {}, since_ms=0, want_form="4")
    assert items == []


def test_unresolved_symbols_are_reported(feed):
    """A typo'd ticker would otherwise produce zero filings forever, silently."""
    feed._cik_by_ticker = {"AAPL": "0000320193"}
    assert feed.unresolved(["AAPL", "NOTATICKER"]) == ["NOTATICKER"]


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_malformed_xml_returns_empty_not_an_exception(feed):
    items, oldest = feed._parse_atom("<not xml", {}, since_ms=0, want_form="4")
    assert items == [] and oldest is None


def test_xml_prolog_with_iso_8859_1_declaration_parses(atom, feed):
    """httpx hands us a decoded str; ElementTree refuses one that still declares
    an encoding. Dropping the prolog is the fix and this asserts it works."""
    assert atom.lstrip().startswith("<?xml")
    assert 'ISO-8859-1' in atom[:120]
    items, _ = feed._parse_atom(atom, {c: "X" for c in _ciks_for_form(atom, "4")},
                                since_ms=0, want_form="4")
    assert items


def test_ids_are_stable_across_parses(atom, feed):
    ciks = {c: "X" for c in _ciks_for_form(atom, "4")}
    a, _ = feed._parse_atom(atom, ciks, since_ms=0, want_form="4")
    b, _ = feed._parse_atom(atom, ciks, since_ms=0, want_form="4")
    assert [i.id for i in a] == [i.id for i in b]
    assert len({i.id for i in a}) == len(a), "ids collided within one page"
