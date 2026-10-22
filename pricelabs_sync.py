#!/usr/bin/env python3
"""
PriceLabs Dynamic Pricing & Cleaner Turnover Calendar Automation
================================================================
Automates the extraction of PriceLabs dynamic pricing and minimum stay rules
PLUS real-time booked/blocked dates directly from Airbnb's iCalendar (.ics) feed
for Mammoth Charm LLC. Builds:
  1. Two RFC 5545 compliant iCalendar (.ics) feeds
  2. Two interactive guest HTML monthly calendar grids (Standard & 25% Discounted)
  3. One dedicated read-only Cleaner Turnover Schedule dashboard (cleaner.html)

Author: Mammoth Charm LLC Operations
Version: 4.2.2 (Cleaner Grid Visual Polish + Pink/Amber Diagonal Turnover + Emoji-Free UI)
"""

import os
import sys
import re
import json
import logging
from datetime import datetime, timedelta
from typing import List, Dict, Any, Tuple, Set

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

# Direct Airbnb iCal URL for real-time availability override
AIRBNB_ICAL_URL = os.getenv(
    "AIRBNB_ICAL_URL",
    "https://www.airbnb.com/calendar/ical/1089448089629471493.ics?t=8d0abb3562d14c34a55f64514154b318"
)

OUTPUT_STANDARD_FILENAME = os.getenv("OUTPUT_ICS_FILENAME", "pricelabs_pricing.ics")
OUTPUT_DISCOUNTED_FILENAME = os.getenv("OUTPUT_DISCOUNTED_ICS_FILENAME", "pricelabs_discounted.ics")

OUTPUT_STANDARD_HTML = "calendar.html"
OUTPUT_DISCOUNTED_HTML = "calendar_discounted.html"
OUTPUT_CLEANER_HTML = "cleaner.html"

TIMEZONE_STR = "America/Los_Angeles"
DISCOUNT_MULTIPLIER = 0.75  # 25% reduction
CLEANING_FEE = 250          # $250 flat cleaning fee per stay

# Web3Forms Access Key (Delivers directly to davidvargas72@gmail.com)
WEB3FORMS_ACCESS_KEY = "323b43c5-dc22-4126-b48a-add7e5609c26"


# ---------------------------------------------------------------------------
# Perpetual US Federal Holiday Calculator
# ---------------------------------------------------------------------------
def calculate_us_holidays(year: int) -> Dict[str, str]:
    """Calculates all US Federal Holidays dynamically using calendar rules."""
    def nth_weekday(month: int, weekday: int, n: int) -> str:
        first_day = datetime(year, month, 1).date()
        offset = (weekday - first_day.weekday()) % 7
        day = 1 + offset + (n - 1) * 7
        return f"{year}-{month:02d}-{day:02d}"

    def last_weekday(month: int, weekday: int) -> str:
        if month == 12:
            next_month = datetime(year + 1, 1, 1).date()
        else:
            next_month = datetime(year, month + 1, 1).date()
        last_day = next_month - timedelta(days=1)
        offset = (last_day.weekday() - weekday) % 7
        target = last_day - timedelta(days=offset)
        return target.strftime("%Y-%m-%d")

    return {
        f"{year}-01-01": "New Year's Day",
        nth_weekday(1, 0, 3): "MLK Jr. Day",
        nth_weekday(2, 0, 3): "Presidents' Day",
        last_weekday(5, 0): "Memorial Day",
        f"{year}-06-19": "Juneteenth",
        f"{year}-07-04": "4th of July",
        nth_weekday(9, 0, 1): "Labor Day",
        nth_weekday(10, 0, 2): "Columbus Day",
        f"{year}-11-11": "Veterans Day",
        nth_weekday(11, 3, 4): "Thanksgiving",
        f"{year}-12-25": "Christmas Day",
    }


# ---------------------------------------------------------------------------
# 0. Direct Airbnb iCalendar (.ics) Availability & Turnover Extractor
# ---------------------------------------------------------------------------
class AirbnbICalExtractor:
    """
    Fetches and parses Airbnb's direct iCal feed to extract:
      1. blocked_dates: Set of YYYY-MM-DD strings where the night is occupied
      2. event_blocks: List of individual reservation/block dicts with start & end dates
         so back-to-back Same-Day Turnovers (DTEND == next DTSTART) are preserved.
    """

    def __init__(self, ical_url: str):
        self.ical_url = ical_url

    def fetch_ical_data(self) -> Tuple[Set[str], List[Dict[str, str]]]:
        blocked_dates: Set[str] = set()
        event_blocks: List[Dict[str, str]] = []

        if not self.ical_url:
            return blocked_dates, event_blocks

        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; MammothCharmCalendarSync/4.2.2; +https://mammothcharm.com)"
        }

        try:
            logger.info("Fetching live availability directly from Airbnb iCal feed...")
            response = requests.get(self.ical_url, headers=headers, timeout=20)
            if response.status_code != 200:
                logger.warning(f"Airbnb iCal returned HTTP {response.status_code}. Falling back to PriceLabs availability.")
                return blocked_dates, event_blocks

            # Unfold RFC 5545 multi-line fields
            raw_ics = response.text.replace("\r\n ", "").replace("\r\n\t", "").replace("\n ", "")
            events = re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", raw_ics, re.DOTALL)

            for event_block in events:
                dtstart_match = re.search(r"DTSTART(?:;[^:]*)?:(\d{8})", event_block)
                dtend_match = re.search(r"DTEND(?:;[^:]*)?:(\d{8})", event_block)

                if dtstart_match and dtend_match:
                    start_str = dtstart_match.group(1)
                    end_str = dtend_match.group(1)
                    try:
                        start_date = datetime.strptime(start_str, "%Y%m%d").date()
                        end_date = datetime.strptime(end_str, "%Y%m%d").date()
                        if end_date <= start_date:
                            continue

                        event_blocks.append({
                            "start": start_date.strftime("%Y-%m-%d"),
                            "end": end_date.strftime("%Y-%m-%d"),
                            "label": "Booked"
                        })

                        curr = start_date
                        # In iCal, DTEND is exclusive (morning of check-out)
                        while curr < end_date:
                            blocked_dates.add(curr.strftime("%Y-%m-%d"))
                            curr += timedelta(days=1)
                    except ValueError:
                        continue

            event_blocks.sort(key=lambda x: (x["start"], x["end"]))
            logger.info(
                f"Successfully parsed {len(blocked_dates)} booked nights across {len(event_blocks)} reservation blocks from Airbnb iCal."
            )
        except Exception as e:
            logger.warning(f"Failed to fetch Airbnb iCal ({e}). Falling back to PriceLabs availability.")

        return blocked_dates, event_blocks


