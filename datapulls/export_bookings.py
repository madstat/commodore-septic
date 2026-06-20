#!/usr/bin/env python3
"""Fetch Lodgify reservation data and write it to JSON.

Reads `LODGIFY_HOST`, `LODGIFY_AUTH_KEY`, and `LODGIFY_PROPERTY_ID`
from environment or `.env`.

Usage example:
    python3 datapulls/export_bookings.py --date-from 2026-01-01 --date-to 2026-01-31 --updated-since 2025-12-01 --output data/lodgify_reservations.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv


def load_config() -> Dict[str, str]:
    load_dotenv('.env', override=True)
    return {
        'host': os.getenv('LODGIFY_HOST', '').strip(),
        'auth_key': os.getenv('LODGIFY_AUTH_KEY', '').strip(),
        'property_id': os.getenv('LODGIFY_PROPERTY_ID', '').strip(),
    }


def parse_date(s: str) -> datetime:
    for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Invalid date format: {s}. Use YYYY-MM-DD or 'YYYY-MM-DD HH:MM:SS'.")


def build_headers(auth_key: str) -> Dict[str, str]:
    # Lodgify Public API v2 uses X-ApiKey auth header.
    return {
        'Accept': 'application/json',
        'X-ApiKey': auth_key,
    }


def extract_items(payload: Any) -> Tuple[List[Dict[str, Any]], bool, Optional[int]]:
    """Return (items, has_next, total_count_if_known) from varied API shapes."""
    if isinstance(payload, list):
        return payload, False, None

    if not isinstance(payload, dict):
        return [], False, None

    for key in ('items', 'results', 'data', 'reservations', 'bookings'):
        v = payload.get(key)
        if isinstance(v, list):
            items = v
            break
    else:
        items = []

    has_next = bool(payload.get('hasNext') or payload.get('has_next'))

    total_count: Optional[int] = None
    pagination = payload.get('pagination') if isinstance(payload.get('pagination'), dict) else None
    if pagination and isinstance(pagination.get('count'), int):
        total_count = pagination['count']
    elif isinstance(payload.get('count'), int):
        total_count = payload['count']

    return items, has_next, total_count


def normalized_lodgify_host(host: str) -> str:
    h = host.strip().rstrip('/')
    if not h.startswith('http://') and not h.startswith('https://'):
        h = 'https://' + h
    if not h.endswith('/v2'):
        h = h + '/v2'
    return h


def _parse_booking_date(v: Any) -> Optional[date]:
    if not v:
        return None
    s = str(v)
    try:
        # Handles date-only and datetime strings by taking first 10 chars.
        return datetime.strptime(s[:10], '%Y-%m-%d').date()
    except Exception:
        return None


def booking_in_range(item: Dict[str, Any], start_d: date, end_d: date) -> bool:
    arrival = _parse_booking_date(item.get('arrival'))
    departure = _parse_booking_date(item.get('departure'))
    if arrival is None and departure is None:
        return False
    if arrival is None:
        arrival = departure
    if departure is None:
        departure = arrival
    if arrival is None or departure is None:
        return False
    return departure >= start_d and arrival <= end_d


def fetch_page(
    host: str,
    auth_key: str,
    property_id: str,
    updated_since: str,
    page: int,
    page_size: int,
    timeout: int = 60,
    retries: int = 5,
    backoff_base_s: float = 1.0,
) -> Dict[str, Any]:
    # Official Lodgify v2 bookings list endpoint.
    endpoint = os.getenv('LODGIFY_RESERVATIONS_ENDPOINT', '/reservations/bookings')
    url = normalized_lodgify_host(host) + endpoint

    params = {
        'page': page,
        'size': page_size,
        'includeCount': 'true',
        # API supports "updatedSince" but not a to/until counterpart.
        'updatedSince': updated_since,
        'stayFilter': 'All',
        'includeExternal': 'true',
    }

    backoff_s = backoff_base_s
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, headers=build_headers(auth_key), params=params, timeout=timeout)
        except Exception as exc:
            if attempt == retries:
                raise RuntimeError(f'Network error after {retries} attempts: {exc}') from exc
            time.sleep(backoff_s)
            backoff_s *= 2
            continue

        if r.status_code == 429:
            # Respect server-provided retry delay when present.
            retry_after = r.headers.get('Retry-After')
            if retry_after:
                try:
                    wait_s = max(float(retry_after), backoff_s)
                except ValueError:
                    wait_s = backoff_s
            else:
                wait_s = backoff_s

            if attempt == retries:
                raise RuntimeError(f'Lodgify API rate limited (HTTP 429) after {retries} attempts: {r.text}')

            time.sleep(wait_s)
            backoff_s *= 2
            continue

        if 500 <= r.status_code < 600:
            if attempt == retries:
                raise RuntimeError(f'Lodgify API server error HTTP {r.status_code} after {retries} attempts: {r.text}')
            time.sleep(backoff_s)
            backoff_s *= 2
            continue

        if r.status_code >= 400:
            raise RuntimeError(f'Lodgify API error HTTP {r.status_code}: {r.text}')

        try:
            return r.json()
        except Exception as exc:
            if attempt == retries:
                raise RuntimeError(f'Failed to parse JSON response after {retries} attempts: {exc}') from exc
            time.sleep(backoff_s)
            backoff_s *= 2

    raise RuntimeError(f'Failed to fetch Lodgify page after {retries} attempts')


def fetch_reservations(
    host: str,
    auth_key: str,
    property_id: str,
    date_from: str,
    date_to: str,
    updated_since: str,
    page_size: int,
    sleep_s: float,
    max_rpm: int,
    retries: int,
    backoff_base_s: float,
) -> List[Dict[str, Any]]:
    all_items: List[Dict[str, Any]] = []
    all_filtered: List[Dict[str, Any]] = []
    page = 1
    min_interval = 60.0 / max_rpm
    last_request_time: Optional[float] = None
    total_count: Optional[int] = None
    start_d = datetime.strptime(date_from[:10], '%Y-%m-%d').date()
    end_d = datetime.strptime(date_to[:10], '%Y-%m-%d').date()

    while True:
        # Enforce a hard per-request pacing cap.
        if last_request_time is not None:
            elapsed = time.monotonic() - last_request_time
            wait_s = max(sleep_s, min_interval) - elapsed
            if wait_s > 0:
                time.sleep(wait_s)

        last_request_time = time.monotonic()
        payload = fetch_page(
            host=host,
            auth_key=auth_key,
            property_id=property_id,
            updated_since=updated_since,
            page=page,
            page_size=page_size,
            retries=retries,
            backoff_base_s=backoff_base_s,
        )
        items, has_next, count_from_payload = extract_items(payload)
        if total_count is None and count_from_payload is not None:
            total_count = count_from_payload

        if items:
            all_items.extend(items)
            for item in items:
                # Keep only rows for selected property and requested date window.
                if str(item.get('property_id', '')) != str(property_id):
                    continue
                if not booking_in_range(item, start_d, end_d):
                    continue
                all_filtered.append(item)

        if total_count is not None and len(all_items) >= total_count:
            break

        if not has_next and len(items) < page_size:
            break

        page += 1

    return all_filtered


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(description='Fetch Lodgify reservations to JSON')
    p.add_argument('--date-from', required=True, help="Start date (YYYY-MM-DD or 'YYYY-MM-DD HH:MM:SS')")
    p.add_argument('--date-to', required=True, help="End date (YYYY-MM-DD or 'YYYY-MM-DD HH:MM:SS')")
    p.add_argument(
        '--updated-since',
        default=None,
        help=(
            "Use this value for Lodgify API updatedSince pre-filter "
            "(YYYY-MM-DD or 'YYYY-MM-DD HH:MM:SS'). Defaults to --date-from."
        ),
    )
    p.add_argument('--output', '-o', default='data/lodgify_reservations.json', help='Output JSON path')
    p.add_argument('--page-size', type=int, default=100, help='Requested API page size')
    p.add_argument('--sleep', type=float, default=0.25, help='Sleep seconds between pages')
    p.add_argument('--max-rpm', type=int, default=700, help='Hard request-rate cap in requests/minute (Lodgify limit is 750)')
    p.add_argument('--retries', type=int, default=5, help='Retry attempts for transient/API rate-limit errors')
    p.add_argument('--backoff-base', type=float, default=1.0, help='Initial exponential backoff delay in seconds')
    args = p.parse_args(argv)

    cfg = load_config()
    host = cfg['host']
    auth_key = cfg['auth_key']
    property_id = cfg['property_id']

    if not host or not auth_key or not property_id:
        print('Missing one or more required env vars: LODGIFY_HOST, LODGIFY_AUTH_KEY, LODGIFY_PROPERTY_ID', file=sys.stderr)
        return 2

    if args.max_rpm <= 0:
        print('--max-rpm must be a positive integer', file=sys.stderr)
        return 2
    if args.retries <= 0:
        print('--retries must be a positive integer', file=sys.stderr)
        return 2
    if args.backoff_base <= 0:
        print('--backoff-base must be a positive number', file=sys.stderr)
        return 2

    try:
        start = parse_date(args.date_from)
        end = parse_date(args.date_to)
        updated_since_raw = args.updated_since or args.date_from
        updated_since_dt = parse_date(updated_since_raw)
    except ValueError as err:
        print(err, file=sys.stderr)
        return 2

    if end < start:
        print('date-to must be >= date-from', file=sys.stderr)
        return 2

    date_from = start.strftime('%Y-%m-%dT%H:%M:%S')
    date_to = end.strftime('%Y-%m-%dT%H:%M:%S')
    updated_since = updated_since_dt.strftime('%Y-%m-%dT%H:%M:%S')

    try:
        reservations = fetch_reservations(
            host=host,
            auth_key=auth_key,
            property_id=property_id,
            date_from=date_from,
            date_to=date_to,
            updated_since=updated_since,
            page_size=args.page_size,
            sleep_s=args.sleep,
            max_rpm=args.max_rpm,
            retries=args.retries,
            backoff_base_s=args.backoff_base,
        )
    except Exception as err:
        print(f'Failed to fetch reservations: {err}', file=sys.stderr)
        return 1

    output_dir = os.path.dirname(args.output)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    payload = {
        'source': 'lodgify',
        'property_id': property_id,
        'date_from': date_from,
        'date_to': date_to,
        'updated_since': updated_since,
        'count': len(reservations),
        'reservations': reservations,
    }

    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=True, indent=2)

    print(f'Wrote {len(reservations)} reservations to {args.output}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
