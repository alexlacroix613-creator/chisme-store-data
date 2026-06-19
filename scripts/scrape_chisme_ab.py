#!/usr/bin/env python3
"""Generate FRESH Alberta store data for the Tequila Chisme locator.

Pulls the list of Alberta liquor stores carrying a given SKU directly from the
public LiquorConnect OData API (no auth, no key):

    GET https://appapi.liquorconnect.com/odata/Products({sku})/Suppliers
        Accept: application/json

SKU 143270 = Tequila Chisme Blanco. The endpoint returns ~50 Alberta suppliers
with full address, phone, coordinates, hours, etc. Fields come back SPACE-PADDED
and ALL-CAPS, so we .strip() everything and title-case names/addresses/cities.

Outputs two things:
  1. The locator JSON (byte-for-byte compatible with chisme-alberta-stores.json),
     written to the --output path.
  2. (in-code) `build_supabase_rows()` returns a list of dicts ready to UPSERT
     into a Supabase table. Another builder owns the actual Supabase write — this
     script never touches Supabase or any live site.

Stores with null coordinates are geocoded via free Nominatim (1 req/sec, polite
User-Agent). Any store that STILL has no coordinates is dropped (never emit
[0,0] pins).

Usage:
    python3 scrape_chisme_ab.py                               # Chisme Blanco (default)
    python3 scrape_chisme_ab.py --sku 785712 --name "Plata"   # any SKU
    python3 scrape_chisme_ab.py -o /path/to/public/stores.json
    python3 scrape_chisme_ab.py --no-geocode                  # skip Nominatim
"""

import argparse
import json
import logging
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_SKU = "143270"
DEFAULT_NAME = "Tequila Chisme Blanco"

ODATA_URL = "https://appapi.liquorconnect.com/odata/Products({sku})/Suppliers"
# Public-facing page humans visit (kept for parity with the existing JSON).
PRODUCT_PAGE = "https://www.liquorconnect.com/Products/Item/{sku}"
SOURCE_URL = "https://www.liquorconnect.com/Products/Stores/{sku}"
IMAGE_URL = "https://connectstorageprd.blob.core.windows.net/productimages/{sku}.jpg"

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = (
    "TequilaChisme-StoreLocator/1.0 (chisme.party; contact alex@siempretequila.com)"
)

HTTP_TIMEOUT = 150  # seconds (LiquorConnect OData endpoint can be slow, ~85s observed)
GEOCODE_TIMEOUT = 20  # seconds
GEOCODE_DELAY = 1.1  # seconds between Nominatim calls (their policy: max 1/sec)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("chisme-ab")


# --------------------------------------------------------------------------- #
# Text helpers                                                                 #
# --------------------------------------------------------------------------- #
def clean(value) -> str:
    """Strip space-padding; return '' for None."""
    return (value or "").strip()


def title_case_city(c: str) -> str:
    return " ".join(w.capitalize() for w in clean(c).split())


def smart_title(name: str) -> str:
    """Title-case a store/address string.

    Keeps a few connector words lowercase, and keeps directional/unit suffixes
    (SW, SE, NW, NE) uppercase to match the existing chisme-alberta-stores.json
    convention (e.g. "130-366 Aspen Glen Landing SW").
    """
    keep_lower = {"and", "de", "of", "the"}
    keep_upper = {"sw", "se", "nw", "ne", "nb", "sb", "eb", "wb"}
    out = []
    for word in clean(name).split():
        lw = word.lower()
        if lw in keep_lower:
            out.append(lw)
        elif lw in keep_upper:
            out.append(word.upper())
        else:
            out.append(word.title())
    return " ".join(out)


# --------------------------------------------------------------------------- #
# Fetch                                                                        #
# --------------------------------------------------------------------------- #
def fetch_suppliers(sku: str) -> list[dict]:
    """Fetch the raw supplier records from the OData endpoint."""
    url = ODATA_URL.format(sku=sku)
    req = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": USER_AGENT}
    )
    log.info("Fetching suppliers for SKU %s …", sku)
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    records = data.get("value", []) if isinstance(data, dict) else data
    log.info("Got %d raw supplier records.", len(records))
    return records


