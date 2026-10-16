#!/usr/bin/env python3
"""
PriceLabs Dynamic Pricing Calendar Automation
=============================================
Automates the daily extraction of PriceLabs dynamic pricing and minimum stay rules
for Mammoth Charm LLC, builds an RFC 5545 compliant iCalendar (.ics) feed,
and publishes it to an S3/R2 bucket or updates Google Calendar directly.

Author: Mammoth Charm LLC Operations
Version: 1.5.0 (Removed redundant DESCRIPTION property)
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
        if "occupancy" in r and r["occupancy"] is not None:
            try:
                occ = float(r["occupancy"])
                if occ >= 1.0:
                    return False
                if occ == 0.0:
                    return True
            except (ValueError, TypeError):
                pass

        for key in ("booking_status", "status", "reservation_status"):
            if key in r and r[key]:
                val = str(r[key]).strip().lower()
                if val in ("booked", "reserved", "unavailable", "blocked", "closed"):
                    return False
                if val in ("available", "open"):
                    return True

        if "blocked" in r and r["blocked"] is not None:
            val = r["blocked"]
            if isinstance(val, bool) and val:
                return False
            if isinstance(val, (int, float)) and val > 0:
                return False
            if str(val).strip().lower() in ("true", "1", "yes"):
                return False

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
        if ZoneInfo:
            today = datetime.now(ZoneInfo(TIMEZONE_STR)).date()
        else:
            today = (datetime.utcnow() - timedelta(hours=7)).date()

        start_date = today + timedelta(days=1)
        end_date = start_date + timedelta(days=365)
        logger.info(f"Filtering calendar window: {start_date} to {end_date} ({TIMEZONE_STR})")

        normalized = []
        for r in raw_records:
            d_str = r.get("date")
            if not d_str:
                continue

            try:
                d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
            except ValueError:
                continue

            if d_obj < start_date or d_obj > end_date:
                continue

            is_avail = self._determine_availability(r)

            normalized.append({
                "date": d_str,
                "price": str(r.get("price") or r.get("final_price") or r.get("recommended_price", "0")),
                "min_stay": str(r.get("min_stay", "2")),
                "available": is_avail
            })

        logger.info(f"Retained {len(normalized)} records starting from {start_date}.")
        return normalized

# ---------------------------------------------------------------------------
# 2. RFC 5545 iCalendar (.ics) Builder
# ---------------------------------------------------------------------------
class ICSCalendarBuilder:
    """Builds an RFC 5545 compliant .ics calendar feed."""

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

            # Primary title displayed on the Google Calendar grid
            event_title
