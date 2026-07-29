"""SEC EDGAR filings feed.

The highest-signal free source available, and the one that survived Stage 1's
data-source cull (issue #13). Form 4 insider transactions and 8-K material
events are timestamped by the regulator rather than by a news desk, which means
the publication time is trustworthy in a way headline feeds are not.

**Design note: the firehose, not per-company polling.**

The obvious approach is to fetch ``data.sec.gov/submissions/CIK##########.json``
per watchlist symbol. That works, and it is a trap: those documents are ~160KB
each, so twenty symbols on a ten-minute cycle is roughly 475MB of downloads per
day for a handful of actual filings.

``browse-edgar?action=getcurrent`` returns the most recent filings across all
companies for a given form type in one ~29KB request. We fetch that per form
type and filter to the watchlist. Two orders of magnitude less traffic, and
fresher, because we are not waiting for a symbol's turn in a round-robin.

The tradeoff is that the firehose is a *window*, not a cursor: if more than its
page size of a given form type is filed between two polls, we miss the overflow.
That is a real gap and it is why :meth:`poll` reports when it saturates.
"""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime
from xml.etree import ElementTree

from thepit.core.clock import Clock
from thepit.core.types import FeedUnavailable, FetchRecord, NewsItem
from thepit.feeds import http as feed_http
from thepit.feeds.http import FeedHttp
from thepit.store.repos import news_id

BROWSE_EDGAR = "https://www.sec.gov/cgi-bin/browse-edgar"
TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"

ATOM_NS = {"a": "http://www.w3.org/2005/Atom"}

# Form types worth waking an agent for, in rough order of signal.
#   4    insider transactions. An officer buying is one of the few genuinely
#        informative public signals.
#   8-K  material events: the "something happened" form.
#   10-Q/10-K  periodic financials.
DEFAULT_FORMS = ("4", "8-K", "10-Q", "10-K")

# SEC throttles bursts harder than its published 10 req/s ceiling implies.
# Pacing costs nothing at our cadence. See FeedHttp(min_interval_s=...).
MIN_REQUEST_INTERVAL_S = 0.5

# `count` is capped server-side at 100 regardless of what is requested
# (asked for 400, got 101). Measured window spans, 2026-07-29:
#
#     form 4    ~54 minutes
#     form 8-K  ~5.7 hours
#
# Form 4 is the binding constraint: poll more often than every ~54 minutes or
# filings slide off the end unseen. 10 minutes gives roughly 5x headroom, and
# the saturation warning in poll() catches the case where that stops being
# enough (a heavy filing day, or a stalled poller).
MAX_PAGE_SIZE = 100
RECOMMENDED_POLL_INTERVAL_S = 600

# `8-K - Cycurion, Inc. (0001868419) (Filer)`
_TITLE_RE = re.compile(r"^(?P<form>[^-]+?)\s*-\s*(?P<company>.+?)\s*\((?P<cik>\d{10})\)")
_ACCESSION_RE = re.compile(r"accession-number=([\d-]+)")
_PROLOG_RE = re.compile(r"^\s*<\?xml[^>]*\?>")


