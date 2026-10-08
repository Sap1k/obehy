"""Fetch every network source of a production build before any conversion starts.

Fetching up front dates all sources of a release within minutes of each other and makes an
unreachable host fail the build before the long JrUtil stages instead of after them.
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from obehy import gvd, national_czptt, national_jdf, regional_overlay
from obehy.pipeline import download
from obehy.pipeline.files import utc_now, write_json
from obehy.pipeline.reporting import Reporter

JdfSourceFetcher = Callable[[Path, Reporter], Path]
CzpttSourceFetcher = Callable[[national_czptt.BuildConfig, Path, Reporter], Path]


@dataclass(frozen=True)
class FetchedSources:
    reference_date: date
    jdf: Path
    czptt: Path
    regional: list[regional_overlay.Source]
    retrieval: dict[str, object]


def fetch_jdf_sources(destination: Path, reporter: Reporter) -> Path:
    national_jdf.download_sources(download.download_file, destination, reporter)
    return destination


def _timed[T](timings: dict[str, float], name: str, fetch: Callable[[], T]) -> T:
    started = time.monotonic()
    result = fetch()
    timings[name] = round(time.monotonic() - started, 3)
    return result


def fetch_sources(
    root: Path,
    czptt_config: national_czptt.BuildConfig,
    reporter: Reporter,
    *,
    jdf_fetcher: JdfSourceFetcher = fetch_jdf_sources,
    czptt_fetcher: CzpttSourceFetcher = national_czptt.snapshot_sources,
    regional_downloader: regional_overlay.DownloadGtfsFn | None = None,
) -> FetchedSources:
    """Download national JDF, CZPTT, PID and IDS JMK into ``root``.

    Every attempt goes to ``root/fetch-log.json``, also when a source finally fails.
    """

    root.mkdir(parents=True, exist_ok=True)
    reference_date = gvd.prague_today()
    started_at = utc_now()
    timings: dict[str, float] = {}
    downloader = regional_downloader or functools.partial(
        regional_overlay.download_gtfs, reporter=reporter
    )
    with download.fetch_log() as log:
        try:
            jdf = _timed(
                timings, "national-jdf", lambda: jdf_fetcher(root / "national-jdf", reporter)
            )
            czptt = _timed(
                timings,
                "national-czptt",
                lambda: czptt_fetcher(czptt_config, root / "czptt", reporter),
            )
            regional = _timed(
                timings,
                "regional",
                lambda: regional_overlay.snapshot_sources(root / "regional", downloader),
            )
        finally:
            write_json(
                root / "fetch-log.json",
                {"schema_version": 1, "seconds": timings, "attempts": log.attempts},
            )
    retrieval: dict[str, object] = {
        "reference_date": reference_date.isoformat(),
        "started_at": started_at,
        "finished_at": utc_now(),
        "seconds": timings,
    }
    return FetchedSources(reference_date, jdf, czptt, regional, retrieval)
