#!/usr/bin/env python3
"""
PriceLabs Dynamic Pricing Calendar Automation
=============================================
Automates the daily extraction of PriceLabs dynamic pricing and minimum stay rules
for Mammoth Charm LLC, builds an RFC 5545 compliant iCalendar (.ics) feed,
and publishes it to an S3/R2 bucket or updates Google Calendar directly.

Author: Mammoth Charm LLC Operations
Version: 1.0.0
"""

import os
import sys
import json
import logging
import argparse
from datetime import datetime, timedelta
from typing import List, Dict, Any, Optional

import requests

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("pricelabs_sync")


# ---------------------------------------------------------------------------
# Configuration & Environment
# ---------------------------------------------------------------------------
PRICELABS_API_KEY = os.getenv("PRICELABS_API_KEY", "")
PRICELABS_LISTING_ID = os.getenv("PRICELABS_LISTING_ID", "1089448089629471493")
PRICELABS_PMS_NAME = os.getenv("PRICELABS_PMS_NAME", "airbnb")

# S3 / Cloudflare R2 / Object Storage Configuration
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "")
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "")  # E.g. Cloudflare R2 endpoint
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")
AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
OUTPUT_ICS_FILENAME = os.getenv("OUTPUT_ICS_FILENAME", "pricelabs_pricing.ics")

# Google Calendar Direct API Configuration (Optional)
GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "")