# --------------------------------------------------------------------------- #
# Geocoding                                                                    #
# --------------------------------------------------------------------------- #
def geocode(query: str) -> tuple[float, float] | None:
    """Geocode a free-text address via Nominatim. Returns (lat, lng) or None."""
    params = urllib.parse.urlencode(
        {"format": "json", "q": query, "limit": 1, "countrycodes": "ca"}
    )
    url = f"{NOMINATIM_URL}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=GEOCODE_TIMEOUT) as resp:
            results = json.loads(resp.read().decode("utf-8"))
        if results:
            return float(results[0]["lat"]), float(results[0]["lon"])
        log.warning("  Nominatim returned no match for: %s", query)
    except Exception as exc:  # noqa: BLE001 - never let one geocode kill the run
        log.warning("  Geocode failed for %r: %s", query, exc)
    return None


# --------------------------------------------------------------------------- #
# Mapping                                                                      #
# --------------------------------------------------------------------------- #
def coords_from_record(rec: dict) -> tuple[float, float] | None:
    """Pull (lat, lng) from a record, preferring GeoLocation, then Lat/Long."""
    geo = rec.get("GeoLocation")
    if isinstance(geo, dict):
        coords = geo.get("coordinates")
        if isinstance(coords, list) and len(coords) == 2:
            lng, lat = coords[0], coords[1]
            if lat is not None and lng is not None:
                return float(lat), float(lng)
    lat, lng = rec.get("Latitude"), rec.get("Longitude")
    if lat is not None and lng is not None:
        return float(lat), float(lng)
    return None


def map_stores(records: list[dict], do_geocode: bool = True) -> tuple[list[dict], dict]:
    """Filter to liquor stores, map to output schema, geocode null-coord stores.

    Returns (stores, stats) where stats has counts for reporting.
    """
    stats = {
        "raw": len(records),
        "dropped_non_liquor": 0,
        "valid_coords": 0,
        "geocoded": 0,
        "dropped_no_coords": 0,
    }
    stores = []

    for rec in records:
        if clean(rec.get("SupplierType")) != "Liquor Store":
            stats["dropped_non_liquor"] += 1
            log.info("Dropping non-liquor-store: %s (%s)",
                     clean(rec.get("Name")), clean(rec.get("SupplierType")))
            continue

        name = smart_title(rec.get("Name"))
        address = smart_title(rec.get("StreetAddress"))
        city = title_case_city(rec.get("City"))
        postal = clean(rec.get("PostalCode")).upper()
        phone = clean(rec.get("Phone"))
        supplier_id = rec.get("Id")

        coords = coords_from_record(rec)
        if coords is None and do_geocode:
            # Primary: full free-text address. Fallback: postal code + city only
            # (LiquorConnect truncates some addresses to a single word, but the
            # postal code is reliable and geocodes to a tight neighbourhood pin).
            queries = [f"{address}, {city}, AB {postal}, Canada"]
            if postal:
                queries.append(f"{postal}, {city}, AB, Canada")
            for query in queries:
                log.info("Geocoding (no coords from API): %s — %s", name, query)
                time.sleep(GEOCODE_DELAY)
                coords = geocode(query)
                if coords:
                    stats["geocoded"] += 1
                    log.info("  -> %.6f, %.6f", coords[0], coords[1])
                    break
        elif coords is not None:
            stats["valid_coords"] += 1

        if coords is None:
            stats["dropped_no_coords"] += 1
            log.warning("Dropping (no usable coordinates): %s", name)
            continue

        lat, lng = coords
        stores.append(
            {
                "name": name,
                "address": address,
                "city": city,
                "postal": postal,
                "phone": phone,
                "lat": lat,
                "lng": lng,
                "directions_url": f"https://www.google.com/maps/dir//{lat},{lng}",
                # Internal field used to build the Supabase upsert key; stripped
                # before writing the locator JSON.
                "_supplier_id": supplier_id,
            }
        )

    return stores, stats