class EdgarNewsFeed:
    """Recent SEC filings for a watchlist."""

    name = "edgar"

    def __init__(
        self,
        http: FeedHttp,
        clock: Clock,
        *,
        forms: tuple[str, ...] = DEFAULT_FORMS,
        page_size: int = MAX_PAGE_SIZE,
    ) -> None:
        self._http = http
        self._clock = clock
        self._forms = forms
        self._page_size = min(page_size, MAX_PAGE_SIZE)
        self._cik_by_ticker: dict[str, str] = {}
        self._map_loaded_ms = 0

    # -- lifecycle -----------------------------------------------------------

    async def probe(self) -> None:
        if not feed_http.has_contact():
            raise FeedUnavailable(
                "SEC requires a contact email in the User-Agent. "
                "Set THEPIT_CONTACT_EMAIL=you@example.com. "
                "Without it www.sec.gov returns a bare 403."
            )
        res = await self._http.get(
            TICKER_MAP_URL, source=self.name, kind="news", record_raw=False
        )
        if not res.ok:
            raise FeedUnavailable(f"EDGAR ticker map unavailable: {res.record.error}")

    async def load_ticker_map(self) -> tuple[FetchRecord, int]:
        """Fetch ticker -> CIK. Returns the fetch record and how many were loaded.

        ~800KB, and it changes only when companies list or delist, so this is
        refreshed daily rather than per poll.
        """
        res = await self._http.get(
            TICKER_MAP_URL, source=self.name, kind="news", record_raw=False
        )
        if not res.ok or res.text is None:
            return res.record, 0

        import json

        try:
            data = json.loads(res.text)
        except ValueError as exc:
            return _as_failure(res.record, f"ticker map parse failed: {exc}"), 0

        # Shape is {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "..."}, ...}
        self._cik_by_ticker = {
            str(row["ticker"]).upper(): f"{int(row['cik_str']):010d}"
            for row in data.values()
            if row.get("ticker") and row.get("cik_str") is not None
        }
        self._map_loaded_ms = self._clock.now_ms()
        return res.record, len(self._cik_by_ticker)

    def cik_for(self, ticker: str) -> str | None:
        return self._cik_by_ticker.get(ticker.upper())

    def unresolved(self, symbols: list[str]) -> list[str]:
        """Watchlist symbols with no CIK. Worth surfacing rather than swallowing:
        a typo'd ticker silently produces zero filings forever."""
        return [s for s in symbols if s.upper() not in self._cik_by_ticker]

    # -- polling -------------------------------------------------------------

    async def poll(
        self, symbols: list[str], since_ms: int
    ) -> tuple[list[NewsItem], list[FetchRecord], list[str]]:
        """Filings for `symbols` published since `since_ms`.

        Returns (items, fetch records, warnings). Warnings carry the saturation
        signal described in the module docstring -- a silently truncated window
        would look exactly like a quiet day.
        """
        if not self._cik_by_ticker:
            rec, _ = await self.load_ticker_map()
            if not self._cik_by_ticker:
                return [], [rec], ["ticker map unavailable; cannot resolve symbols"]

        wanted: dict[str, str] = {}
        for s in symbols:
            cik = self.cik_for(s)
            if cik:
                wanted.setdefault(cik, s.upper())

        items: list[NewsItem] = []
        records: list[FetchRecord] = []
        warnings: list[str] = []

        for form in self._forms:
            res = await self._http.get(
                BROWSE_EDGAR,
                source=self.name,
                kind="news",
                params={
                    "action": "getcurrent",
                    "type": form,
                    "owner": "include",
                    "count": self._page_size,
                    "output": "atom",
                },
            )
            records.append(res.record)
            if not res.ok or not res.text:
                continue

            parsed, oldest_ms = self._parse_atom(res.text, wanted, since_ms, form)
            items.extend(parsed)

            # If the whole page is newer than our watermark we have no way to
            # know what fell off the end of it.
            if oldest_ms is not None and oldest_ms > since_ms > 0:
                warnings.append(
                    f"form {form}: window saturated (oldest entry {oldest_ms} is newer "
                    f"than watermark {since_ms}); filings may have been missed"
                )

        items.sort(key=lambda i: i.published_ms)
        return items, records, warnings

    @staticmethod
    def _form_matches(form: str, want_form: str) -> bool:
        """Whether a returned form type is actually the one we asked for.

        ``browse-edgar``'s ``type=`` parameter is a **prefix** match, not an
        exact one. Asking for ``type=4`` returns 424B2, 424B5, 40-F, 497K and
        anything else beginning with "4" -- in a live sample, 73 of 100 entries
        were 424B2 prospectus supplements and only 10 were the Form 4 insider
        filings requested. Verified 2026-07-29.

        ``/A`` amendments are kept: an amended 8-K is a revision of the same
        material event, not a different kind of document.
        """
        return form == want_form or form == f"{want_form}/A"

    def _parse_atom(
        self, xml_text: str, wanted: dict[str, str], since_ms: int, want_form: str
    ) -> tuple[list[NewsItem], int | None]:
        try:
            # httpx has already decoded the body to str. ElementTree refuses a
            # str that still carries an encoding declaration (EDGAR's prolog
            # says ISO-8859-1), and re-encoding to match the declaration would
            # mangle any character outside Latin-1. Dropping the prolog is the
            # honest fix: the decoding has already happened correctly.
            root = ElementTree.fromstring(_PROLOG_RE.sub("", xml_text, count=1))
        except ElementTree.ParseError:
            return [], None

        now = self._clock.now_ms()
        # Keyed by accession number, not appended to a list.
        #
        # EDGAR emits one <entry> per PARTY to a filing, not per filing. A Form
        # 4 has both an issuer and a reporting owner, so the same accession
        # number appears twice with two different CIKs -- in the test fixture,
        # 10 Form 4 entries are only 5 actual filings.
        #
        # Emitting both would be wrong twice over: the ids collide, so
        # `ON CONFLICT DO NOTHING` would silently keep whichever arrived first
        # and discard the other party's symbol association. Merging here means
        # one filing becomes one item carrying every watchlist symbol involved.
        merged: dict[str, NewsItem] = {}
        oldest: int | None = None

        for entry in root.findall("a:entry", ATOM_NS):
            title = _text(entry, "a:title")
            updated = _text(entry, "a:updated")
            if not title or not updated:
                continue

            m = _TITLE_RE.match(title)
            if not m:
                continue

            published_ms = _iso_to_ms(updated)
            if published_ms is None:
                continue
            oldest = published_ms if oldest is None else min(oldest, published_ms)

            if published_ms <= since_ms:
                continue

            form = m.group("form").strip()
            if not self._form_matches(form, want_form):
                continue

            cik = m.group("cik")
            if cik not in wanted:
                continue

            entry_id = _text(entry, "a:id") or ""
            accession = _ACCESSION_RE.search(entry_id)
            external_id = accession.group(1) if accession else entry_id
            if not external_id:
                continue

            link_el = entry.find("a:link", ATOM_NS)
            url = link_el.get("href") if link_el is not None else None

            company = m.group("company").strip()
            symbol = wanted[cik]

            prior = merged.get(external_id)
            if prior is not None:
                # Same filing, another party. Union the symbols and keep the
                # earliest publication instant -- the parties' entries can carry
                # timestamps a second or two apart and the filing happened once.
                if symbol not in prior.symbols:
                    merged[external_id] = replace(
                        prior,
                        symbols=tuple(sorted({*prior.symbols, symbol})),
                        published_ms=min(prior.published_ms, published_ms),
                    )
                continue

            merged[external_id] = NewsItem(
                id=news_id(self.name, external_id),
                published_ms=published_ms,
                ingested_ms=now,
                source=self.name,
                external_id=external_id,
                kind="filing",
                symbols=(symbol,),
                headline=f"{form}: {company}",
                summary=_describe(form),
                url=url,
            )

        return list(merged.values()), oldest


# ---------------------------------------------------------------------------


def _text(entry: ElementTree.Element, path: str) -> str | None:
    el = entry.find(path, ATOM_NS)
    return el.text.strip() if el is not None and el.text else None


def _iso_to_ms(s: str) -> int | None:
    """Parse EDGAR's `2026-07-29T12:41:49-04:00`.

    The offset is present and correct, so this is a real instant rather than a
    date we would have to guess a timezone for. That is precisely why the atom
    feed's `updated` is used as `published_ms` instead of the `filingDate` field
    in the submissions API, which is date-only and would round every filing to
    midnight.
    """
    try:
        return int(datetime.fromisoformat(s).timestamp() * 1000)
    except ValueError:
        return None


def _describe(form: str) -> str:
    return {
        "4": "Insider transaction (Form 4)",
        "8-K": "Material event",
        "10-Q": "Quarterly report",
        "10-K": "Annual report",
    }.get(form, f"SEC filing: {form}")


def _as_failure(rec: FetchRecord, error: str) -> FetchRecord:
    from dataclasses import replace

    return replace(rec, ok=False, error=error)
