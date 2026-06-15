#!/usr/bin/env python3
"""Download Shelly v2 power-consumption history in daily chunks and write CSV.

Reads `SHELLY_HOST`, `SHELLY_AUTH_KEY`, and `DEVICE_IDS` from environment or .env.

Usage example:
  python3 export_history.py --date-from 2025-01-01 --date-to 2025-01-07 --output out.csv
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timedelta
from typing import List, Dict, Any

import requests
from dotenv import load_dotenv


def load_config() -> Dict[str, str]:
    load_dotenv('.env', override=True)
    cfg = {
        'host': os.getenv('SHELLY_HOST', '').strip(),
        'auth_key': os.getenv('SHELLY_AUTH_KEY', '').strip(),
        'device_ids': os.getenv('DEVICE_IDS', '').strip(),
    }
    return cfg


def parse_date(s: str) -> datetime:
    # Accept YYYY-MM-DD or YYYY-MM-DD HH:MM:SS
    for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Invalid date format: {s}. Use YYYY-MM-DD or 'YYYY-MM-DD HH:MM:SS'.")


def daterange_days(start: datetime, end: datetime) -> List[Dict[str, datetime]]:
    ranges = []
    cur = datetime(start.year, start.month, start.day)
    last = end
    while cur <= last:
        day_start = cur
        day_end = cur + timedelta(days=1) - timedelta(seconds=1)
        if day_end > end:
            day_end = end
        ranges.append({'from': day_start, 'to': day_end})
        cur = cur + timedelta(days=1)
    return ranges


def fetch_chunk(host: str, auth_key: str, device: str, channel: int, date_from: str, date_to: str, retries=3, timeout=60) -> Dict[str, Any]:
    url = host.rstrip('/') + '/v2/statistics/power-consumption'
    params = {
        'id': device,
        'channel': str(channel),
        'date_range': 'custom',
        'date_from': date_from,
        'date_to': date_to,
        'auth_key': auth_key,
    }
    backoff = 1
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(url, params=params, timeout=timeout)
        except Exception as e:
            if attempt == retries:
                raise
            time.sleep(backoff)
            backoff *= 2
            continue

        if r.status_code == 200:
            try:
                return r.json()
            except Exception:
                if attempt == retries:
                    r.raise_for_status()
        else:
            # non-200
            if attempt == retries:
                raise RuntimeError(f'HTTP {r.status_code}: {r.text}')
        time.sleep(backoff)
        backoff *= 2

    raise RuntimeError('Failed to fetch after retries')


def normalize_entry(e: Dict[str, Any]) -> Dict[str, Any]:
    return {
        'datetime': e.get('datetime') or e.get('time') or '',
        'consumption': e.get('consumption', ''),
        'voltage': e.get('voltage', ''),
        'reversed': e.get('reversed', ''),
        'cost': e.get('cost', ''),
        'purpose': e.get('purpose', ''),
        'tariff_id': e.get('tariff_id', ''),
    }


def main(argv: List[str]):
    p = argparse.ArgumentParser(description='Download Shelly v2 power-consumption history to CSV')
    p.add_argument('--device', help='device id (overrides DEVICE_IDS)', default=None)
    p.add_argument('--channel', help='channel number', default=0, type=int)
    p.add_argument('--date-from', required=True, help="Start date (YYYY-MM-DD or 'YYYY-MM-DD HH:MM:SS')")
    p.add_argument('--date-to', required=True, help="End date (YYYY-MM-DD or 'YYYY-MM-DD HH:MM:SS')")
    p.add_argument('--output', '-o', default='data/septic_controls_history.csv', help='Output CSV path (default: data/septic_controls_history.csv)')
    p.add_argument('--sleep', type=float, default=1.05, help='Seconds to sleep between requests (rate limit)')
    args = p.parse_args(argv)

    cfg = load_config()
    host = cfg['host']
    auth = cfg['auth_key']
    if not host or not auth:
        print('Missing SHELLY_HOST or SHELLY_AUTH_KEY in environment or .env', file=sys.stderr)
        sys.exit(2)

    device = args.device
    if not device:
        ids = cfg['device_ids']
        if not ids:
            print('No device specified and DEVICE_IDS not set in environment/.env', file=sys.stderr)
            sys.exit(2)
        device = ids.split(',')[0].strip()

    try:
        start = parse_date(args.date_from)
        end = parse_date(args.date_to)
    except ValueError as e:
        print(e, file=sys.stderr)
        sys.exit(2)

    if end < start:
        print('date-to must be >= date-from', file=sys.stderr)
        sys.exit(2)

    ranges = daterange_days(start, end)

    all_rows: List[Dict[str, Any]] = []

    for r in ranges:
        df = r['from'].strftime('%Y-%m-%d %H:%M:%S')
        dt = r['to'].strftime('%Y-%m-%d %H:%M:%S')
        print(f'Fetching {df} -> {dt}')
        try:
            j = fetch_chunk(host, auth, device, args.channel, df, dt)
        except Exception as e:
            print(f'Failed to fetch chunk {df} - {dt}: {e}', file=sys.stderr)
            sys.exit(1)

        # The returned JSON may be {timezone, interval, history} or {data: {...}}
        data = None
        if isinstance(j, dict) and 'history' in j:
            data = j
        elif isinstance(j, dict) and 'data' in j and isinstance(j['data'], dict) and 'history' in j['data']:
            data = j['data']
        else:
            print('Unexpected JSON structure from server:', j, file=sys.stderr)
            sys.exit(1)

        history = data.get('history', []) or []
        for e in history:
            all_rows.append(normalize_entry(e))

        time.sleep(args.sleep)

    # Write CSV
    fieldnames = ['datetime', 'consumption', 'voltage', 'reversed', 'cost', 'purpose', 'tariff_id']
    out_path = args.output
    output_dir = os.path.dirname(out_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(out_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for row in all_rows:
            w.writerow(row)

    print(f'Wrote {len(all_rows)} rows to {out_path}')


if __name__ == '__main__':
    main(sys.argv[1:])
