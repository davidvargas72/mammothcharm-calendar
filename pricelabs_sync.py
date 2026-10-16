#!/usr/bin/env python3
"""
PriceLabs Dynamic Pricing Calendar Automation
=============================================
Automates the daily extraction of PriceLabs dynamic pricing and minimum stay rules
for Mammoth Charm LLC, builds an RFC 5545 compliant iCalendar (.ics) feed,
and publishes it to an S3/R2 bucket or updates Google Calendar directly.

Author: Mammoth Charm LLC Operations
Version: 1.1.0 (Updated API Endpoint)
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

OUTPUT_ICS_FILENAME = os.getenv("OUTPUT_ICS_FILENAME", "pricelabs_pricing.ics")

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
        Fetches calendar pricing data using the POST /listing_prices endpoint.
        """
        if not self.api_key:
            logger.warning("No PRICELABS_API_KEY set.")
            raise ValueError("Missing PriceLabs credentials.")

        headers = {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
        # Updated to the correct POST endpoint
        url = f"{self.API_BASE_URL}/listing_prices"
        payload = {
            "listings": [
                {
                    "id": self.listing_id,
                    "pms": self.pms
                }
            ]
        }

        logger.info(f"Querying PriceLabs API for listing {self.listing_id} ({self.pms})...")
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        
        if response.status_code == 200:
            data = response.json()
            pricing_data = []
            
            # Parse the list structure returned by the API
            if isinstance(data, list) and len(data) > 0:
                pricing_data = data[0].get("data", [])
            elif isinstance(data, dict):
                listings_array = data.get("listings", data.get("prices", []))
                if listings_array and isinstance(listings_array, list):
                    pricing_data = listings_array[0].get("data", [])
                else:
                    pricing_data = data.get("data", [])

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


# ---------------------------------------------------------------------------
# 2. RFC 5545 iCalendar (.ics) Builder
# ---------------------------------------------------------------------------
class ICSCalendarBuilder:
    """Builds an RFC 5545 compliant .ics calendar file matching user formatting rules."""

    def __init__(self, calendar_name: str = "PriceLabs", timezone: str = "America/Los_Angeles"):
        self.calendar_name = calendar_name
        self.timezone = timezone

    def build_ics(self, pricing_records: List[Dict[str, Any]]) -> str:
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

            summary = f"${price} / {status_str} / {min_stay} N"
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
# Main Execution Pipeline
# ---------------------------------------------------------------------------
def main():
    logger.info("=== Starting PriceLabs Calendar Sync ===")

    # Step 1: Extract data
    extractor = PriceLabsExtractor(
        api_key=PRICELABS_API_KEY,
        listing_id=PRICELABS_LISTING_ID,
        pms=PRICELABS_PMS_NAME
    )
    records = extractor.fetch_pricing(days_ahead=365)

    # Step 2: Build ICS Calendar
    builder = ICSCalendarBuilder(calendar_name="PriceLabs")
    ics_content = builder.build_ics(records)

    # Save local file
    with open(OUTPUT_ICS_FILENAME, "w", encoding="utf-8") as f:
        f.write(ics_content)
    logger.info(f"Local calendar file written to: {OUTPUT_ICS_FILENAME}")

    logger.info("=== PriceLabs Calendar Sync Finished Successfully ===")

if __name__ == "__main__":
    main()