# --------------------------------------------------------------------------- #
# Output builders                                                              #
# --------------------------------------------------------------------------- #
def build_payload(stores: list[dict], sku: str, name: str, run_ts: str) -> dict:
    """Build the locator JSON payload (matches chisme-alberta-stores.json)."""
    public_stores = [{k: v for k, v in s.items() if not k.startswith("_")} for s in stores]
    return {
        "product": {
            "name": name,
            "sku": sku,
            "size_ml": 750,
            "category": "Tequila",
            "image_url": IMAGE_URL.format(sku=sku),
            "product_page": PRODUCT_PAGE.format(sku=sku),
        },
        "province": "AB",
        "source": "liquorconnect.com",
        "source_url": SOURCE_URL.format(sku=sku),
        "last_updated": run_ts,
        "store_count": len(public_stores),
        "stores": public_stores,
    }


def build_supabase_rows(stores: list[dict], sku: str, run_ts: str) -> list[dict]:
    """Build a list of dicts ready to UPSERT into a Supabase table.

    The stable upsert key is the LiquorConnect supplier Id when present, else a
    'name|address' fallback. Another builder owns the actual Supabase write.
    """
    rows = []
    for s in stores:
        supplier_id = s.get("_supplier_id")
        upsert_key = (
            f"lc:{supplier_id}"
            if supplier_id is not None
            else f"na:{s['name']}|{s['address']}"
        )
        rows.append(
            {
                "upsert_key": upsert_key,
                "name": s["name"],
                "address": s["address"],
                "city": s["city"],
                "province": "AB",
                "postal": s["postal"],
                "phone": s["phone"],
                "lat": s["lat"],
                "lng": s["lng"],
                "directions_url": s["directions_url"],
                "sku": sku,
                "last_updated": run_ts,
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# Main                                                                         #
# --------------------------------------------------------------------------- #
def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sku", default=DEFAULT_SKU)
    p.add_argument("--name", default=DEFAULT_NAME)
    p.add_argument("-o", "--output", default="chisme-alberta-stores.json")
    p.add_argument(
        "--supabase-output",
        default=None,
        help="Optional path to also write the Supabase-ready rows as JSON.",
    )
    p.add_argument(
        "--no-geocode",
        action="store_true",
        help="Skip Nominatim geocoding (null-coord stores are dropped).",
    )
    args = p.parse_args()

    run_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        records = fetch_suppliers(args.sku)
    except Exception as exc:  # noqa: BLE001
        log.error("FATAL: could not fetch suppliers: %s", exc)
        return 1

    if not records:
        log.error("FATAL: endpoint returned zero records for SKU %s.", args.sku)
        return 1

    try:
        stores, stats = map_stores(records, do_geocode=not args.no_geocode)
    except Exception as exc:  # noqa: BLE001
        log.error("FATAL: mapping failed: %s", exc)
        return 1

    if not stores:
        log.error("FATAL: no stores survived filtering/geocoding.")
        return 1

    payload = build_payload(stores, args.sku, args.name, run_ts)
    supabase_rows = build_supabase_rows(stores, args.sku, run_ts)

    try:
        out = Path(args.output)
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001
        log.error("FATAL: could not write output %s: %s", args.output, exc)
        return 1

    if args.supabase_output:
        try:
            Path(args.supabase_output).write_text(
                json.dumps(supabase_rows, indent=2, ensure_ascii=False) + "\n"
            )
            log.info("Wrote %d Supabase rows → %s",
                     len(supabase_rows), args.supabase_output)
        except Exception as exc:  # noqa: BLE001
            log.error("Could not write Supabase output: %s", exc)
            # Non-fatal: locator JSON already written.

    # ----- report ---------------------------------------------------------- #
    log.info("=" * 60)
    log.info("DONE: %s (SKU %s)", args.name, args.sku)
    log.info("  raw records:        %d", stats["raw"])
    log.info("  dropped non-store:  %d", stats["dropped_non_liquor"])
    log.info("  valid API coords:   %d", stats["valid_coords"])
    log.info("  geocoded:           %d", stats["geocoded"])
    log.info("  dropped no-coords:  %d", stats["dropped_no_coords"])
    log.info("  FINAL store_count:  %d", payload["store_count"])
    log.info("  output:             %s", out)
    log.info("=" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(main())
