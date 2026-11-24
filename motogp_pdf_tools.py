"""MotoGP PDF downloader: one session, one GP weekend, or a whole season.

For Google ADK:
    from .motogp_pdf_tools import (
        download_session_pdfs, download_weekend_pdfs, download_season_pdfs
    )

Uses the undocumented public-facing MotoGP Results API. The schema may change.
Run again to resume: previously downloaded PDFs are never overwritten.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API = "https://api.motogp.pulselive.com/motogp/v1"
BASE_DIR = Path.home() / "MotoGP_Results"  # Change this if needed.
SESSIONS = ("fp1", "pr", "fp2", "q1", "q2", "spr", "wup", "rac")
MAX_PDF_BYTES = 30 * 1024 * 1024
REQUEST_PAUSE_SECONDS = 0.2


def _parse_url(url: str) -> tuple[str, str, str]:
    """Return (year, GP, session) after validating the complete site URL."""
    parsed = urlsplit(url.strip())
    parts = parsed.path.strip("/").split("/")
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() not in {"www.motogp.com", "motogp.com"}
        or len(parts) != 7
        or parts[:2] != ["en", "gp-results"]
        or parts[4].lower() != "motogp"
        or parts[6].lower() != "classification"
        or not re.fullmatch(r"20\d{2}", parts[2])
        or not re.fullmatch(r"[a-z0-9]{2,8}", parts[3].lower())
        or parts[5].lower() not in SESSIONS
    ):
        raise ValueError("Expected a MotoGP /en/gp-results/YEAR/GP/motogp/SESSION/classification URL")
    return parts[2], parts[3].lower(), parts[5].lower()


def _validate_year(year: int) -> str:
    if isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year <= 2099:
        raise ValueError("Year must be an integer between 2000 and 2099")
    return str(year)


def _http_client() -> requests.Session:
    http = requests.Session()
    http.headers.update({"User-Agent": "Mozilla/5.0 MotoGP-portfolio-learning/2.0"})
    retry = Retry(
        total=2,
        backoff_factor=0.7,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
    )
    http.mount("https://", HTTPAdapter(max_retries=retry))
    return http


def _json_get(http: requests.Session, path: str, **params):
    response = http.get(f"{API}{path}", params=params, timeout=(10, 30))
    response.raise_for_status()
    return response.json()


def _fetch_season_events(http: requests.Session, year: str) -> tuple[list, str]:
    seasons = _json_get(http, "/results/seasons")
    season = next((s for s in seasons if str(s.get("year")) == year), None)
    if not season:
        raise ValueError(f"Season {year} not found in API")
    events = _json_get(http, "/results/events", seasonUuid=season["id"])
    if not isinstance(events, list):
        raise ValueError("Expected a list of events from API")
    # The endpoint without isFinished includes both completed and future races.
    # Exclude official test events; never assume that a calendar entry has results.
    unique = {}
    for event in events:
        if not isinstance(event, dict) or event.get("test") is True:
            continue
        code = str(event.get("short_name") or "").lower()
        if not re.fullmatch(r"[a-z0-9]{2,8}", code):
            continue
        unique[code] = event
    if not unique:
        raise ValueError("No valid race events in API response")
    return sorted(unique.values(), key=lambda e: (str(e.get("date_start") or e.get("date") or ""),
                                                   str(e.get("short_name") or ""))), season["id"]


def _category_uuid(http: requests.Session, event_id: str) -> str:
    categories = _json_get(http, "/results/categories", eventUuid=event_id)
    category = next(
        (c for c in categories if c.get("legacy_id") == 3 or
         str(c.get("name") or "").startswith("MotoGP")),
        None,
    )
    if not category:
        raise ValueError("MotoGP category not found")
    return category["id"]


def _walk_pdf_links(node):
    if isinstance(node, dict):
        for child in node.values():
            yield from _walk_pdf_links(child)
    elif isinstance(node, list):
        for child in node:
            yield from _walk_pdf_links(child)
    elif isinstance(node, str):
        parsed = urlsplit(node)
        if (parsed.scheme == "https" and parsed.hostname == "resources.motogp.com"
                and parsed.path.lower().endswith(".pdf")):
            yield node


def _link_metadata(link: str) -> tuple[str, str, str] | None:
    """Safely extract YEAR/GP/SESSION from a resources.motogp.com PDF URL."""
    parsed = urlsplit(link)
    if parsed.scheme != "https" or parsed.hostname != "resources.motogp.com":
        return None
    parts = parsed.path.strip("/").split("/")
    # Example: files/results/2026/JPN/MotoGP/FP1/Classification.pdf
    if len(parts) < 7 or parts[:2] != ["files", "results"] or parts[4].lower() != "motogp":
        return None
    year, gp, session = parts[2], parts[3].lower(), parts[5].lower()
    if not re.fullmatch(r"20\d{2}", year) or not re.fullmatch(r"[a-z0-9]{2,8}", gp):
        return None
    return year, gp, session


def _get_links_by_session(http: requests.Session, event: dict, year: str) -> tuple[dict, list]:
    """Fetch each session's metadata once for the entire weekend."""
    gp = str(event["short_name"]).lower()
    category_id = _category_uuid(http, event["id"])
    sessions = _json_get(http, "/results/sessions", eventUuid=event["id"], categoryUuid=category_id)
    if not isinstance(sessions, list):
        raise ValueError(f"Unexpected sessions response for {gp}")
    by_session = {s: set() for s in SESSIONS}
    unmapped = set()
    for session in sessions:
        if not isinstance(session, dict):
            continue
        for link in _walk_pdf_links(session.get("session_files") or {}):
            info = _link_metadata(link)
            if info is None or info[:2] != (year, gp):
                unmapped.add("unknown_path")
                continue
            code = info[2]
            if code in by_session:
                by_session[code].add(link)
            else:
                unmapped.add(code)
    return {session: sorted(links) for session, links in by_session.items()}, sorted(unmapped)


