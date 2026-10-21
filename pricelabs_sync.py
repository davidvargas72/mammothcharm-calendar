#!/usr/bin/env python3
"""
PriceLabs Dynamic Pricing Calendar Automation
=============================================
Automates the extraction of PriceLabs dynamic pricing and minimum stay rules
PLUS real-time booked/blocked dates directly from Airbnb's iCalendar (.ics) feed
for Mammoth Charm LLC. Builds two RFC 5545 compliant iCalendar (.ics) feeds
AND two interactive HTML monthly calendar grids (Standard and 25% Discounted).

Author: Mammoth Charm LLC Operations
Version: 3.7.0 (Direct Airbnb iCal Sync + 24hr Inquiry Confirmation + Mandatory Guest Count)
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
# 0. Direct Airbnb iCalendar (.ics) Availability Extractor
# ---------------------------------------------------------------------------
class AirbnbICalExtractor:
    """Fetches and parses Airbnb's direct iCal feed to extract all booked/blocked dates."""

    def __init__(self, ical_url: str):
        self.ical_url = ical_url

    def fetch_blocked_dates(self) -> Set[str]:
        blocked_dates: Set[str] = set()
        if not self.ical_url:
            return blocked_dates

        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; MammothCharmCalendarSync/3.7; +https://mammothcharm.com)"
        }

        try:
            logger.info("Fetching live availability directly from Airbnb iCal feed...")
            response = requests.get(self.ical_url, headers=headers, timeout=20)
            if response.status_code != 200:
                logger.warning(f"Airbnb iCal returned HTTP {response.status_code}. Falling back to PriceLabs availability.")
                return blocked_dates

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
                        curr = start_date
                        # In iCal, DTEND is exclusive (morning of check-out)
                        while curr < end_date:
                            blocked_dates.add(curr.strftime("%Y-%m-%d"))
                            curr += timedelta(days=1)
                    except ValueError:
                        continue

            logger.info(f"Successfully parsed {len(blocked_dates)} booked/blocked nights directly from Airbnb iCal.")
        except Exception as e:
            logger.warning(f"Failed to fetch Airbnb iCal ({e}). Falling back to PriceLabs availability.")

        return blocked_dates


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

    def _normalize_records(self, raw_records: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
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

            is_avail = self._determine_availability(r, d_str)
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

        return standard_records, discounted_records


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
# 3. Interactive HTML Calendar Grid Builder (With Built-In Inquiry Form)
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
        updated_str = now_local.strftime("%b %d, %Y")

        return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{self.title}</title>
<style>
  :root {{
    --bg-avail: #f0fdf4;
    --border-avail: #86efac;
    --text-avail: #166534;
    --bg-unavail: #ffe4e6;
    --border-unavail: #fda4af;
    --header-bg: #00c4b3;
    --header-text: #0f172a;
    --star-color: #d97706;
  }}
  * {{ box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  
  body {{ margin: 0; padding: 0; background: transparent; color: #1e293b; overflow-x: hidden; }}
  
  .cal-container {{ max-width: 900px; margin: 0 auto; border: 1px solid #cbd5e1; border-radius: 6px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.05); background: #ffffff; }}
  
  .cal-header {{ display: flex; justify-content: space-between; align-items: center; padding: 14px 18px; background: var(--header-bg); color: var(--header-text); }}
  .cal-title {{ font-size: 1.35rem; font-weight: 800; margin: 0; color: var(--header-text); letter-spacing: -0.01em; }}
  .nav-btns {{ display: flex; gap: 8px; align-items: center; }}
  .nav-btn {{ background: #0f172a; border: none; color: #ffffff; padding: 8px 14px; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 0.88rem; transition: background 0.15s; }}
  .nav-btn:hover {{ background: #1e293b; }}

  .legend {{ display: flex; gap: 12px; padding: 10px 18px; background: #f8fafc; border-bottom: 1px solid #e2e8f0; align-items: center; justify-content: space-between; flex-wrap: wrap; }}
  .legend-items {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
  .legend-pill {{ display: inline-flex; align-items: center; padding: 4px 10px; border-radius: 4px; font-size: 0.8rem; font-weight: 700; }}
  .pill-avail {{ background: var(--bg-avail); color: var(--text-avail); border: 1px solid var(--border-avail); }}
  .pill-unavail {{ background: var(--bg-unavail); color: #be123c; border: 1px solid var(--border-unavail); }}
  .pill-holiday {{ background: transparent; color: var(--star-color); font-weight: 800; font-size: 0.85rem; padding: 0; }}
  .updated-label {{ color: #64748b; font-size: 0.75rem; font-weight: 500; }}

  .quote-prompt {{ background: #f0fdfa; border-bottom: 1px solid #ccfbf1; padding: 7px 18px; font-size: 0.8rem; font-weight: 600; color: #0f766e; display: flex; justify-content: space-between; align-items: center; }}
  .clear-btn {{ background: transparent; border: 1px solid #0f766e; color: #0f766e; border-radius: 4px; padding: 2px 8px; font-size: 0.72rem; font-weight: 700; cursor: pointer; display: none; }}
  .clear-btn:hover {{ background: #ccfbf1; }}

  .weekdays {{ display: grid; grid-template-columns: repeat(7, 1fr); background: #f1f5f9; border-bottom: 1px solid #e2e8f0; text-align: center; font-weight: 700; font-size: 0.8rem; padding: 8px 0; color: #334155; }}
  .days-grid {{ display: grid; grid-template-columns: repeat(7, 1fr); gap: 1px; background: #cbd5e1; }}
  
  .day-cell {{ min-height: 75px; background: #fff; padding: 6px; display: flex; flex-direction: column; justify-content: space-between; transition: background 0.12s; user-select: none; }}
  .day-cell.empty {{ background: #f8fafc; }}
  .day-cell.avail {{ background: var(--bg-avail); cursor: pointer; }}
  .day-cell.avail:hover {{ filter: brightness(0.96); }}
  .day-cell.unavail {{ background: var(--bg-unavail); }}
  .day-cell.unavail.checkout-eligible {{ cursor: pointer; }}

  .day-cell.selected-endpoint {{ background: var(--header-bg) !important; color: var(--header-text) !important; }}
  .day-cell.selected-endpoint .price,
  .day-cell.selected-endpoint .min-stay,
  .day-cell.selected-endpoint .day-num {{ color: var(--header-text) !important; }}
  .day-cell.in-range {{ background: #ccfbf1 !important; }}
  .endpoint-badge {{ font-size: 0.62rem; font-weight: 800; text-transform: uppercase; background: #0f172a; color: #fff; padding: 1px 4px; border-radius: 3px; display: inline-block; margin-top: 2px; }}

  .day-top {{ display: flex; justify-content: space-between; align-items: flex-start; width: 100%; height: 16px; }}
  .holiday-star {{ font-size: 1rem; color: var(--star-color); line-height: 1; text-shadow: 0 1px 1px rgba(0,0,0,0.1); }}
  .day-num {{ font-size: 0.85rem; font-weight: 700; text-align: right; color: #475569; margin-left: auto; }}
  
  .day-content {{ text-align: center; margin: auto 0; }}
  .price {{ font-size: 1.05rem; font-weight: 800; color: var(--text-avail); display: block; }}
  .min-stay {{ font-size: 0.72rem; color: #334155; display: block; margin-top: 2px; font-weight: 600; }}

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

  @media (max-width: 650px) {{
    .cal-header {{ flex-direction: column; gap: 10px; align-items: flex-start; }}
    .day-cell {{ min-height: 60px; padding: 4px; }}
    .price {{ font-size: 0.85rem; }}
    .min-stay {{ font-size: 0.62rem; }}
    .endpoint-badge {{ font-size: 0.52rem; padding: 1px 2px; }}
    .quote-grid {{ flex-direction: column; align-items: flex-start; }}
    .quote-right {{ width: 100%; justify-content: space-between; }}
    .form-row {{ grid-template-columns: 1fr; }}
    .form-submit-row {{ flex-direction: column; align-items: stretch; }}
    .monthly-holidays {{ padding: 10px 14px; }}
    .holiday-list {{ flex-direction: column; gap: 4px; }}
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
      <span class="legend-pill pill-avail">Available ($ Rate &amp; Min Stay)</span>
      <span class="legend-pill pill-unavail">Booked</span>
      <span class="pill-holiday">★ US Holiday</span>
    </div>
    <span class="updated-label">Updated: {updated_str}</span>
  </div>
  <div class="quote-prompt">
    <span id="promptText">💡 Click an available Check-In and Check-Out date to calculate your instant stay quote.</span>
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

  function handleDayClick(dateStr) {{
    const info = dataMap[dateStr];
    if (!info) return;

    if (!checkInDate || (checkInDate && checkOutDate)) {{
      if (!info.available) return;
      checkInDate = dateStr;
      checkOutDate = null;
      document.getElementById("promptText").innerText = "Check-In: " + formatDisplayDate(checkInDate) + " — Now click your Check-Out date.";
      document.getElementById("clearBtn").style.display = "inline-block";
      document.getElementById("quoteCard").style.display = "none";
      document.getElementById("inquiryForm").style.display = "none";
      renderCalendar();
      return;
    }}

    if (checkInDate && !checkOutDate) {{
      const clickedObj = parseLocal(dateStr);
      const startObj = parseLocal(checkInDate);

      if (clickedObj <= startObj) {{
        if (info.available) {{
          checkInDate = dateStr;
          document.getElementById("promptText").innerText = "Check-In: " + formatDisplayDate(checkInDate) + " — Now click your Check-Out date.";
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
    document.getElementById("promptText").innerText = "💡 Click an available Check-In and Check-Out date to calculate your instant stay quote.";
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
        status.innerText = "✓ Booking inquiry sent! We will get back to you within 24 hours.";
        document.getElementById("inquiryForm").reset();
      }} else {{
        status.style.color = "#be123c";
        status.innerText = "⚠️ Error sending request: " + (result.message || "Please try again.");
      }}
    }} catch (err) {{
      status.style.display = "block";
      status.style.color = "#be123c";
      status.innerText = "⚠️ Network error. Please try again.";
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
      errorBox.innerText = "⚠️ Selected range includes nights that are already booked. Please select an open date range.";
      return;
    }}

    if (nights < requiredMinStay) {{
      quoteGrid.style.display = "none";
      errorBox.style.display = "block";
      errorBox.innerText = `⚠️ Minimum stay starting on ${{formatDisplayDate(checkInDate)}} is ${{requiredMinStay}} nights (you selected ${{nights}} night${{nights > 1 ? 's' : ''}}).`;
      return;
    }}

    errorBox.style.display = "none";
    quoteGrid.style.display = "flex";

    const grandTotal = accommodationTotal + CLEANING_FEE;
    const datesLabel = `${{formatDisplayDate(checkInDate)}} to ${{formatDisplayDate(checkOutDate)}}`;
    document.getElementById("promptText").innerText = `Selected Stay: ${{datesLabel}} (${{nights}} nights)`;

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
        if (checkInDate && !checkOutDate && parseLocal(dateStr) > parseLocal(checkInDate)) {{
          cell.classList.add("checkout-eligible");
          cell.onclick = () => handleDayClick(dateStr);
        }}
        if (isCheckOut) {{
          cell.classList.add("selected-endpoint");
          contentHtml = `<div class="day-content"><span class="endpoint-badge">Check-Out</span></div>`;
        }}
      }} else {{
        cell.className = "day-cell avail";
        cell.onclick = () => handleDayClick(dateStr);

        let subLabel = `<span class="min-stay">${{info.min_stay}} Night Min</span>`;
        if (isCheckIn) {{
          cell.classList.add("selected-endpoint");
          subLabel = `<span class="endpoint-badge">Check-In</span>`;
        }} else if (isCheckOut) {{
          cell.classList.add("selected-endpoint");
          subLabel = `<span class="endpoint-badge">Check-Out</span>`;
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
# Main Execution Pipeline
# ---------------------------------------------------------------------------
def main():
    logger.info("=== Starting PriceLabs + Airbnb iCal Calendar Sync ===")

    # 1. Fetch live blocked/booked nights directly from Airbnb iCal
    airbnb_extractor = AirbnbICalExtractor(ical_url=AIRBNB_ICAL_URL)
    airbnb_blocked_dates = airbnb_extractor.fetch_blocked_dates()

    # 2. Fetch rates and min-stays from PriceLabs, overriding availability with Airbnb iCal
    extractor = PriceLabsExtractor(
        api_key=PRICELABS_API_KEY,
        listing_id=PRICELABS_LISTING_ID,
        pms=PRICELABS_PMS_NAME,
        airbnb_blocked=airbnb_blocked_dates
    )
    standard_records, discounted_records = extractor.fetch_pricing()

    std_builder = ICSCalendarBuilder(calendar_name="PriceLabs")
    std_ics = std_builder.build_ics(standard_records, uid_prefix="pricelabs-std")
    with open(OUTPUT_STANDARD_FILENAME, "w", encoding="utf-8") as f:
        f.write(std_ics)
    
    disc_builder = ICSCalendarBuilder(calendar_name="PriceLabs Discounted")
    disc_ics = disc_builder.build_ics(discounted_records, uid_prefix="pricelabs-disc")
    with open(OUTPUT_DISCOUNTED_FILENAME, "w", encoding="utf-8") as f:
        f.write(disc_ics)

    std_html_builder = HTMLCalendarBuilder(title="Mammoth Charm - Rates & Availability")
    with open(OUTPUT_STANDARD_HTML, "w", encoding="utf-8") as f:
        f.write(std_html_builder.build_html(standard_records))

    disc_html_builder = HTMLCalendarBuilder(title="Mammoth Charm - Direct Rates (25% Off)")
    with open(OUTPUT_DISCOUNTED_HTML, "w", encoding="utf-8") as f:
        f.write(disc_html_builder.build_html(discounted_records))

    logger.info("=== PriceLabs + Airbnb Calendar Sync Finished Successfully ===")


if __name__ == "__main__":
    main()
