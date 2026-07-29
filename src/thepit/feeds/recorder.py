"""Raw response recorder.

Every provider response is written to disk verbatim before anything parses it.
The point is not debugging, it is ownership: after a month of running, the
recording is a dataset that exists independently of whether the provider still
serves us, still exists, or still allows the terms it allowed in January. That
already mattered once in Stage 1 (issue #13).

Format is gzipped JSON Lines, one file per (source, kind, UTC hour).

*Why JSONL and not one file per response:* a fetch every few seconds for a year
is millions of files. Most filesystems cope; Finder, backups, and `rm -rf` do
not. One append-only file per hour keeps the inode count in the thousands.

*Why gzip and not zstd:* zstd compresses JSON noticeably better, but it is not
in the 3.12 standard library and this project has a hard three-runtime-dependency
budget. gzip gets ~5x on this kind of payload, which is enough. Concatenated gzip
members decompress as one stream, so appending is safe and `zcat` just works.
"""

from __future__ import annotations

import gzip
import json
import time
from datetime import UTC, datetime
from pathlib import Path


class RawRecorder:
    """Append-only writer for provider responses.

    Not thread-safe by design: the engine is the single writer, and adding a
    lock here would imply otherwise.
    """

    def __init__(self, root: Path | str, *, enabled: bool = True) -> None:
        self.root = Path(root)
        self.enabled = enabled

    def record(
        self,
        source: str,
        kind: str,
        payload: str | bytes,
        *,
        ts_ms: int,
        meta: dict | None = None,
    ) -> str | None:
        """Append one response. Returns the path to store in `fetch_log.raw_path`.

        Recording must never be able to break the feed: the data is nice to have,
        the feed staying up is the requirement. So a failure here is swallowed
        and reported as ``None`` rather than raised. The caller still gets its
        parsed data and still writes its `fetch_log` row; only the raw copy is
        lost, and the absence of a `raw_path` is itself the record of that.
        """
        if not self.enabled:
            return None

        try:
            dt = datetime.fromtimestamp(ts_ms / 1000, tz=UTC)
            rel = Path(source) / kind / f"{dt:%Y-%m-%d}" / f"{dt:%H}.jsonl.gz"
            path = self.root / rel
            path.parent.mkdir(parents=True, exist_ok=True)

            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", errors="replace")

            line = json.dumps(
                {"ts_ms": ts_ms, "meta": meta or {}, "body": payload},
                separators=(",", ":"),
            )
            # mode="at" appends a fresh gzip member. Concatenated members are a
            # valid gzip stream, so the file stays readable with plain zcat.
            with gzip.open(path, "at", encoding="utf-8", compresslevel=6) as fh:
                fh.write(line + "\n")
            return str(rel)
        except Exception:  # pragma: no cover - defensive, see docstring
            return None

    def disk_usage_bytes(self) -> int:
        if not self.root.exists():
            return 0
        return sum(p.stat().st_size for p in self.root.rglob("*.jsonl.gz"))

    def prune_older_than(self, days: int) -> int:
        """Delete recordings older than `days`. Returns bytes reclaimed.

        Disk headroom on this machine is tight (22GB free alongside large video
        files), so retention is a first-class operation rather than something to
        bolt on after the disk fills at 3am.
        """
        if not self.root.exists():
            return 0
        cutoff = time.time() - days * 86_400
        freed = 0
        for p in self.root.rglob("*.jsonl.gz"):
            if p.stat().st_mtime < cutoff:
                freed += p.stat().st_size
                p.unlink()
        # Leave the directory tree; empty dirs are free and recreating them
        # every hour is churn.
        return freed
