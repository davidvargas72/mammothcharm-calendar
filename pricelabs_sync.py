#!/usr/bin/env python3
"""
PriceLabs Dynamic Pricing Calendar Automation
=============================================
Automates the daily extraction of PriceLabs dynamic pricing and minimum stay rules
for Mammoth Charm LLC, builds an RFC 5545 compliant iCalendar (.ics) feed,
and publishes it to an S3/R2 bucket or updates Google Calendar directly.

Author: Mammoth Charm LLC Operations
Version: 1.2.0 (Date window filtering and robust availability detection)
"""

import os
import sys
import json
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

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
TIMEZONE_STR = "America/Los_Angeles"

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

    def fetch_pricing(self) -> List[Dict[str, Any]]:
        if not self.api_key:
            logger.warning("No PRICELABS_API_KEY set.")
            raise ValueError("Missing PriceLabs credentials.")

        headers = {
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

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

    def _determine_availability(self, r: Dict[str, Any]) -> bool:
        """
        Inspects PriceLabs fields (occupancy, booking_status, status, blocked, available)
        to identify whether a date is open or unavailable.
        """
        # 1. Occupancy check (PriceLabs date-level occupancy: 1.0 or 1 = booked/occupied)
        if "occupancy" in r and r["occupancy"] is not None:
            try:
                occ = float(r["occupancy"])
                if occ >= 1.0:
                    return False
                if occ == 0.0:
                    return True
            except (ValueError, TypeError):
                pass

        # 2. Status text check
        for key in ("booking_status", "status", "reservation_status"):
            if key in r and r[key]:
                val = str(r[key]).strip().lower()
                if val in ("booked", "reserved", "unavailable", "blocked", "closed"):
                    return False
                if val in ("available", "open"):
                    return True

        # 3. Explicit blocked flag
        if "blocked" in r and r["blocked"] is not None:
            val = r["blocked"]
            if isinstance(val, bool) and val:
                return False
            if isinstance(val, (int, float)) and val > 0:
                return False
            if str(val).strip().lower() in ("true", "1", "yes"):
                return False

        # 4. Explicit available flag
        for key in ("available", "availability", "is_available"):
            if key in r and r[key] is not None:
                val = r[key]
                if isinstance(val, bool):
                    return val
                if isinstance(val, (int, float)):
                    return val > 0
                if str(val).strip().lower() in ("false", "0", "no", "unavailable", "booked"):
                    return False
                if str(val).strip().lower() in ("true", "1", "yes", "available"):
                    return True

        return True

    def _normalize_records(self, raw_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        # Compute today's date in Pacific Time
        if ZoneInfo:
            today = datetime.now(ZoneInfo(TIMEZONE_STR)).date()
        else:
            today = (datetime.utcnow() - timedelta(hours=7)).date()

        one_year_ahead = today + timedelta(days=365)
        logger.info(f"Filtering calendar window: {today} to {one_year_ahead} ({TIMEZONE_STR})")

        normalized = []
        for r in raw_records:
            d_str = r.get("date")
            if not d_str:
                continue

            try:
                d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
            except ValueError:
                continue

            # Exclude past dates and dates beyond 365 days
            if d_obj < today or d_obj > one_year_ahead:
                continue

            is_avail = self._determine_availability(r)

            normalized.append({
                "date": d_str,
                "price": str(r.get("price") or r.get("final_price") or r.get("recommended_price", "0")),
                "min_stay": str(r.get("min_stay", "2")),
                "available": is_avail,
                "raw_sample": r if d_obj == today else None
            })

        # Log today's raw fields in the Action console for verification
        today_match = [x for x in normalized if x["date"] == str(today)]
        if today_match and today_match[0]["raw_sample"]:
            logger.info(f"API fields returned for today ({today}): {json.dumps(today_match[0]['raw_sample'])}")

        logger.info(f"Retained {len(normalized)} records within the 1-year window.")
        return normalized

# ---------------------------------------------------------------------------
# 2. RFC 5545 iCalendar (.ics) Builder
# ---------------------------------------------------------------------------
class ICSCalendarBuilder:
    """Builds an RFC 5545 compliant .ics calendar file."""

    def __init__(self, calendar_name: str = "PriceLabs", timezone: str = TIMEZONE_STR):
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
                d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
            except ValueError:
                continue

            dtstart = d_obj.strftime("%Y%m%d")
            dtend = (d_obj + timedelta(days=1)).strftime("%Y%m%d")

            price = str(item.get("price", "0"))
            min_stay = str(item.get("min_stay", "2"))
            is_avail = item.get("available", True)
            status_str = "Available" if is_avail else "Unavailable"

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

    extractor = PriceLabsExtractor(
        api_key=PRICELABS_API_KEY,
        listing_id=PRICELABS_LISTING_ID,
        pms=PRICELABS_PMS_NAME
    )
    records = extractor.fetch_pricing()

    builder = ICSCalendarBuilder(calendar_name="PriceLabs")
    ics_content = builder.build_ics(records)

    with open(OUTPUT_ICS_FILENAME, "w", encoding="utf-8") as f:
        f.write(ics_content)
    logger.info(f"Local calendar file written to: {OUTPUT_ICS_FILENAME}")

    logger.info("=== PriceLabs Calendar Sync Finished Successfully ===")

if __name__ == "__main__":
    main()