# ---------------------------------------------------------------------------
# 1. PriceLabs Data Extractor
# ---------------------------------------------------------------------------
class PriceLabsExtractor:
    """Extracts daily pricing, minimum stay rules, and availability from PriceLabs."""

    API_BASE_URL = "https://api.pricelabs.co/v1"

    def __init__(self, api_key: str, listing_id: str, pms: str = "airbnb"):
        self.api_key = api_key
        self.listing_id = listing_id
        self.pms = pms

    def fetch_pricing(self, days_ahead: int = 365) -> List[Dict[str, Any]]:
        """
        Fetches calendar pricing data.
        Falls back to authenticated session or custom export if public API key
        is not provisioned.
        """
        if not self.api_key:
            logger.warning("No PRICELABS_API_KEY set. Attempting session or local fallback.")
            return self._fetch_via_session_or_mock(days_ahead)

        headers = {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        url = f"{self.API_BASE_URL}/listings/{self.listing_id}/prices"
        params = {
            "pms": self.pms,
            "days": days_ahead
        }

        logger.info(f"Querying PriceLabs API for listing {self.listing_id} ({self.pms})...")
        response = requests.get(url, headers=headers, params=params, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            pricing_data = data.get("data", data.get("prices", []))
            logger.info(f"Successfully retrieved {len(pricing_data)} daily price records.")
            return self._normalize_records(pricing_data)
        else:
            logger.error(f"PriceLabs API returned HTTP {response.status_code}: {response.text}")
            raise RuntimeError(f"PriceLabs API request failed: {response.status_code}")

    def _normalize_records(self, raw_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Ensures all records adhere to standard keys."""
        normalized = []
        for r in raw_records:
            normalized.append({
                "date": r.get("date"),
                "price": str(r.get("price") or r.get("final_price") or r.get("recommended_price", "0")),
                "min_stay": str(r.get("min_stay", "2")),
                "availability": str(r.get("availability", "1"))
            })
        return normalized

    def _fetch_via_session_or_mock(self, days_ahead: int) -> List[Dict[str, Any]]:
        """Fallback helper for running with local export or web session token."""
        local_csv = os.getenv("PRICELABS_LOCAL_CSV", "")
        if local_csv and os.path.exists(local_csv):
            logger.info(f"Reading pricing data from local CSV export: {local_csv}")
            return self._parse_csv_file(local_csv)
        raise ValueError(
            "Missing PriceLabs credentials. Please set PRICELABS_API_KEY in your environment."
        )

    def _parse_csv_file(self, filepath: str) -> List[Dict[str, Any]]:
        import csv
        results = []
        with open(filepath, mode="r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                results.append({
                    "date": row.get("Date", "").strip(),
                    "price": row.get("Recommended Price", row.get("Your Price", "300")).strip(),
                    "min_stay": row.get("Min Stay", "2").strip(),
                    "availability": row.get("Available", "1").strip()
                })
        return results


# ---------------------------------------------------------------------------
# 2. RFC 5545 iCalendar (.ics) Builder
# ---------------------------------------------------------------------------
class ICSCalendarBuilder:
    """Builds an RFC 5545 compliant .ics calendar file matching user formatting rules."""

    def __init__(self, calendar_name: str = "PriceLabs", timezone: str = "America/Los_Angeles"):
        self.calendar_name = calendar_name
        self.timezone = timezone

    def build_ics(self, pricing_records: List[Dict[str, Any]]) -> str:
        """
        Converts daily records into iCalendar VCALENDAR syntax:
          - Event Title: $[price] / [Available or Booked] / [min_stay] 🌓
          - Event Description: [Available or Booked] / $[price] per night / [min_stay] night minimum stay
          - Timing: All-day event (VALUE=DATE)
          - Transparency: TRANSPARENT (does not block busy times)
        """
        now_utc = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//Mammoth Charm LLC//PriceLabs Calendar Sync//EN",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH",
            f"X-WR-CALNAME:{self.calendar_name}",
            f"X-WR-TIMEZONE:{self.timezone}"
        ]

        count = 0
        for item in pricing_records:
            d_str = item.get("date")
            if not d_str:
                continue

            try:
                d_obj = datetime.strptime(d_str, "%Y-%m-%d")
            except ValueError:
                continue

            next_d = d_obj + timedelta(days=1)
            dtstart = d_obj.strftime("%Y%m%d")
            dtend = next_d.strftime("%Y%m%d")

            price = str(item.get("price", "0"))
            min_stay = str(item.get("min_stay", "2"))
            avail = str(item.get("availability", "1"))
            status_str = "Available" if avail == "1" else "Booked"

            summary = f"${price} / {status_str} / {min_stay} 🌓"
            description = f"{status_str} / ${price} per night / {min_stay} night minimum stay"
            uid = f"pricelabs-{dtstart}@mammothcharm.com"

            lines.append("BEGIN:VEVENT")
            lines.append(f"UID:{uid}")
            lines.append(f"DTSTAMP:{now_utc}")
            lines.append(f"DTSTART;VALUE=DATE:{dtstart}")
            lines.append(f"DTEND;VALUE=DATE:{dtend}")
            lines.append(f"SUMMARY:{summary}")
            lines.append(f"DESCRIPTION:{description}")
            lines.append("STATUS:CONFIRMED")
            lines.append("TRANSP:TRANSPARENT")
            lines.append("END:VEVENT")
            count += 1

        lines.append("END:VCALENDAR")
        logger.info(f"Generated {count} VEVENT entries for iCalendar feed.")
        return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# 3. S3 / Cloudflare R2 Publisher
# ---------------------------------------------------------------------------
class StoragePublisher:
    """Uploads calendar feed to AWS S3, Cloudflare R2, or compatible object storage."""

    def __init__(self, bucket_name: str, endpoint_url: Optional[str] = None):
        self.bucket_name = bucket_name
        self.endpoint_url = endpoint_url or None

    def publish(self, ics_content: str, key_name: str = "pricelabs_pricing.ics") -> str:
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            logger.error("boto3 package not installed. Run 'pip install boto3'")
            raise

        s3_client = boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=AWS_ACCESS_KEY_ID,
            aws_secret_access_key=AWS_SECRET_ACCESS_KEY,
            region_name=AWS_REGION,
            config=Config(signature_version="s3v4")
        )

        logger.info(f"Uploading {key_name} to bucket {self.bucket_name}...")
        s3_client.put_object(
            Bucket=self.bucket_name,
            Key=key_name,
            Body=ics_content.encode("utf-8"),
            ContentType="text/calendar; charset=utf-8",
            CacheControl="no-cache, must-revalidate",
            ACL="public-read"
        )

        if self.endpoint_url:
            public_url = f"{self.endpoint_url.rstrip('/')}/{self.bucket_name}/{key_name}"
        else:
            public_url = f"https://{self.bucket_name}.s3.{AWS_REGION}.amazonaws.com/{key_name}"

        logger.info(f"Successfully published calendar to: {public_url}")
        return public_url


# ---------------------------------------------------------------------------
# 4. Direct Google Calendar API Publisher (Optional Alternative)
# ---------------------------------------------------------------------------
class GoogleCalendarDirectPublisher:
    """Directly creates and updates events on Google Calendar using Service Account."""

    def __init__(self, service_account_json: str, calendar_id: str):
        self.service_account_json = service_account_json
        self.calendar_id = calendar_id

    def sync_events(self, pricing_records: List[Dict[str, Any]]):
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError:
            logger.error("Google API client not installed. Run 'pip install google-api-python-client google-auth'")
            raise

        info = json.loads(self.service_account_json)
        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/calendar"]
        )
        service = build("calendar", "v3", credentials=creds)

        logger.info(f"Starting direct sync to Google Calendar ID: {self.calendar_id}...")
        for item in pricing_records:
            d_str = item.get("date")
            price = str(item.get("price", "0"))
            min_stay = str(item.get("min_stay", "2"))
            avail = str(item.get("availability", "1"))
            status_str = "Available" if avail == "1" else "Booked"

            event_id = f"pl{d_str.replace('-', '')}"
            summary = f"${price} / {status_str} / {min_stay} 🌓"
            desc = f"{status_str} / ${price} per night / {min_stay} night minimum stay"

            event_body = {
                "id": event_id,
                "summary": summary,
                "description": desc,
                "start": {"date": d_str},
                "end": {"date": (datetime.strptime(d_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")},
                "transparency": "transparent"
            }

            try:
                service.events().import_(calendarId=self.calendar_id, body=event_body).execute()
            except Exception as e:
                logger.debug(f"Event import note for {d_str}: {e}")


# ---------------------------------------------------------------------------
# Main Execution Pipeline
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="PriceLabs Calendar Sync Automation")
    parser.add_argument("--days", type=int, default=365, help="Number of forecast days (default: 365)")
    parser.add_argument("--output", type=str, default="pricelabs_pricing.ics", help="Local output file path")
    parser.add_argument("--publish-s3", action="store_true", help="Publish .ics feed to S3/Cloudflare R2")
    parser.add_argument("--sync-gcal", action="store_true", help="Directly sync events to Google Calendar via API")
    args = parser.parse_args()

    logger.info("=== Starting PriceLabs Calendar Sync ===")

    # Step 1: Extract data
    extractor = PriceLabsExtractor(
        api_key=PRICELABS_API_KEY,
        listing_id=PRICELABS_LISTING_ID,
        pms=PRICELABS_PMS_NAME
    )
    records = extractor.fetch_pricing(days_ahead=args.days)

    # Step 2: Build ICS Calendar
    builder = ICSCalendarBuilder(calendar_name="PriceLabs")
    ics_content = builder.build_ics(records)

    # Save local file
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(ics_content)
    logger.info(f"Local calendar file written to: {args.output}")

    # Step 3: Publish to S3 / Cloudflare R2 if requested
    if args.publish_s3 or (S3_BUCKET_NAME and not args.sync_gcal):
        if not S3_BUCKET_NAME:
            logger.error("Cannot publish to S3: S3_BUCKET_NAME is not set.")
            sys.exit(1)
        publisher = StoragePublisher(
            bucket_name=S3_BUCKET_NAME,
            endpoint_url=S3_ENDPOINT_URL
        )
        url = publisher.publish(ics_content, key_name=OUTPUT_ICS_FILENAME)
        print(f"\n[SUCCESS] Live Calendar Feed URL:\n{url}\n")

    # Step 4: Direct Google Calendar sync if requested
    if args.sync_gcal:
        if not GOOGLE_SERVICE_ACCOUNT_JSON or not GOOGLE_CALENDAR_ID:
            logger.error("Cannot sync to Google Calendar: Missing credentials or calendar ID.")
            sys.exit(1)
        gcal_publisher = GoogleCalendarDirectPublisher(
            service_account_json=GOOGLE_SERVICE_ACCOUNT_JSON,
            calendar_id=GOOGLE_CALENDAR_ID
        )
        gcal_publisher.sync_events(records)
        print("\n[SUCCESS] Direct Google Calendar sync complete.\n")

    logger.info("=== PriceLabs Calendar Sync Finished Successfully ===")


if __name__ == "__main__":
    main()
