"""
Agent for creating folders and downloading pdf files on user's device
"""

import os
import re
import requests

from pathlib import Path
from urllib.parse import urlsplit

from .motogp_pdf_tools import (
    download_session_pdfs,
    download_weekend_pdfs,
    download_season_pdfs,
)

from google.adk.agents.llm_agent import Agent
from google.adk.models.lite_llm import LiteLlm

# Configuration
os.environ.setdefault(
    "OLLAMA_API_BASE",
    "http://localhost:11434"
)

API_URL = (
    "https://api.motogp.pulselive.com/motogp/v1"
)

BASE_DIR = Path.home() / "MotoGP_results"

SESSIONS = [
    "fp1", "pr", "fp2", "q1",
    "q2", "spr", "wup", "rac"
]

# Helper: Validate and parse the URL
def parse_motogp_url(url: str) -> dict:
    """Validates a MotoGP URL and extracts season and GP."""

    parsed = urlsplit(url.strip())
    parts = parsed.path.strip("/").split("/")

    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() not in (
            "www.motogp.com", "motogp.com"
        )
        or len(parts) != 7
        or parts[0] != "en"
        or parts[1] != "gp-results"
        or parts[4].lower() != "motogp"
        or parts[6].lower() != "classification"
        or not re.fullmatch(r"20\d{2}", parts[2])
        or not re.fullmatch(
            r"[a-z0-9-]{2,12}", parts[3].lower()
        )
        or parts[5].lower() not in SESSIONS
    ):
        raise ValueError("Invalid MotoGP classification URL")

    return {
        "year": parts[2],
        "gp": parts[3].lower()
    }

def build_folders(year: str, gp_codes: list) -> dict:
    """Creates only missing MotoGP session directories."""

    created = 0
    existing = 0

    for gp in gp_codes:
        if not re.fullmatch(r"[a-zA-Z0-9]{2,8}", gp):
            raise ValueError(f"Invalid GP code: {gp}")

        gp_folder = BASE_DIR / year / gp.capitalize()

        for session in SESSIONS:
            folder = gp_folder / session

            if folder.is_dir():
                existing += 1
            else:
                folder.mkdir(
                    parents=True,
                    exist_ok=True
                )
                created += 1

    return {
        "status": "success",
        "season": year,
        "gp_count": len(gp_codes),
        "created_session_folders": created,
        "existing_session_folders": existing,
        "base_path": str(BASE_DIR / year),
        "gp_codes": gp_codes
    }

# Tool 1: One complete race weekend
def create_weekend_folders(url: str) -> dict:
    """Creates all 8 session folders for one MotoGP weekend.

    Args:
        url: A MotoGP classification URL.

    Returns:
        Folder creation summary.
    """
    try:
        data = parse_motogp_url(url)

        return build_folders(
            data["year"],
            [data["gp"]]
        )

    except (ValueError, OSError) as error:
        return {
            "status": "error",
            "message": str(error)
        }


# Tool 2: Entire MotoGP season
def create_season_folders(url: str) -> dict:
    """Fetches the MotoGP calendar and creates season folders.

    Args:
        url: One MotoGP classification URL for the
             requested season.

    Returns:
        GP list and folder creation summary.
    """
    try:
        data = parse_motogp_url(url)
        year = data["year"]

        response = requests.get(
            f"{API_URL}/events",
            params={"seasonYear": year},
            timeout=20
        )
        response.raise_for_status()

        events = response.json()

        if not isinstance(events, list):
            raise ValueError("Unexpected API response")

        # Keep Grand Prix events, exclude tests
        gp_codes = []

        for event in events:
            if event.get("kind") != "GP":
                continue

            code = event.get("shortname", "")

            if not isinstance(code, str):
                raise ValueError("Invalid API event code")

            code = code.lower()

            if not re.fullmatch(r"[a-z0-9]{2,8}", code):
                raise ValueError(
                    f"Unexpected event code: {code}"
                )

            if code not in gp_codes:
                gp_codes.append(code)

        if not gp_codes:
            raise ValueError(
                "No Grand Prix events found in API"
            )

        result = build_folders(year, gp_codes)

        # Check expected 2026 calendar size
        if year == "2026" and len(gp_codes) != 22:
            result["warning"] = (
                "Expected 22 GPs in 2026, but API "
                f"returned {len(gp_codes)}. "
                "Calendar may be incomplete."
            )

        return result

    except (
        ValueError,
        requests.RequestException,
        OSError
    ) as error:
        return {
            "status": "error",
            "message": str(error)
        }

# Google ADK Agent
root_agent = Agent(
    model=LiteLlm(
        model="ollama_chat/gemma4:e2b",
        api_base="http://localhost:11434",
    ),
    name='motogp_agent',
    description='Organizes MotoGP results into local season, event, session folders',
    instruction="""
        You are a MotoGP file organization assistant.

    Available tools:

    1. create_weekend_folders:
       Creates 8 session folders for one GP.

    2. create_season_folders:
       Fetches the season calendar from the MotoGP API
       and creates 8 session folders for each Grand Prix.

    3. download_session_pdfs:
       Downloads PDF files for a specific session of a GP weekend.

    Rules:
    - Determine whether the user wants one weekend
      or an entire season.
    - Ask for confirmation before creating
      folders for an entire season.
    - Always use the exact MotoGP URL supplied if the user asks about specific weekend.
    - Never invent event codes.
    - Never delete or overwrite existing files.
    - Report folder counts and API warnings.
    - Never claim success when a tool returns an error.

    When the user requests MotoGP PDF reports:
    1. For a single session:
    Call download_session_pdfs(url).
    2. For an entire race weekend:
    Call download_weekend_pdfs(url).
    3. For an entire season:
    Confirm the requested season with the user.
    Then call download_season_pdfs(year).
    4. Do not call each session separately when
    downloading an entire weekend or season.
    5. Report downloaded, existing and failed files.
    6. Explain which sessions have no available PDFs.
    7. Never report success when a tool returns an error.
    """,
    tools=[
        create_weekend_folders,
        create_season_folders,
        download_session_pdfs,
        download_weekend_pdfs,
        download_season_pdfs,
    ]
)