def _download_one(http: requests.Session, link: str, target: Path) -> str:
    """Download a validated PDF without overwriting an existing file."""
    if target.is_file():
        return "existing"
    tmp = target.with_suffix(target.suffix + ".part")
    try:
        # No redirects to third-party hosts or internal/private addresses.
        with http.get(link, stream=True, allow_redirects=False, timeout=(10, 40)) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError(f"Unexpected PDF HTTP status: {response.status_code}")
            total = 0
            checked_signature = False
            with tmp.open("wb") as output:
                for chunk in response.iter_content(chunk_size=65536):
                    if not chunk:
                        continue
                    if not checked_signature:
                        if not chunk.startswith(b"%PDF-"):
                            raise ValueError("Response did not contain a PDF")
                        checked_signature = True
                    total += len(chunk)
                    if total > MAX_PDF_BYTES:
                        raise ValueError("PDF exceeds 30 MB limit")
                    output.write(chunk)
            if not checked_signature:
                raise ValueError("Empty PDF response")
        if target.exists():
            return "existing"
        tmp.rename(target)
        return "downloaded"
    finally:
        tmp.unlink(missing_ok=True)


def _process_event(http: requests.Session, year: str, event: dict,
                   only_session: str | None = None) -> dict:
    gp = str(event["short_name"]).lower()
    result = {
        "gp": gp, "name": event.get("name") or event.get("sponsored_name") or gp.upper(),
        "status": "success", "downloaded": 0, "existing": 0, "failed": 0,
        "sessions": {}, "errors": [], "unmapped_pdf_codes": [],
    }
    try:
        by_session, unmapped = _get_links_by_session(http, event, year)
        result["unmapped_pdf_codes"] = unmapped
        for session in ([only_session] if only_session else SESSIONS):
            folder = BASE_DIR / year / gp.capitalize() / session
            folder.mkdir(parents=True, exist_ok=True)
            links = by_session[session]
            counters = {"found": len(links), "downloaded": 0, "existing": 0, "failed": 0}
            result["sessions"][session] = counters
            for link in links:
                filename = Path(urlsplit(link).path).name
                if not re.fullmatch(r"[A-Za-z0-9_.-]+\.pdf", filename, re.I):
                    counters["failed"] += 1
                    result["failed"] += 1
                    result["errors"].append({"session": session, "file": filename, "error": "Unsafe filename"})
                    continue
                try:
                    status = _download_one(http, link, folder / filename)
                    counters[status] += 1
                    result[status] += 1
                except (OSError, ValueError, requests.RequestException) as exc:
                    counters["failed"] += 1
                    result["failed"] += 1
                    result["errors"].append({"session": session, "file": filename, "error": str(exc)})
                finally:
                    time.sleep(REQUEST_PAUSE_SECONDS)
        if result["failed"]:
            result["status"] = "partial"
        elif not any(s["found"] for s in result["sessions"].values()):
            result["status"] = "no_pdfs_found"
    except (OSError, ValueError, TypeError, KeyError, requests.RequestException) as exc:
        result["status"] = "error"
        result["errors"].append({"error": str(exc)})
    return result


def _save_report(year: str, report: dict) -> str | None:
    """Store full batch results locally; return path or None on failure."""
    try:
        folder = BASE_DIR / year
        folder.mkdir(parents=True, exist_ok=True)
        name = f"_pdf_report_{datetime.now(timezone.utc):%Y%m%d_%H%M%S_%f}.json"
        target = folder / name
        target.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        return str(target)
    except OSError:
        return None