# ---------------------------------------------------------------------------
# 1. PriceLabs Data Extractor
# ---------------------------------------------------------------------------
class PriceLabsExtractor:
    """Extracts daily pricing, minimum stay rules, and availability from PriceLabs."""

    API_BASE_URL = "https://api.pricelabs.co/v1"

    def __init__(self, api_key: str, listing_id: str, pms: str = "airbnb", airbnb_blocked: Set[str] = None):
        self.api_key = api_key
        self.listing_id = listing_id
        self.pms = pms
        self.airbnb_blocked = airbnb_blocked or set()

    def fetch_pricing(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Set[str]]:
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

    def _determine_availability(self, r: Dict[str, Any], d_str: str) -> bool:
        # 1. Master check: If date is blocked in live Airbnb iCal, mark unavailable immediately
        if d_str in self.airbnb_blocked:
            return False

        # 2. Secondary check: PriceLabs internal occupancy / booking flags
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
                val_str = str(r[key]).strip().lower()
                if val_str in ("booked", "reserved", "unavailable", "blocked", "closed"):
                    return False
                if val_str in ("available", "open"):
                    return True

        if "blocked" in r and r["blocked"] is not None:
            blocked_val = r["blocked"]
            if isinstance(blocked_val, bool) and blocked_val:
                return False
            if isinstance(blocked_val, (int, float)) and blocked_val > 0:
                return False
            if str(blocked_val).strip().lower() in ("true", "1", "yes"):
                return False

        for key in ("available", "availability", "is_available"):
            if key in r and r[key] is not None:
                avail_val = r[key]
                if isinstance(avail_val, bool):
                    return avail_val
                if isinstance(avail_val, (int, float)):
                    return avail_val > 0
                if str(avail_val).strip().lower() in ("false", "0", "no", "unavailable", "booked"):
                    return False
                if str(avail_val).strip().lower() in ("true", "1", "yes", "available"):
                    return True

        return True

    def _apply_orphan_gap_rules(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Guest-facing rule: Detects contiguous available windows where NO date in the window can
        satisfy its minimum stay requirement before the next unavailable date:
        - If the window starts at index 0 (immediately touching past/unbookable dates, like Oct 22),
          removes those dates from the bookable list so they render white/unbookable.
        - If the window is between two future booked blocks, marks those dates as available=False
          so they render light pink (Unavailable).
        """
        if not records:
            return records

        records.sort(key=lambda x: x["date"])
        n = len(records)
        i = 0
        leading_remove_indices: Set[int] = set()

        while i < n:
            if not records[i]["available"]:
                i += 1
                continue

            j = i
            while j < n and records[j]["available"]:
                j += 1

            can_any_book = False
            for k in range(i, j):
                try:
                    req_min = int(records[k].get("min_stay", 2))
                except (ValueError, TypeError):
                    req_min = 2
                remaining_open_nights = j - k
                if remaining_open_nights >= req_min:
                    can_any_book = True
                    break

            if not can_any_book:
                if i == 0:
                    for k in range(i, j):
                        leading_remove_indices.add(k)
                else:
                    for k in range(i, j):
                        records[k]["available"] = False

            i = j

        if leading_remove_indices:
            records = [rec for idx, rec in enumerate(records) if idx not in leading_remove_indices]

        return records

    def _normalize_records(self, raw_records: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Set[str]]:
        if ZoneInfo:
            today = datetime.now(ZoneInfo(TIMEZONE_STR)).date()
        else:
            today = (datetime.utcnow() - timedelta(hours=7)).date()

        start_date = today + timedelta(days=1)
        end_date = start_date + timedelta(days=365)

        holiday_catalog = {}
        for y in range(today.year, today.year + 3):
            holiday_catalog.update(calculate_us_holidays(y))

        standard_records = []
        discounted_records = []
        raw_booked_dates: Set[str] = set(self.airbnb_blocked)

        for r in raw_records:
            d_str = r.get("date")
            if not d_str:
                continue

            try:
                d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
            except ValueError:
                continue

            is_avail = self._determine_availability(r, d_str)
            if not is_avail:
                raw_booked_dates.add(d_str)

            if d_obj < start_date or d_obj > end_date:
                continue

            holiday_name = holiday_catalog.get(d_str, "")

            raw_price = r.get("price") or r.get("final_price") or r.get("recommended_price") or 0
            try:
                base_price = float(raw_price)
                std_price = round(base_price)
                disc_price = round(base_price * DISCOUNT_MULTIPLIER)
            except (ValueError, TypeError):
                std_price = 0
                disc_price = 0

            min_stay = str(r.get("min_stay", "2"))

            standard_records.append({
                "date": d_str,
                "price": str(std_price),
                "min_stay": min_stay,
                "available": is_avail,
                "holiday": holiday_name
            })

            discounted_records.append({
                "date": d_str,
                "price": str(disc_price),
                "min_stay": min_stay,
                "available": is_avail,
                "holiday": holiday_name
            })

        standard_records = self._apply_orphan_gap_rules(standard_records)
        discounted_records = self._apply_orphan_gap_rules(discounted_records)

        return standard_records, discounted_records, raw_booked_dates


# ---------------------------------------------------------------------------
# 2. RFC 5545 iCalendar (.ics) Builder
# ---------------------------------------------------------------------------
class ICSCalendarBuilder:
    """Builds an RFC 5545 compliant .ics calendar feed."""

    def __init__(self, calendar_name: str, timezone: str = TIMEZONE_STR):
        self.calendar_name = calendar_name
        self.timezone = timezone

    def build_ics(self, pricing_records: List[Dict[str, Any]], uid_prefix: str = "pricelabs") -> str:
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

        for item in pricing_records:
            d_str = item.get("date")
            if not d_str:
                continue

            d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
            dtstart = d_obj.strftime("%Y%m%d")
            dtend = (d_obj + timedelta(days=1)).strftime("%Y%m%d")

            is_avail = item.get("available", True)
            holiday = item.get("holiday", "")
            holiday_suffix = f" [{holiday}]" if holiday else ""

            if not is_avail:
                event_title = f"Unavailable{holiday_suffix}"
            else:
                price = str(item.get("price", "0"))
                min_stay = str(item.get("min_stay", "2"))
                event_title = f"${price} | {min_stay} Night Min{holiday_suffix}"

            uid = f"{uid_prefix}-{dtstart}@mammothcharm.com"

            lines.append("BEGIN:VEVENT")
            lines.append(f"UID:{uid}")
            lines.append(f"DTSTAMP:{now_utc}")
            lines.append(f"DTSTART;VALUE=DATE:{dtstart}")
            lines.append(f"DTEND;VALUE=DATE:{dtend}")
            lines.append(f"SUMMARY:{event_title}")
            lines.append("STATUS:CONFIRMED")
            lines.append("TRANSP:TRANSPARENT")
            lines.append("END:VEVENT")

        lines.append("END:VCALENDAR")
        return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# 3. Interactive Guest HTML Calendar Grid Builder (With Built-In Inquiry Form)
# ---------------------------------------------------------------------------
class HTMLCalendarBuilder:
    """Builds a standalone, responsive monthly HTML calendar grid with built-in inquiry form."""

    def __init__(self, title: str):
        self.title = title

    def build_html(self, pricing_records: List[Dict[str, Any]]) -> str:
        records_json = json.dumps(pricing_records)
        if ZoneInfo:
            now_local = datetime.now(ZoneInfo(TIMEZONE_STR))
        else:
            now_local = datetime.utcnow() - timedelta(hours=7)

        date_part = now_local.strftime("%b %d, %Y")
        time_part = now_local.strftime("%I:%M %p").lstrip("0").lower()
        updated_str = f"{date_part} at {time_part}"

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{self.title}</title>
<style>
  :root {{
    --bg-avail: #dcfce7;
    --border-avail: #86efac;
    --text-avail: #166534;
    --bg-unavail: #ffe4e6;
    --border-unavail: #fda4af;
    --bg-past: #f8fafc;
    --header-bg: #00c4b3;
    --header-text: #0f172a;
    --star-color: #0f172a;
  }}
  * {{ box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  
  body {{ margin: 0; padding: 0; background: transparent; color: #1e293b; overflow-x: hidden; width: 100%; }}
  
  .cal-container {{ width: 100%; max-width: 900px; margin: 0 auto; border: 1px solid #cbd5e1; border-radius: 6px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.05); background: #ffffff; }}
  
  .cal-header {{ display: flex; justify-content: space-between; align-items: center; padding: 14px 18px; background: #0f172a; color: #ffffff; }}
  .cal-title {{ font-size: 1.35rem; font-weight: 800; margin: 0; color: #ffffff; letter-spacing: -0.01em; }}
  .nav-btns {{ display: flex; gap: 8px; align-items: center; }}
  .nav-btn {{ background: #00c4b3; border: none; color: #0f172a; padding: 8px 14px; border-radius: 6px; cursor: pointer; font-weight: 700; font-size: 0.88rem; transition: background 0.15s; white-space: nowrap; }}
  .nav-btn:hover {{ background: #00a89a; }}

  .legend {{ display: flex; gap: 12px; padding: 10px 18px; background: #f8fafc; border-bottom: 1px solid #e2e8f0; align-items: center; justify-content: space-between; flex-wrap: wrap; }}
  .legend-items {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
  .legend-pill {{ display: inline-flex; align-items: center; padding: 4px 10px; border-radius: 4px; font-size: 0.8rem; font-weight: 700; }}
  .pill-avail {{ background: var(--bg-avail); color: var(--text-avail); border: 1px solid var(--border-avail); }}
  .pill-unavail {{ background: var(--bg-unavail); color: #be123c; border: 1px solid var(--border-unavail); }}
  .pill-holiday {{ background: transparent; color: var(--star-color); font-weight: 800; font-size: 0.85rem; padding: 0; }}
  .updated-label {{ color: #64748b; font-size: 0.75rem; font-weight: 500; }}

  .quote-prompt {{ background: #f0fdfa; border-bottom: 1px solid #ccfbf1; padding: 6px 18px; font-size: 0.8rem; font-weight: 600; color: #0f766e; display: flex; justify-content: space-between; align-items: center; min-height: 36px; }}
  .clear-btn {{ background: #ffffff; border: 1px solid #0f766e; color: #0f766e; border-radius: 4px; padding: 3px 10px; font-size: 0.74rem; font-weight: 700; cursor: pointer; display: none; margin-left: auto; transition: all 0.15s; }}
  .clear-btn:hover {{ background: #0f766e; color: #ffffff; }}

  .weekdays {{ display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); background: #f1f5f9; border-bottom: 1px solid #e2e8f0; text-align: center; font-weight: 700; font-size: 0.8rem; padding: 8px 0; color: #334155; }}
  .days-grid {{ display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 1px; background: #cbd5e1; }}
  
  .day-cell {{ min-height: 75px; background: #fff; padding: 6px; display: flex; flex-direction: column; justify-content: space-between; transition: background 0.12s; user-select: none; min-width: 0; overflow: hidden; }}
  .day-cell.empty {{ background: var(--bg-past); }}
  .day-cell.avail {{ background: var(--bg-avail); cursor: pointer; }}
  .day-cell.avail:hover {{ filter: brightness(0.96); }}
  .day-cell.unavail {{ background: var(--bg-unavail); }}

  /* 1A. Crisp 50/50 Diagonal Split for Start of Booked Block After Available Day (Upper-Left Green / Lower-Right Pink) */
  .day-cell.unavail.turnaround-checkin-booked {{
    background: linear-gradient(135deg, var(--bg-avail) 50%, var(--bg-unavail) 50%);
  }}

  /* 1B. Crisp 50/50 Diagonal Split for Start of Booked Block After Unbookable/White Day (e.g., Oct 23: Upper-Left White / Lower-Right Pink) */
  .day-cell.unavail.turnaround-past-booked {{
    background: linear-gradient(135deg, var(--bg-past) 50%, var(--bg-unavail) 50%);
  }}

  /* 2. Crisp 50/50 Diagonal Split for First Available Day After Booked Block (e.g., Nov 17: Upper-Left Pink / Lower-Right Green) */
  .day-cell.avail.turnaround-checkout-avail {{
    background: linear-gradient(135deg, var(--bg-unavail) 50%, var(--bg-avail) 50%);
  }}

  /* 3. Dashed Teal Border Around Eligible Check-Out Candidate (No Text Inside Until Clicked) */
  .day-cell.unavail.checkout-eligible {{
    cursor: pointer;
    outline: 2.5px dashed #0d9488;
    outline-offset: -2.5px;
  }}
  .day-cell.unavail.checkout-eligible:hover {{
    filter: brightness(0.95);
  }}

  .day-cell.selected-endpoint {{ background: var(--header-bg) !important; color: var(--header-text) !important; outline: none !important; }}
  .day-cell.selected-endpoint .price,
  .day-cell.selected-endpoint .min-stay,
  .day-cell.selected-endpoint .day-num {{ color: var(--header-text) !important; }}
  .day-cell.in-range {{ background: #ccfbf1 !important; }}
  .endpoint-badge {{ font-size: 0.62rem; font-weight: 800; text-transform: uppercase; background: #0f172a; color: #fff; padding: 2px 5px; border-radius: 3px; display: inline-block; margin-top: 2px; white-space: nowrap; letter-spacing: 0.02em; }}

  .day-top {{ display: flex; justify-content: space-between; align-items: flex-start; width: 100%; height: 16px; }}
  .holiday-star {{ font-size: 1rem; color: var(--star-color); line-height: 1; text-shadow: 0 1px 1px rgba(0,0,0,0.1); }}
  .day-num {{ font-size: 0.85rem; font-weight: 700; text-align: right; color: #475569; margin-left: auto; line-height: 1; }}
  
  .day-content {{ text-align: center; margin: auto 0; width: 100%; }}
  .price {{ font-size: 1.05rem; font-weight: 800; color: var(--text-avail); display: block; line-height: 1.15; white-space: nowrap; }}
  .min-stay {{ font-size: 0.72rem; color: #334155; display: block; margin-top: 2px; font-weight: 600; line-height: 1.1; white-space: nowrap; }}

  /* Quote Summary & Built-In Form */
  .quote-card {{ background: #f8fafc; border-top: 2px solid var(--header-bg); padding: 14px 18px; display: none; }}
  .quote-grid {{ display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; }}
  .quote-details {{ font-size: 0.88rem; color: #334155; line-height: 1.5; }}
  .quote-right {{ text-align: right; display: flex; align-items: center; gap: 14px; flex-wrap: wrap; }}
  .quote-total {{ font-size: 1.3rem; font-weight: 800; color: #0f172a; }}
  .quote-btn {{ background: var(--header-bg); color: var(--header-text); border: none; font-weight: 800; font-size: 0.88rem; padding: 10px 16px; border-radius: 6px; cursor: pointer; transition: background 0.15s; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  .quote-btn:hover {{ background: #00a89a; }}
  .quote-error {{ color: #be123c; font-size: 0.85rem; font-weight: 700; }}

  .inquiry-form {{ margin-top: 14px; padding-top: 14px; border-top: 1px solid #cbd5e1; display: none; }}
  .form-row {{ display: grid; grid-template-columns: 1.2fr 1.3fr 1fr 0.9fr; gap: 10px; margin-bottom: 10px; }}
  .form-input {{ width: 100%; padding: 8px 10px; border: 1px solid #cbd5e1; border-radius: 5px; font-size: 0.85rem; }}
  .form-submit-row {{ display: flex; gap: 10px; align-items: center; }}
  .submit-btn {{ background: #0f172a; color: #ffffff; border: none; font-weight: 700; font-size: 0.85rem; padding: 9px 18px; border-radius: 5px; cursor: pointer; white-space: nowrap; }}
  .submit-btn:hover {{ background: #1e293b; }}
  .form-status {{ font-size: 0.85rem; font-weight: 700; margin-top: 8px; display: none; }}

  .monthly-holidays {{ background: #f8fafc; padding: 12px 18px; font-size: 0.85rem; color: #334155; border-top: 1px solid #cbd5e1; }}
  .holiday-list-title {{ font-weight: 800; margin-bottom: 6px; color: #0f172a; text-transform: uppercase; font-size: 0.75rem; letter-spacing: 0.05em; }}
  .holiday-list {{ margin: 0; padding: 0; list-style: none; display: flex; flex-wrap: wrap; gap: 12px 24px; }}
  .holiday-item {{ display: flex; align-items: center; gap: 6px; font-weight: 500; }}

  /* Mobile Portrait UX Optimization */
  @media (max-width: 650px) {{
    .cal-header {{ flex-direction: row; justify-content: space-between; align-items: center; padding: 10px 12px; gap: 6px; }}
    .cal-title {{ font-size: 1.05rem; }}
    .nav-btns {{ gap: 4px; }}
    .nav-btn {{ padding: 6px 9px; font-size: 0.74rem; border-radius: 5px; }}
    .legend {{ padding: 8px 10px; gap: 6px; }}
    .legend-items {{ gap: 6px; }}
    .legend-pill {{ padding: 3px 7px; font-size: 0.68rem; }}
    .pill-holiday {{ font-size: 0.72rem; }}
    .updated-label {{ font-size: 0.66rem; width: 100%; }}
    .quote-prompt {{ padding: 5px 10px; font-size: 0.68rem; min-height: 30px; }}
    .clear-btn {{ padding: 2px 8px; font-size: 0.68rem; }}
    .weekdays {{ font-size: 0.68rem; padding: 5px 0; }}
    .day-cell {{ min-height: 52px; padding: 3px 2px; }}
    .day-top {{ height: 12px; }}
    .holiday-star {{ font-size: 0.72rem; }}
    .day-num {{ font-size: 0.68rem; }}
    .price {{ font-size: 0.74rem; letter-spacing: -0.02em; }}
    .min-stay {{ font-size: 0.50rem; letter-spacing: -0.02em; margin-top: 1px; }}
    .endpoint-badge {{ font-size: 0.44rem; padding: 1px 2px; letter-spacing: 0; }}
    .day-cell.unavail.checkout-eligible {{ outline-width: 2px; outline-offset: -2px; }}
    .quote-card {{ padding: 10px 12px; }}
    .quote-grid {{ flex-direction: column; align-items: flex-start; gap: 8px; }}
    .quote-details {{ font-size: 0.78rem; }}
    .quote-right {{ width: 100%; justify-content: space-between; }}
    .quote-total {{ font-size: 1.1rem; }}
    .quote-btn {{ padding: 8px 12px; font-size: 0.8rem; }}
    .form-row {{ grid-template-columns: 1fr; gap: 8px; }}
    .form-submit-row {{ flex-direction: column; align-items: stretch; }}
    .monthly-holidays {{ padding: 8px 10px; font-size: 0.74rem; }}
    .holiday-list-title {{ font-size: 0.68rem; margin-bottom: 4px; }}
    .holiday-list {{ flex-direction: column; gap: 3px; }}
  }}
</style>
</head>
<body>
<div class="cal-container">
  <div class="cal-header">
    <h2 class="cal-title" id="monthLabel">Loading...</h2>
    <div class="nav-btns">
      <button class="nav-btn" onclick="changeMonth(-1)">&#8592; Prev</button>
      <button class="nav-btn" onclick="goToday()">Today</button>
      <button class="nav-btn" onclick="changeMonth(1)">Next &#8594;</button>
    </div>
  </div>
  <div class="legend">
    <div class="legend-items">
      <span class="legend-pill pill-avail">Available</span>
      <span class="legend-pill pill-unavail">Unavailable</span>
      <span class="pill-holiday">★ US Holiday</span>
    </div>
    <span class="updated-label">Updated: {updated_str}</span>
  </div>
  <div class="quote-prompt">
    <span id="promptText">Click an available Check-In and Check-Out date to calculate your instant stay quote.</span>
    <button class="clear-btn" id="clearBtn" onclick="clearSelection()">Clear Dates</button>
  </div>
  <div class="weekdays">
    <div>Sun</div><div>Mon</div><div>Tue</div><div>Wed</div><div>Thu</div><div>Fri</div><div>Sat</div>
  </div>
  <div class="days-grid" id="daysGrid"></div>

  <div class="quote-card" id="quoteCard">
    <div class="quote-grid" id="quoteGrid">
      <div class="quote-details" id="quoteDetails"></div>
      <div class="quote-right">
        <div class="quote-total" id="quoteTotal"></div>
        <button type="button" class="quote-btn" id="inquireBtn" onclick="toggleInquiryForm()">Inquire to Book</button>
      </div>
    </div>
    <div class="quote-error" id="quoteError" style="display: none;"></div>

    <form class="inquiry-form" id="inquiryForm" onsubmit="submitInquiry(event)">
      <div class="form-row">
        <input type="text" class="form-input" id="guestName" placeholder="Your Full Name *" required>
        <input type="email" class="form-input" id="guestEmail" placeholder="Your Email Address *" required>
        <input type="tel" class="form-input" id="guestPhone" placeholder="Phone Number *" required>
        <input type="number" class="form-input" id="guestCount" placeholder="Number of Guests *" min="1" max="20" required>
      </div>
      <div class="form-submit-row">
        <input type="text" class="form-input" id="guestNotes" placeholder="Optional notes or questions...">
        <button type="submit" class="submit-btn" id="submitBtn">Send Booking Request</button>
      </div>
      <div class="form-status" id="formStatus"></div>
    </form>
  </div>

  <div class="monthly-holidays" id="monthlyHolidays" style="display: none;"></div>
</div>

<script>
  const rawData = {records_json};
  const CLEANING_FEE = {CLEANING_FEE};
  const WEB3FORMS_KEY = "{WEB3FORMS_ACCESS_KEY}";
  const dataMap = {{}};
  rawData.forEach(item => {{ dataMap[item.date] = item; }});

  let current = new Date();
  current.setDate(1);

  let checkInDate = null;
  let checkOutDate = null;
  let activeQuoteSummary = {{}};

  function parseLocal(dateStr) {{
    const [y, m, d] = dateStr.split('-').map(Number);
    return new Date(y, m - 1, d);
  }}

  function formatDisplayDate(dateStr) {{
    const d = parseLocal(dateStr);
    return d.toLocaleDateString('en-US', {{ month: 'short', day: 'numeric', year: 'numeric' }});
  }}

  function getPrevDateStr(dateStr) {{
    const d = parseLocal(dateStr);
    d.setDate(d.getDate() - 1);
    const y = d.getFullYear();
    const m = String(d.getMonth() + 1).padStart(2, '0');
    const day = String(d.getDate()).padStart(2, '0');
    return `${{y}}-${{m}}-${{day}}`;
  }}

  function getOpenWindowAfter(checkInStr) {{
    if (!checkInStr) return {{ firstUnavailableDate: null, openNightsCount: 0 }};
    const startObj = parseLocal(checkInStr);
    let openNights = 1;
    for (let i = 1; i <= 365; i++) {{
      const curr = new Date(startObj.getFullYear(), startObj.getMonth(), startObj.getDate() + i);
      const y = curr.getFullYear();
      const m = String(curr.getMonth() + 1).padStart(2, '0');
      const d = String(curr.getDate()).padStart(2, '0');
      const dKey = `${{y}}-${{m}}-${{d}}`;
      const dayInfo = dataMap[dKey];
      if (!dayInfo) return {{ firstUnavailableDate: null, openNightsCount: openNights }};
      if (!dayInfo.available) return {{ firstUnavailableDate: dKey, openNightsCount: openNights }};
      openNights++;
    }}
    return {{ firstUnavailableDate: null, openNightsCount: openNights }};
  }}

  function validateCheckInSelection(dateStr) {{
    const info = dataMap[dateStr];
    const reqMin = parseInt(info.min_stay, 10) || 2;
    const windowInfo = getOpenWindowAfter(dateStr);

    const quoteCard = document.getElementById("quoteCard");
    const quoteGrid = document.getElementById("quoteGrid");
    const errorBox = document.getElementById("quoteError");
    const inquiryForm = document.getElementById("inquiryForm");

    if (windowInfo.openNightsCount < reqMin && windowInfo.firstUnavailableDate) {{
      quoteCard.style.display = "block";
      quoteGrid.style.display = "none";
      inquiryForm.style.display = "none";
      errorBox.style.display = "block";
      errorBox.innerText = `Check-in on ${{formatDisplayDate(dateStr)}} does not meet the ${{reqMin}}-night minimum stay requirement because ${{formatDisplayDate(windowInfo.firstUnavailableDate)}} is already unavailable. Please select an earlier check-in date.`;
    }} else {{
      quoteCard.style.display = "none";
      errorBox.style.display = "none";
      inquiryForm.style.display = "none";
    }}
  }}

  function handleDayClick(dateStr) {{
    const info = dataMap[dateStr];
    if (!info) return;

    if (!checkInDate || (checkInDate && checkOutDate)) {{
      if (!info.available) return;
      checkInDate = dateStr;
      checkOutDate = null;
      document.getElementById("promptText").innerText = "";
      document.getElementById("clearBtn").style.display = "inline-block";
      validateCheckInSelection(checkInDate);
      renderCalendar();
      return;
    }}

    if (checkInDate && !checkOutDate) {{
      const clickedObj = parseLocal(dateStr);
      const startObj = parseLocal(checkInDate);

      if (clickedObj <= startObj) {{
        if (info.available) {{
          checkInDate = dateStr;
          document.getElementById("promptText").innerText = "";
          validateCheckInSelection(checkInDate);
        }} else {{
          clearSelection();
        }}
        renderCalendar();
        return;
      }}

      checkOutDate = dateStr;
      calculateQuote();
      renderCalendar();
    }}
  }}

  function clearSelection() {{
    checkInDate = null;
    checkOutDate = null;
    document.getElementById("promptText").innerText = "Click an available Check-In and Check-Out date to calculate your instant stay quote.";
    document.getElementById("clearBtn").style.display = "none";
    document.getElementById("quoteCard").style.display = "none";
    document.getElementById("inquiryForm").style.display = "none";
    renderCalendar();
  }}

  function toggleInquiryForm() {{
    const form = document.getElementById("inquiryForm");
    const status = document.getElementById("formStatus");
    status.style.display = "none";
    form.style.display = (form.style.display === "block") ? "none" : "block";
  }}

  async function submitInquiry(e) {{
    e.preventDefault();
    const btn = document.getElementById("submitBtn");
    const status = document.getElementById("formStatus");
    btn.disabled = true;
    btn.innerText = "Sending...";

    const payload = {{
      access_key: WEB3FORMS_KEY,
      subject: `Direct Booking Inquiry: ${{activeQuoteSummary.dates}} — ${{document.getElementById("guestName").value}}`,
      from_name: "Mammoth Charm Booking Calendar",
      name: document.getElementById("guestName").value,
      email: document.getElementById("guestEmail").value,
      phone: document.getElementById("guestPhone").value,
      number_of_guests: document.getElementById("guestCount").value,
      requested_dates: activeQuoteSummary.dates,
      total_nights: activeQuoteSummary.nights,
      estimated_total: activeQuoteSummary.total,
      rate_breakdown: activeQuoteSummary.breakdown,
      guest_notes: document.getElementById("guestNotes").value || "None"
    }};

    try {{
      const response = await fetch("https://api.web3forms.com/submit", {{
        method: "POST",
        headers: {{ "Content-Type": "application/json", "Accept": "application/json" }},
        body: JSON.stringify(payload)
      }});
      const result = await response.json();
      status.style.display = "block";
      if (response.status === 200) {{
        status.style.color = "#166534";
        status.innerText = "Booking inquiry sent. We will get back to you within 24 hours.";
        document.getElementById("inquiryForm").reset();
      }} else {{
        status.style.color = "#be123c";
        status.innerText = "Error sending request: " + (result.message || "Please try again.");
      }}
    }} catch (err) {{
      status.style.display = "block";
      status.style.color = "#be123c";
      status.innerText = "Network error. Please try again.";
    }}
    btn.disabled = false;
    btn.innerText = "Send Booking Request";
  }}

  function calculateQuote() {{
    const startObj = parseLocal(checkInDate);
    const endObj = parseLocal(checkOutDate);
    let curr = new Date(startObj);
    let nights = 0;
    let accommodationTotal = 0;
    let breakdownLines = [];
    let hasBlocked = false;

    const checkInInfo = dataMap[checkInDate];
    const requiredMinStay = parseInt(checkInInfo.min_stay, 10) || 1;

    while (curr < endObj) {{
      const y = curr.getFullYear();
      const m = String(curr.getMonth() + 1).padStart(2, '0');
      const d = String(curr.getDate()).padStart(2, '0');
      const dKey = `${{y}}-${{m}}-${{d}}`;
      const dayData = dataMap[dKey];

      if (!dayData || !dayData.available) {{
        hasBlocked = true;
        break;
      }}

      const price = parseFloat(dayData.price);
      accommodationTotal += price;
      breakdownLines.push(`${{dKey}}: $${{price}}`);
      nights++;
      curr.setDate(curr.getDate() + 1);
    }}

    const quoteCard = document.getElementById("quoteCard");
    const quoteGrid = document.getElementById("quoteGrid");
    const errorBox = document.getElementById("quoteError");
    const detailsBox = document.getElementById("quoteDetails");
    const totalBox = document.getElementById("quoteTotal");
    const inquiryForm = document.getElementById("inquiryForm");

    quoteCard.style.display = "block";
    inquiryForm.style.display = "none";

    if (hasBlocked) {{
      quoteGrid.style.display = "none";
      errorBox.style.display = "block";
      errorBox.innerText = "Selected range includes nights that are already unavailable. Please select an open date range.";
      return;
    }}

    if (nights < requiredMinStay) {{
      quoteGrid.style.display = "none";
      errorBox.style.display = "block";
      errorBox.innerText = `Minimum stay starting on ${{formatDisplayDate(checkInDate)}} is ${{requiredMinStay}} nights (you selected ${{nights}} night${{nights > 1 ? 's' : ''}}).`;
      return;
    }}

    errorBox.style.display = "none";
    quoteGrid.style.display = "flex";

    const grandTotal = accommodationTotal + CLEANING_FEE;
    const datesLabel = `${{formatDisplayDate(checkInDate)}} to ${{formatDisplayDate(checkOutDate)}}`;

    detailsBox.innerHTML = `
      <strong>${{formatDisplayDate(checkInDate)}} &rarr; ${{formatDisplayDate(checkOutDate)}}</strong> (${{nights}} Nights)<br>
      Nightly Subtotal: <strong>$${{accommodationTotal.toLocaleString()}}</strong> + Cleaning Fee: <strong>$${{CLEANING_FEE}}</strong>
    `;
    totalBox.innerText = `Total: $${{grandTotal.toLocaleString()}}`;

    activeQuoteSummary = {{
      dates: datesLabel,
      nights: `${{nights}} Nights`,
      total: `$${{grandTotal.toLocaleString()}} ($${{accommodationTotal.toLocaleString()}} Nightly Subtotal + $${{CLEANING_FEE}} Cleaning Fee)`,
      breakdown: breakdownLines.join(" | ")
    }};
  }}

  function renderCalendar() {{
    const year = current.getFullYear();
    const month = current.getMonth();
    const monthNames = ["January","February","March","April","May","June","July","August","September","October","November","December"];
    document.getElementById("monthLabel").innerText = monthNames[month] + " " + year;

    const firstDay = new Date(year, month, 1).getDay();
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const grid = document.getElementById("daysGrid");
    grid.innerHTML = "";
    
    let currentMonthHolidays = [];

    let eligibleTurnaroundDate = null;
    if (checkInDate && !checkOutDate && dataMap[checkInDate]) {{
      const reqMin = parseInt(dataMap[checkInDate].min_stay, 10) || 2;
      const winInfo = getOpenWindowAfter(checkInDate);
      if (winInfo.openNightsCount >= reqMin) {{
        eligibleTurnaroundDate = winInfo.firstUnavailableDate;
      }}
    }}

    for (let i = 0; i < firstDay; i++) {{
      const empty = document.createElement("div");
      empty.className = "day-cell empty";
      grid.appendChild(empty);
    }}

    for (let d = 1; d <= daysInMonth; d++) {{
      const cell = document.createElement("div");
      const mm = String(month + 1).padStart(2, '0');
      const dd = String(d).padStart(2, '0');
      const dateStr = `${{year}}-${{mm}}-${{dd}}`;
      const info = dataMap[dateStr];
      const prevInfo = dataMap[getPrevDateStr(dateStr)];

      let contentHtml = "";
      let starHtml = "";

      if (info && info.holiday) {{
        starHtml = `<span class="holiday-star" title="${{info.holiday}}">★</span>`;
        currentMonthHolidays.push({{ day: d, name: info.holiday }});
      }}

      const isCheckIn = (dateStr === checkInDate);
      const isCheckOut = (dateStr === checkOutDate);
      let isInRange = false;

      if (checkInDate && checkOutDate) {{
        const cDate = parseLocal(dateStr);
        isInRange = (cDate > parseLocal(checkInDate) && cDate < parseLocal(checkOutDate));
      }}

      if (!info) {{
        cell.className = "day-cell empty";
      }} else if (!info.available) {{
        cell.className = "day-cell unavail";

        if (prevInfo && prevInfo.available) {{
          cell.classList.add("turnaround-checkin-booked");
        }} else if (!prevInfo) {{
          cell.classList.add("turnaround-past-booked");
        }}

        if (dateStr === eligibleTurnaroundDate) {{
          cell.classList.add("checkout-eligible");
          cell.onclick = () => handleDayClick(dateStr);
        }}

        if (isCheckOut) {{
          cell.classList.add("selected-endpoint");
          contentHtml = `<div class="day-content"><span class="endpoint-badge">CHECK-OUT</span></div>`;
        }}
      }} else {{
        cell.className = "day-cell avail";
        cell.onclick = () => handleDayClick(dateStr);

        if (prevInfo && !prevInfo.available) {{
          cell.classList.add("turnaround-checkout-avail");
        }}

        let subLabel = `<span class="min-stay">${{info.min_stay}} Night Min</span>`;
        if (isCheckIn) {{
          cell.classList.add("selected-endpoint");
          subLabel = `<span class="endpoint-badge">CHECK-IN</span>`;
        }} else if (isCheckOut) {{
          cell.classList.add("selected-endpoint");
          subLabel = `<span class="endpoint-badge">CHECK-OUT</span>`;
        }} else if (isInRange) {{
          cell.classList.add("in-range");
        }}

        contentHtml = `<div class="day-content"><span class="price">$${{info.price}}</span>${{subLabel}}</div>`;
      }}

      cell.innerHTML = `<div class="day-top">${{starHtml}}<span class="day-num">${{d}}</span></div>${{contentHtml}}`;
      grid.appendChild(cell);
    }}
    
    const holidayContainer = document.getElementById("monthlyHolidays");
    if (currentMonthHolidays.length > 0) {{
      let listHtml = currentMonthHolidays.map(h => 
        `<li class="holiday-item"><span style="color:var(--star-color);">★</span> ${{monthNames[month]}} ${{h.day}}: ${{h.name}}</li>`
      ).join("");
      holidayContainer.innerHTML = `<div class="holiday-list-title">${{monthNames[month]}} Holidays</div><ul class="holiday-list">${{listHtml}}</ul>`;
      holidayContainer.style.display = "block";
    }} else {{
      holidayContainer.style.display = "none";
    }}
  }}

  function changeMonth(delta) {{
    current.setMonth(current.getMonth() + delta);
    renderCalendar();
  }}

  function goToday() {{
    current = new Date();
    current.setDate(1);
    renderCalendar();
  }}

  renderCalendar();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# 4. Dedicated Cleaner Turnover Schedule Dashboard Builder (cleaner.html)
# ---------------------------------------------------------------------------
class CleanerCalendarBuilder:
    """
    Builds a standalone, read-only Cleaner Turnover Schedule dashboard (cleaner.html)
    featuring:
      - Section A: Upcoming Cleaning Windows summary list (Next 8 turnovers)
      - Section B: Monthly Turnover Calendar Grid with color-coded cells (no cell text),
        white cells for past/today, and Pink + Amber Outline + Amber Diagonal for Same-Day Turnovers.
    """

    def __init__(self, title: str = "Mammoth Charm - Cleaning & Turnover Schedule"):
        self.title = title

    def _build_cleaner_dataset(
        self,
        event_blocks: List[Dict[str, str]],
        raw_booked_dates: Set[str]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        if ZoneInfo:
            today = datetime.now(ZoneInfo(TIMEZONE_STR)).date()
        else:
            today = (datetime.utcnow() - timedelta(hours=7)).date()

        # Active tracking begins at today + 1 day (consistent with Guest Calendar & 1-day advance notice)
        active_start = today + timedelta(days=1)
        end_window = active_start + timedelta(days=365)

        holiday_catalog = {}
        for y in range(today.year, today.year + 3):
            holiday_catalog.update(calculate_us_holidays(y))

        # Clamp/filter event blocks to active_start so Airbnb's automatic 1-day advance-notice block
        # on 'today' is ignored, matching the Direct Booking Calendar.
        active_blocks: List[Dict[str, str]] = []
        for b in event_blocks:
            s_obj = datetime.strptime(b["start"], "%Y-%m-%d").date()
            e_obj = datetime.strptime(b["end"], "%Y-%m-%d").date()
            if e_obj <= active_start:
                continue
            clamped_start = max(s_obj, active_start)
            active_blocks.append({
                "start": clamped_start.strftime("%Y-%m-%d"),
                "end": e_obj.strftime("%Y-%m-%d"),
                "label": "Booked"
            })

        # Fallback if no VEVENT blocks were parsed
        if not active_blocks and raw_booked_dates:
            future_dates = sorted([
                d for d in raw_booked_dates
                if datetime.strptime(d, "%Y-%m-%d").date() >= active_start
            ])
            if future_dates:
                b_start = future_dates[0]
                prev_d = datetime.strptime(b_start, "%Y-%m-%d").date()
                for d_str in future_dates[1:]:
                    curr_d = datetime.strptime(d_str, "%Y-%m-%d").date()
                    if (curr_d - prev_d).days > 1:
                        active_blocks.append({
                            "start": b_start,
                            "end": (prev_d + timedelta(days=1)).strftime("%Y-%m-%d"),
                            "label": "Booked"
                        })
                        b_start = d_str
                    prev_d = curr_d
                active_blocks.append({
                    "start": b_start,
                    "end": (prev_d + timedelta(days=1)).strftime("%Y-%m-%d"),
                    "label": "Booked"
                })

        active_blocks.sort(key=lambda x: (x["start"], x["end"]))

        checkin_dates: Set[str] = set()
        checkout_dates: Set[str] = set()
        occupied_nights: Set[str] = set()

        for b in active_blocks:
            checkin_dates.add(b["start"])
            checkout_dates.add(b["end"])
            s_obj = datetime.strptime(b["start"], "%Y-%m-%d").date()
            e_obj = datetime.strptime(b["end"], "%Y-%m-%d").date()
            c_obj = s_obj
            while c_obj < e_obj:
                occupied_nights.add(c_obj.strftime("%Y-%m-%d"))
                c_obj += timedelta(days=1)

        # Build daily records starting at active_start (so dates <= today render white/past)
        daily_records: List[Dict[str, Any]] = []
        curr = active_start
        while curr <= end_window:
            d_str = curr.strftime("%Y-%m-%d")
            is_in = d_str in checkin_dates
            is_out = d_str in checkout_dates
            is_occ = d_str in occupied_nights

            if is_in and is_out:
                day_state = "SAME_DAY_TURNOVER"
            elif is_in:
                if curr == active_start:
                    # Immediately follows white unbookable/today cell (e.g., Oct 23): Upper-Left White / Lower-Right Pink
                    day_state = "CHECK_IN_FROM_PAST"
                else:
                    day_state = "CHECK_IN"
            elif is_out and not is_occ:
                day_state = "CHECK_OUT"
            elif is_occ:
                day_state = "BOOKED"
            else:
                day_state = "OPEN"

            daily_records.append({
                "date": d_str,
                "state": day_state,
                "holiday": holiday_catalog.get(d_str, "")
            })
            curr += timedelta(days=1)

        # Build Upcoming Cleaning Windows summary list (next 8 turnovers)
        upcoming_windows: List[Dict[str, Any]] = []
        for idx, b in enumerate(active_blocks):
            co_str = b["end"]
            co_date = datetime.strptime(co_str, "%Y-%m-%d").date()

            next_ci_str = None
            for next_b in active_blocks[idx + 1:]:
                if next_b["start"] >= co_str:
                    next_ci_str = next_b["start"]
                    break

            if next_ci_str == co_str:
                upcoming_windows.append({
                    "type": "SAME_DAY",
                    "checkout_date": co_str,
                    "next_checkin_date": next_ci_str,
                    "gap_days": 0
                })
            elif next_ci_str:
                next_ci_date = datetime.strptime(next_ci_str, "%Y-%m-%d").date()
                gap_days = (next_ci_date - co_date).days
                upcoming_windows.append({
                    "type": "WINDOW",
                    "checkout_date": co_str,
                    "next_checkin_date": next_ci_str,
                    "gap_days": gap_days
                })
            else:
                upcoming_windows.append({
                    "type": "OPEN_ENDED",
                    "checkout_date": co_str,
                    "next_checkin_date": None,
                    "gap_days": None
                })

            if len(upcoming_windows) >= 8:
                break

        return daily_records, upcoming_windows

    def build_html(self, event_blocks: List[Dict[str, str]], raw_booked_dates: Set[str]) -> str:
        daily_records, upcoming_windows = self._build_cleaner_dataset(event_blocks, raw_booked_dates)
        records_json = json.dumps(daily_records)
        windows_json = json.dumps(upcoming_windows)

        if ZoneInfo:
            now_local = datetime.now(ZoneInfo(TIMEZONE_STR))
        else:
            now_local = datetime.utcnow() - timedelta(hours=7)

        date_part = now_local.strftime("%b %d, %Y")
        time_part = now_local.strftime("%I:%M %p").lstrip("0").lower()
        updated_str = f"{date_part} at {time_part}"

        # Also provide full holiday map to JS so past dates in current month still show holiday stars if applicable
        holiday_map = {}
        for y in range(now_local.year, now_local.year + 3):
            holiday_map.update(calculate_us_holidays(y))
        holidays_json = json.dumps(holiday_map)

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{self.title}</title>
<style>
  :root {{
    --bg-open: #dcfce7;
    --border-open: #86efac;
    --text-open: #166534;
    --bg-booked: #ffe4e6;
    --border-booked: #fda4af;
    --text-booked: #9f1239;
    --border-turnover: #f59e0b;
    --text-turnover: #92400e;
    --bg-past: #f8fafc;
    --header-bg: #00c4b3;
    --header-text: #0f172a;
    --star-color: #0f172a;
  }}
  * {{ box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  body {{ margin: 0; padding: 0; background: transparent; color: #1e293b; overflow-x: hidden; width: 100%; }}

  .dashboard-wrap {{ width: 100%; max-width: 900px; margin: 0 auto; display: flex; flex-direction: column; gap: 16px; }}

  /* Section A: Upcoming Cleaning Windows Card */
  .windows-card {{ border: 1px solid #cbd5e1; border-radius: 6px; background: #ffffff; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }}
  .windows-header {{ background: #0f172a; color: #ffffff; padding: 12px 18px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }}
  .windows-title {{ font-size: 1.05rem; font-weight: 800; margin: 0; letter-spacing: 0.02em; text-transform: uppercase; }}
  .windows-sub {{ font-size: 0.78rem; color: #94a3b8; font-weight: 600; }}
  .windows-list {{ padding: 12px 16px; display: flex; flex-direction: column; gap: 8px; }}
  .window-row {{ display: flex; justify-content: space-between; align-items: center; padding: 10px 12px; border-radius: 6px; border: 1px solid #e2e8f0; background: #f8fafc; gap: 10px; flex-wrap: wrap; }}
  .window-row.urgent {{ background: #fffbeb; border: 1.5px solid #f59e0b; }}
  .win-badge {{ font-size: 0.72rem; font-weight: 800; padding: 4px 8px; border-radius: 4px; text-transform: uppercase; white-space: nowrap; }}
  .win-badge.urgent {{ background: #f59e0b; color: #0f172a; }}
  .win-badge.normal {{ background: var(--bg-open); color: var(--text-open); border: 1px solid var(--border-open); }}
  .win-details {{ font-size: 0.86rem; color: #1e293b; font-weight: 600; flex: 1; min-width: 200px; }}
  .win-meta {{ font-size: 0.78rem; color: #475569; font-weight: 700; white-space: nowrap; }}

  /* Section B: Monthly Turnover Calendar Grid */
  .cal-container {{ width: 100%; border: 1px solid #cbd5e1; border-radius: 6px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.05); background: #ffffff; }}
  .cal-header {{ display: flex; justify-content: space-between; align-items: center; padding: 14px 18px; background: #0f172a; color: #ffffff; }}
  .cal-title {{ font-size: 1.35rem; font-weight: 800; margin: 0; color: #ffffff; letter-spacing: -0.01em; }}
  .nav-btns {{ display: flex; gap: 8px; align-items: center; }}
  .nav-btn {{ background: #00c4b3; border: none; color: #0f172a; padding: 8px 14px; border-radius: 6px; cursor: pointer; font-weight: 700; font-size: 0.88rem; transition: background 0.15s; white-space: nowrap; }}
  .nav-btn:hover {{ background: #00a89a; }}

  .legend {{ display: flex; gap: 10px; padding: 10px 18px; background: #f8fafc; border-bottom: 1px solid #e2e8f0; align-items: center; justify-content: space-between; flex-wrap: wrap; }}
  .legend-items {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
  .legend-pill {{ display: inline-flex; align-items: center; padding: 4px 9px; border-radius: 4px; font-size: 0.76rem; font-weight: 700; }}
  .pill-open {{ background: var(--bg-open); color: var(--text-open); border: 1px solid var(--border-open); }}
  .pill-booked {{ background: var(--bg-booked); color: #be123c; border: 1px solid var(--border-booked); }}
  .pill-turnover {{ background: var(--bg-booked); color: var(--text-turnover); border: 2px solid var(--border-turnover); }}
  .pill-holiday {{ background: transparent; color: var(--star-color); font-weight: 800; font-size: 0.8rem; padding: 0 2px; }}
  .updated-label {{ color: #64748b; font-size: 0.74rem; font-weight: 500; }}

  .weekdays {{ display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); background: #f1f5f9; border-bottom: 1px solid #e2e8f0; text-align: center; font-weight: 700; font-size: 0.8rem; padding: 8px 0; color: #334155; }}
  .days-grid {{ display: grid; grid-template-columns: repeat(7, minmax(0, 1fr)); gap: 1px; background: #cbd5e1; }}

  .day-cell {{ min-height: 74px; background: #fff; padding: 6px; display: flex; flex-direction: column; justify-content: space-between; user-select: none; min-width: 0; overflow: hidden; }}
  .day-cell.empty {{ background: var(--bg-past); }}

  .day-cell.state-open {{ background: var(--bg-open); }}
  .day-cell.state-booked {{ background: var(--bg-booked); }}
  .day-cell.state-checkin {{ background: linear-gradient(135deg, var(--bg-open) 50%, var(--bg-booked) 50%); }}
  .day-cell.state-checkin-past {{ background: linear-gradient(135deg, var(--bg-past) 50%, var(--bg-booked) 50%); }}
  .day-cell.state-checkout {{ background: linear-gradient(135deg, var(--bg-booked) 50%, var(--bg-open) 50%); }}

  /* Same-Day Turnover: Light Pink Background + Solid Amber Outline + Solid Amber Diagonal Line */
  .day-cell.state-turnover {{
    background: linear-gradient(
      135deg,
      var(--bg-booked) calc(50% - 1.5px),
      var(--border-turnover) calc(50% - 1.5px),
      var(--border-turnover) calc(50% + 1.5px),
      var(--bg-booked) calc(50% + 1.5px)
    );
    outline: 2.5px solid var(--border-turnover);
    outline-offset: -2.5px;
  }}

  .day-top {{ display: flex; justify-content: space-between; align-items: flex-start; width: 100%; height: 16px; }}
  .holiday-star {{ font-size: 1rem; color: var(--star-color); line-height: 1; }}
  .day-num {{ font-size: 0.85rem; font-weight: 700; text-align: right; color: #334155; margin-left: auto; line-height: 1; }}

  .monthly-holidays {{ background: #f8fafc; padding: 12px 18px; font-size: 0.85rem; color: #334155; border-top: 1px solid #cbd5e1; }}
  .holiday-list-title {{ font-weight: 800; margin-bottom: 6px; color: #0f172a; text-transform: uppercase; font-size: 0.75rem; letter-spacing: 0.05em; }}
  .holiday-list {{ margin: 0; padding: 0; list-style: none; display: flex; flex-wrap: wrap; gap: 12px 24px; }}
  .holiday-item {{ display: flex; align-items: center; gap: 6px; font-weight: 500; }}

  @media (max-width: 650px) {{
    .windows-header {{ padding: 10px 12px; }}
    .windows-title {{ font-size: 0.92rem; }}
    .windows-sub {{ font-size: 0.7rem; }}
    .windows-list {{ padding: 10px; gap: 6px; }}
    .window-row {{ padding: 8px 10px; gap: 6px; }}
    .win-badge {{ font-size: 0.64rem; padding: 3px 6px; }}
    .win-details {{ font-size: 0.78rem; min-width: 100%; }}
    .win-meta {{ font-size: 0.72rem; }}

    .cal-header {{ padding: 10px 12px; gap: 6px; }}
    .cal-title {{ font-size: 1.05rem; }}
    .nav-btns {{ gap: 4px; }}
    .nav-btn {{ padding: 6px 9px; font-size: 0.74rem; border-radius: 5px; }}
    .legend {{ padding: 8px 10px; gap: 6px; }}
    .legend-items {{ gap: 5px; }}
    .legend-pill {{ padding: 3px 6px; font-size: 0.65rem; }}
    .pill-holiday {{ font-size: 0.7rem; }}
    .updated-label {{ font-size: 0.65rem; width: 100%; }}
    .weekdays {{ font-size: 0.68rem; padding: 5px 0; }}
    .day-cell {{ min-height: 52px; padding: 3px 2px; }}
    .day-cell.state-turnover {{ outline-width: 2px; outline-offset: -2px; }}
    .day-top {{ height: 12px; }}
    .holiday-star {{ font-size: 0.72rem; }}
    .day-num {{ font-size: 0.68rem; }}
    .monthly-holidays {{ padding: 8px 10px; font-size: 0.74rem; }}
    .holiday-list-title {{ font-size: 0.68rem; margin-bottom: 4px; }}
    .holiday-list {{ flex-direction: column; gap: 3px; }}
  }}
</style>
</head>
<body>
<div class="dashboard-wrap">

  <!-- Section A: Upcoming Cleaning Windows -->
  <div class="windows-card">
    <div class="windows-header">
      <h2 class="windows-title">Upcoming Cleaning Windows</h2>
      <span class="windows-sub">Check-Out: 10:00 AM &bull; Check-In: 4:00 PM</span>
    </div>
    <div class="windows-list" id="windowsList"></div>
  </div>

  <!-- Section B: Monthly Turnover Calendar -->
  <div class="cal-container">
    <div class="cal-header">
      <h2 class="cal-title" id="monthLabel">Loading...</h2>
      <div class="nav-btns">
        <button class="nav-btn" onclick="changeMonth(-1)">&#8592; Prev</button>
        <button class="nav-btn" onclick="goToday()">Today</button>
        <button class="nav-btn" onclick="changeMonth(1)">Next &#8594;</button>
      </div>
    </div>
    <div class="legend">
      <div class="legend-items">
        <span class="legend-pill pill-open">Open (Available to Clean)</span>
        <span class="legend-pill pill-booked">Booked</span>
        <span class="legend-pill pill-turnover">Same-Day Turnover</span>
        <span class="pill-holiday">★ US Holiday</span>
      </div>
      <span class="updated-label">Updated: {updated_str}</span>
    </div>
    <div class="weekdays">
      <div>Sun</div><div>Mon</div><div>Tue</div><div>Wed</div><div>Thu</div><div>Fri</div><div>Sat</div>
    </div>
    <div class="days-grid" id="daysGrid"></div>
    <div class="monthly-holidays" id="monthlyHolidays" style="display: none;"></div>
  </div>

</div>

<script>
  const rawData = {records_json};
  const upcomingWindows = {windows_json};
  const holidayCatalog = {holidays_json};
  const dataMap = {{}};
  rawData.forEach(item => {{ dataMap[item.date] = item; }});

  let current = new Date();
  current.setDate(1);

  function parseLocal(dateStr) {{
    const [y, m, d] = dateStr.split('-').map(Number);
    return new Date(y, m - 1, d);
  }}

  function formatShortDate(dateStr) {{
    const d = parseLocal(dateStr);
    return d.toLocaleDateString('en-US', {{ weekday: 'short', month: 'short', day: 'numeric' }});
  }}

  function renderWindowsList() {{
    const container = document.getElementById("windowsList");
    if (!upcomingWindows || upcomingWindows.length === 0) {{
      container.innerHTML = `<div class="window-row"><div class="win-details">No upcoming check-outs currently scheduled.</div></div>`;
      return;
    }}

    container.innerHTML = upcomingWindows.map(w => {{
      if (w.type === "SAME_DAY") {{
        return `
          <div class="window-row urgent">
            <span class="win-badge urgent">SAME-DAY TURNOVER</span>
            <div class="win-details">
              <strong>${{formatShortDate(w.checkout_date)}}</strong> &mdash; Clean between <strong>10:00 AM &ndash; 4:00 PM</strong>
            </div>
            <span class="win-meta">Next Guest In Today @ 4:00 PM</span>
          </div>
        `;
      }} else if (w.type === "WINDOW") {{
        const dayWordPlural = w.gap_days === 1 ? "Day" : "Days";
        return `
          <div class="window-row">
            <span class="win-badge normal">${{w.gap_days}}-DAY WINDOW</span>
            <div class="win-details">
              <strong>Out:</strong> ${{formatShortDate(w.checkout_date)}} (10:00 AM) &rarr; <strong>Next In:</strong> ${{formatShortDate(w.next_checkin_date)}} (4:00 PM)
            </div>
            <span class="win-meta">${{w.gap_days}} ${{dayWordPlural}} Available to Clean</span>
          </div>
        `;
      }} else {{
        return `
          <div class="window-row">
            <span class="win-badge normal">OPEN WINDOW</span>
            <div class="win-details">
              <strong>Out:</strong> ${{formatShortDate(w.checkout_date)}} (10:00 AM) &rarr; Open calendar afterward
            </div>
            <span class="win-meta">Flexible Clean</span>
          </div>
        `;
      }}
    }}).join("");
  }}

  function renderCalendar() {{
    const year = current.getFullYear();
    const month = current.getMonth();
    const monthNames = ["January","February","March","April","May","June","July","August","September","October","November","December"];
    document.getElementById("monthLabel").innerText = monthNames[month] + " " + year;

    const firstDay = new Date(year, month, 1).getDay();
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const grid = document.getElementById("daysGrid");
    grid.innerHTML = "";

    let currentMonthHolidays = [];

    for (let i = 0; i < firstDay; i++) {{
      const empty = document.createElement("div");
      empty.className = "day-cell empty";
      grid.appendChild(empty);
    }}

    for (let d = 1; d <= daysInMonth; d++) {{
      const cell = document.createElement("div");
      const mm = String(month + 1).padStart(2, '0');
      const dd = String(d).padStart(2, '0');
      const dateStr = `${{year}}-${{mm}}-${{dd}}`;
      const info = dataMap[dateStr];
      const holidayName = (info && info.holiday) ? info.holiday : (holidayCatalog[dateStr] || "");

      let starHtml = "";
      if (holidayName) {{
        starHtml = `<span class="holiday-star" title="${{holidayName}}">★</span>`;
        currentMonthHolidays.push({{ day: d, name: holidayName }});
      }}

      if (!info) {{
        cell.className = "day-cell empty";
      }} else {{
        cell.className = "day-cell";
        if (info.state === "SAME_DAY_TURNOVER") {{
          cell.classList.add("state-turnover");
        }} else if (info.state === "CHECK_IN_FROM_PAST") {{
          cell.classList.add("state-checkin-past");
        }} else if (info.state === "CHECK_IN") {{
          cell.classList.add("state-checkin");
        }} else if (info.state === "CHECK_OUT") {{
          cell.classList.add("state-checkout");
        }} else if (info.state === "BOOKED") {{
          cell.classList.add("state-booked");
        }} else {{
          cell.classList.add("state-open");
        }}
      }}

      cell.innerHTML = `<div class="day-top">${{starHtml}}<span class="day-num">${{d}}</span></div>`;
      grid.appendChild(cell);
    }}

    const holidayContainer = document.getElementById("monthlyHolidays");
    if (currentMonthHolidays.length > 0) {{
      let listHtml = currentMonthHolidays.map(h =>
        `<li class="holiday-item"><span style="color:var(--star-color);">★</span> ${{monthNames[month]}} ${{h.day}}: ${{h.name}}</li>`
      ).join("");
      holidayContainer.innerHTML = `<div class="holiday-list-title">${{monthNames[month]}} Holidays</div><ul class="holiday-list">${{listHtml}}</ul>`;
      holidayContainer.style.display = "block";
    }} else {{
      holidayContainer.style.display = "none";
    }}
  }}

  function changeMonth(delta) {{
    current.setMonth(current.getMonth() + delta);
    renderCalendar();
  }}

  function goToday() {{
    current = new Date();
    current.setDate(1);
    renderCalendar();
  }}

  renderWindowsList();
  renderCalendar();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main Execution Pipeline
# ---------------------------------------------------------------------------
def main():
    logger.info("=== Starting PriceLabs + Airbnb iCal + Cleaner Schedule Sync ===")

    # 1. Fetch live blocked/booked nights & explicit reservation blocks directly from Airbnb iCal
    airbnb_extractor = AirbnbICalExtractor(ical_url=AIRBNB_ICAL_URL)
    airbnb_blocked_dates, airbnb_event_blocks = airbnb_extractor.fetch_ical_data()

    # 2. Fetch rates and min-stays from PriceLabs, overriding availability with Airbnb iCal
    extractor = PriceLabsExtractor(
        api_key=PRICELABS_API_KEY,
        listing_id=PRICELABS_LISTING_ID,
        pms=PRICELABS_PMS_NAME,
        airbnb_blocked=airbnb_blocked_dates
    )
    standard_records, discounted_records, raw_booked_dates = extractor.fetch_pricing()

    # 3. Build RFC 5545 iCalendar (.ics) feeds
    std_builder = ICSCalendarBuilder(calendar_name="PriceLabs")
    std_ics = std_builder.build_ics(standard_records, uid_prefix="pricelabs-std")
    with open(OUTPUT_STANDARD_FILENAME, "w", encoding="utf-8") as f:
        f.write(std_ics)
    
    disc_builder = ICSCalendarBuilder(calendar_name="PriceLabs Discounted")
    disc_ics = disc_builder.build_ics(discounted_records, uid_prefix="pricelabs-disc")
    with open(OUTPUT_DISCOUNTED_FILENAME, "w", encoding="utf-8") as f:
        f.write(disc_ics)

    # 4. Build Guest HTML Calendars (Standard & 25% Discounted)
    std_html_builder = HTMLCalendarBuilder(title="Mammoth Charm - Rates & Availability")
    with open(OUTPUT_STANDARD_HTML, "w", encoding="utf-8") as f:
        f.write(std_html_builder.build_html(standard_records))

    disc_html_builder = HTMLCalendarBuilder(title="Mammoth Charm - Direct Rates (25% Off)")
    with open(OUTPUT_DISCOUNTED_HTML, "w", encoding="utf-8") as f:
        f.write(disc_html_builder.build_html(discounted_records))

    # 5. Build Dedicated Cleaner Turnover Schedule Dashboard (cleaner.html)
    cleaner_html_builder = CleanerCalendarBuilder(title="Mammoth Charm - Cleaning & Turnover Schedule")
    with open(OUTPUT_CLEANER_HTML, "w", encoding="utf-8") as f:
        f.write(cleaner_html_builder.build_html(airbnb_event_blocks, raw_booked_dates))

    logger.info("=== All Guest & Cleaner Calendars Synced Successfully ===")


if __name__ == "__main__":
    main()
