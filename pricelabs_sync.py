#!/usr/bin/env python3
"""
PriceLabs Dynamic Pricing Calendar Automation
=============================================
Automates the daily extraction of PriceLabs dynamic pricing and minimum stay rules
for Mammoth Charm LLC, builds two RFC 5545 compliant iCalendar (.ics) feeds
AND two interactive HTML monthly calendar grids (Standard and 25% Discounted).

Author: Mammoth Charm LLC Operations
Version: 3.2.0 (Option 1A Cyan/Dark Text Header + Option 2A Pill Badge Legend)
"""

import os
import sys
import json
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Tuple

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

OUTPUT_STANDARD_FILENAME = os.getenv("OUTPUT_ICS_FILENAME", "pricelabs_pricing.ics")
OUTPUT_DISCOUNTED_FILENAME = os.getenv("OUTPUT_DISCOUNTED_ICS_FILENAME", "pricelabs_discounted.ics")

OUTPUT_STANDARD_HTML = "calendar.html"
OUTPUT_DISCOUNTED_HTML = "calendar_discounted.html"

TIMEZONE_STR = "America/Los_Angeles"
DISCOUNT_MULTIPLIER = 0.75  # 25% reduction

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

    def fetch_pricing(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
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
                val =