def _summarize(year: str, mode: str, event_results: list[dict]) -> dict:
    totals = {
        "downloaded": sum(e["downloaded"] for e in event_results),
        "existing": sum(e["existing"] for e in event_results),
        "failed": sum(e["failed"] for e in event_results),
    }
    issue_count = sum(e["status"] == "error" for e in event_results)
    missing = [e["gp"] for e in event_results if e["status"] == "no_pdfs_found"]
    failed = [e["gp"] for e in event_results if e["status"] in {"error", "partial"}]
    status = "partial" if failed else ("no_pdfs_found" if len(missing) == len(event_results) else "success")
    report = {
        "status": status, "mode": mode, "year": year,
        "folder": str(BASE_DIR / year),
        "events_processed": len(event_results),
        "events_with_error": issue_count,
        **totals,
        "events_without_pdfs": missing,
        "events_with_errors": failed,
        "events": event_results,
    }
    report_path = _save_report(year, report)
    # Compact result for the LLM; full per-file errors reside in report_path.
    return {
        k: v for k, v in report.items() if k != "events"
    } | {
        "report_path": report_path,
        "per_event": [
            {
                "gp": e["gp"], "status": e["status"],
                "downloaded": e["downloaded"], "existing": e["existing"],
                "failed": e["failed"],
                "empty_sessions": [s for s, counts in e["sessions"].items() if counts["found"] == 0],
                "unmapped_codes": e["unmapped_pdf_codes"],
            }
            for e in event_results
        ],
    }


def _execute(url: str | None = None, year: int | None = None,
             mode: str = "session", verbose: bool = False) -> dict:
    try:
        if url is not None:
            parsed_year, gp_code, session_code = _parse_url(url)
        else:
            if year is None:
                raise ValueError("A year or URL is required")
            parsed_year = _validate_year(year)
            gp_code, session_code = None, None
        with _http_client() as http:
            events, _ = _fetch_season_events(http, parsed_year)
            if mode == "season":
                selected = events
            else:
                selected = [event for event in events
                            if str(event.get("short_name", "")).lower() == gp_code]
                if not selected:
                    raise ValueError(f"Grand Prix {gp_code} not found in season {parsed_year}")
            results = []
            for index, event in enumerate(selected, 1):
                if verbose:
                    print(f"[{index}/{len(selected)}] {event['short_name']} ...", flush=True)
                outcome = _process_event(
                    http, parsed_year, event,
                    only_session=session_code if mode == "session" else None
                )
                results.append(outcome)
                if verbose:
                    print(
                        f"    {outcome['status']}: "
                        f"{outcome['downloaded']} downloaded, "
                        f"{outcome['existing']} existing, "
                        f"{outcome['failed']} failed",
                        flush=True
                    )
        return _summarize(parsed_year, mode, results)
    except (ValueError, OSError, KeyError, TypeError, requests.RequestException) as exc:
        return {"status": "error", "mode": mode, "message": str(exc)}


def download_session_pdfs(url: str) -> dict:
    """Downloads PDF reports for one requested MotoGP session.

    Args:
        url: Full official MotoGP classification page URL.

    Returns:
        Download summary, missing reports, and JSON report path.
    """
    return _execute(url=url, mode="session")


def download_weekend_pdfs(url: str) -> dict:
    """Downloads all available reports for all 8 sessions of one GP weekend.

    Args:
        url: Any official MotoGP classification URL from the GP weekend.

    Returns:
        Download summary, missing sessions, and JSON report path.
    """
    return _execute(url=url, mode="weekend")


def download_season_pdfs(year: int) -> dict:
    """Downloads available MotoGP PDF reports for EVERY non-test GP in a year.

    The full-season operation can take many minutes. Run it again to resume;
    existing PDF files are not overwritten. Future races may have no PDFs yet.

    Args:
        year: Season year, such as 2026.

    Returns:
        Counts per event, errors, missing sessions, and JSON report path.
    """
    return _execute(year=year, mode="season")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Download MotoGP results PDFs")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--session", metavar="URL", help="Download one session")
    group.add_argument("--weekend", metavar="URL", help="Download all eight sessions")
    group.add_argument("--season", type=int, metavar="YEAR", help="Download all race weekends")
    args = parser.parse_args()
    if args.session:
        output = _execute(url=args.session, mode="session", verbose=True)
    elif args.weekend:
        output = _execute(url=args.weekend, mode="weekend", verbose=True)
    else:
        output = _execute(year=args.season, mode="season", verbose=True)
    print(json.dumps(output, ensure_ascii=False, indent=2))
    raise SystemExit(1 if output["status"] in {"error", "partial"} else 0)
