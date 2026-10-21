#!/usr/bin/env python3
"""
PriceLabs Dynamic Pricing Calendar Automation
=============================================
Automates the daily extraction of PriceLabs dynamic pricing and minimum stay rules
for Mammoth Charm LLC, builds two RFC 5545 compliant iCalendar (.ics) feeds
AND two interactive HTML monthly calendar grids (Standard and 25% Discounted).

Author: Mammoth Charm LLC Operations
Version: 3.3.0 (Perpetual Dynamic US Holidays + Option 3 Top-Corner Holiday Tag)
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
# Perpetual US Federal Holiday Calculator (Zero external dependencies)
# ---------------------------------------------------------------------------
def calculate_us_holidays(year: int) -> Dict[str, str]:
    """Calculates all 11 US Federal Holidays dynamically using calendar rules.
    Runs perpetually for 2026, 2027, 2028, and any future year.
    """
    def nth_weekday(month: int, weekday: int, n: int) -> str:
        # weekday: 0 = Mon, 1 = Tue, ..., 6 = Sun
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
        f"{year}-01-01": "New Year's",
        nth_weekday(1, 0, 3): "MLK Day",          # 3rd Monday in Jan
        nth_weekday(2, 0, 3): "Presidents' Day",   # 3rd Monday in Feb
        last_weekday(5, 0): "Memorial Day",        # Last Monday in May
        f"{year}-06-19": "Juneteenth",
        f"{year}-07-04": "4th of July",
        nth_weekday(9, 0, 1): "Labor Day",         # 1st Monday in Sep
        nth_weekday(10, 0, 2): "Columbus Day",     # 2nd Monday in Oct
        f"{year}-11-11": "Veterans Day",
        nth_weekday(11, 3, 4): "Thanksgiving",     # 4th Thursday in Nov
        f"{year}-12-25": "Christmas",
    }


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
        logger.info(f"Filtering calendar window: {start_date} to {end_date} ({TIMEZONE_STR})")

        # Dynamically build holiday catalog across the active 3-year rolling window
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

            is_avail = self._determine_availability(r)
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

        logger.info(f"Retained {len(standard_records)} dates starting from {start_date}.")
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
            count += 1

        lines.append("END:VCALENDAR")
        logger.info(f"Generated {count} VEVENT entries for '{self.calendar_name}'.")
        return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# 3. Interactive HTML Calendar Grid Builder (Option 3: Corner Holiday Tag)
# ---------------------------------------------------------------------------
class HTMLCalendarBuilder:
    """Builds a standalone, responsive monthly HTML calendar grid."""

    def __init__(self, title: str):
        self.title = title

    def build_html(self, pricing_records: List[Dict[str, Any]]) -> str:
        records_json = json.dumps(pricing_records)
        updated_str = datetime.now().strftime("%b %d, %Y")

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
    --text-unavail: #be123c;
    --header-bg: #00c4b3;
    --header-text: #0f172a;
  }}
  * {{ box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  body {{ margin: 0; padding: 12px; background: #ffffff; color: #1e293b; }}
  .cal-container {{ max-width: 900px; margin: 0 auto; border: 1px solid #cbd5e1; border-radius: 12px; overflow: hidden; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }}
  
  /* Cyan Header with Dark Text */
  .cal-header {{ display: flex; justify-content: space-between; align-items: center; padding: 16px 20px; background: var(--header-bg); color: var(--header-text); }}
  .cal-title {{ font-size: 1.35rem; font-weight: 800; margin: 0; color: var(--header-text); letter-spacing: -0.01em; }}
  .nav-btns {{ display: flex; gap: 8px; align-items: center; }}
  .nav-btn {{ background: #0f172a; border: none; color: #ffffff; padding: 8px 14px; border-radius: 6px; cursor: pointer; font-weight: 600; font-size: 0.88rem; transition: background 0.15s, transform 0.1s; }}
  .nav-btn:hover {{ background: #1e293b; }}
  .nav-btn:active {{ transform: scale(0.98); }}

  /* Legend Bar with Holiday Sample Badge */
  .legend {{ display: flex; gap: 12px; padding: 12px 20px; background: #f8fafc; border-bottom: 1px solid #e2e8f0; align-items: center; justify-content: space-between; flex-wrap: wrap; }}
  .legend-items {{ display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }}
  .legend-pill {{ display: inline-flex; align-items: center; padding: 5px 12px; border-radius: 6px; font-size: 0.82rem; font-weight: 700; }}
  .pill-avail {{ background: var(--bg-avail); color: var(--text-avail); border: 1px solid var(--border-avail); }}
  .pill-unavail {{ background: var(--bg-unavail); color: var(--text-unavail); border: 1px solid var(--border-unavail); }}
  .pill-holiday {{ background: #fef3c7; color: #92400e; border: 1px solid #fde68a; font-weight: 700; padding: 5px 10px; border-radius: 6px; font-size: 0.80rem; }}
  .updated-label {{ color: #64748b; font-size: 0.78rem; font-weight: 500; }}

  .weekdays {{ display: grid; grid-template-columns: repeat(7, 1fr); background: #f1f5f9; border-bottom: 1px solid #e2e8f0; text-align: center; font-weight: 700; font-size: 0.8rem; padding: 9px 0; color: #334155; }}
  .days-grid {{ display: grid; grid-template-columns: repeat(7, 1fr); gap: 1px; background: #cbd5e1; }}
  .day-cell {{ min-height: 92px; background: #fff; padding: 6px; display: flex; flex-direction: column; justify-content: space-between; }}
  .day-cell.empty {{ background: #f8fafc; }}
  .day-cell.avail {{ background: var(--bg-avail); }}
  .day-cell.unavail {{ background: var(--bg-unavail); color: var(--text-unavail); }}
  
  /* Option 3: Top-Row Holiday Tag + Day Number */
  .day-top {{ display: flex; justify-content: space-between; align-items: flex-start; width: 100%; gap: 4px; }}
  .holiday-tag {{ font-size: 0.64rem; font-weight: 700; color: #92400e; background: #fef3c7; border: 1px solid #fde68a; padding: 1px 5px; border-radius: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 75%; line-height: 1.25; }}
  .day-cell.unavail .holiday-tag {{ background: #ffffff; border-color: #fda4af; color: #be123c; }}
  .day-num {{ font-size: 0.8rem; font-weight: 700; text-align: right; color: #475569; margin-left: auto; }}
  
  .day-content {{ text-align: center; margin: auto 0; }}
  .price {{ font-size: 1.05rem; font-weight: 800; color: var(--text-avail); display: block; }}
  .min-stay {{ font-size: 0.72rem; color: #334155; display: block; margin-top: 2px; font-weight: 600; }}
  .unavail-label {{ font-size: 0.8rem; font-weight: 700; color: var(--text-unavail); }}

  @media (max-width: 600px) {{
    .cal-header {{ flex-direction: column; gap: 10px; align-items: flex-start; }}
    .day-cell {{ min-height: 74px; padding: 3px; }}
    .holiday-tag {{ font-size: 0.54rem; padding: 0 3px; max-width: 68%; }}
    .price {{ font-size: 0.85rem; }}
    .min-stay {{ font-size: 0.58rem; }}
    .unavail-label {{ font-size: 0.65rem; }}
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
      <span class="legend-pill pill-unavail">Unavailable</span>
      <span class="pill-holiday">★ US Holiday</span>
    </div>
    <span class="updated-label">Updated: {updated_str}</span>
  </div>
  <div class="weekdays">
    <div>Sun</div><div>Mon</div><div>Tue</div><div>Wed</div><div>Thu</div><div>Fri</div><div>Sat</div>
  </div>
  <div class="days-grid" id="daysGrid"></div>
</div>

<script>
  const rawData = {records_json};
  const dataMap = {{}};
  rawData.forEach(item => {{ dataMap[item.date] = item; }});

  let current = new Date();
  current.setDate(1);

  function renderCalendar() {{
    const year = current.getFullYear();
    const month = current.getMonth();
    const monthNames = ["January","February","March","April","May","June","July","August","September","October","November","December"];
    document.getElementById("monthLabel").innerText = monthNames[month] + " " + year;

    const firstDay = new Date(year, month, 1).getDay();
    const daysInMonth = new Date(year, month + 1, 0).getDate();
    const grid = document.getElementById("daysGrid");
    grid.innerHTML = "";

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
      let holidayTagHtml = "";

      if (info && info.holiday) {{
        holidayTagHtml = `<span class="holiday-tag" title="${{info.holiday}}">★ ${{info.holiday}}</span>`;
      }}

      if (!info) {{
        cell.className = "day-cell empty";
      }} else if (!info.available) {{
        cell.className = "day-cell unavail";
        contentHtml = `<div class="day-content"><span class="unavail-label">Unavailable</span></div>`;
      }} else {{
        cell.className = "day-cell avail";
        contentHtml = `<div class="day-content"><span class="price">$${{info.price}}</span><span class="min-stay">${{info.min_stay}} Night Min</span></div>`;
      }}

      cell.innerHTML = `<div class="day-top">${{holidayTagHtml}}<span class="day-num">${{d}}</span></div>${{contentHtml}}`;
      grid.appendChild(cell);
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
    logger.info("=== Starting PriceLabs Calendar Sync ===")

    extractor = PriceLabsExtractor(
        api_key=PRICELABS_API_KEY,
        listing_id=PRICELABS_LISTING_ID,
        pms=PRICELABS_PMS_NAME
    )
    standard_records, discounted_records = extractor.fetch_pricing()

    # 1. Standard Calendar Feed (.ics)
    std_builder = ICSCalendarBuilder(calendar_name="PriceLabs")
    std_ics = std_builder.build_ics(standard_records, uid_prefix="pricelabs-std")
    with open(OUTPUT_STANDARD_FILENAME, "w", encoding="utf-8") as f:
        f.write(std_ics)
    logger.info(f"Standard calendar written to: {OUTPUT_STANDARD_FILENAME}")

    # 2. Discounted Calendar Feed (.ics)
    disc_builder = ICSCalendarBuilder(calendar_name="PriceLabs Discounted")
    disc_ics = disc_builder.build_ics(discounted_records, uid_prefix="pricelabs-disc")
    with open(OUTPUT_DISCOUNTED_FILENAME, "w", encoding="utf-8") as f:
        f.write(disc_ics)
    logger.info(f"Discounted calendar written to: {OUTPUT_DISCOUNTED_FILENAME}")

    # 3. Standard Interactive HTML Calendar
    std_html_builder = HTMLCalendarBuilder(title="Mammoth Charm - Rates & Availability")
    with open(OUTPUT_STANDARD_HTML, "w", encoding="utf-8") as f:
        f.write(std_html_builder.build_html(standard_records))
    logger.info(f"Standard HTML calendar written to: {OUTPUT_STANDARD_HTML}")

    # 4. Discounted Interactive HTML Calendar
    disc_html_builder = HTMLCalendarBuilder(title="Mammoth Charm - Direct Rates (25% Off)")
    with open(OUTPUT_DISCOUNTED_HTML, "w", encoding="utf-8") as f:
        f.write(disc_html_builder.build_html(discounted_records))
    logger.info(f"Discounted HTML calendar written to: {OUTPUT_DISCOUNTED_HTML}")

    logger.info("=== PriceLabs Calendar Sync Finished Successfully ===")


if __name__ == "__main__":
    main()
