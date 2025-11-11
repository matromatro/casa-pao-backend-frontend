from __future__ import annotations

from google.oauth2.service_account import Credentials
import gspread
import json
import os
from pathlib import Path


def load_service_account_info() -> dict:
    """Load the Google service-account JSON used for gspread auth."""

    # Allow overriding the credentials path via environment variable so the
    # script can be pointed at alternate keys without editing the file.
    credentials_path = os.environ.get(
        "SERVICE_ACCOUNT_PATH", "casa-do-pao-frances-api-b21a00db61b6.json"
    )

    info_path = Path(credentials_path)
    if not info_path.exists():
        raise FileNotFoundError(
            f"Service-account JSON not found: {info_path.resolve()}"
        )

    with info_path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def main() -> None:
    info = load_service_account_info()
    creds = Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key("1tJW5BHQTq3a5O1w-RTIJ7iIUcq29939NBsLgKMyGCEk")
    print(sh.title)


if __name__ == "__main__":
    main()
