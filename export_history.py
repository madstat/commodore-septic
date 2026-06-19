#!/usr/bin/env python3
"""Download Shelly v2 power-consumption history in daily chunks and write to SQLite.

Reads `SHELLY_HOST`, `SHELLY_AUTH_KEY`, and `DEVICE_IDS` from environment or .env.

Usage example:
    python3 export_history.py --date-from 2025-01-01 --date-to 2025-01-07 --output out.db
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta
from typing import List, Dict, Any

import requests
import sqlite3
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


def _safe_float(v: Any) -> Any:
    if v is None or v == '':
        return None
    try:
        return float(v)
    except Exception:
        return None


def main(argv: List[str]):
    p = argparse.ArgumentParser(description='Download Shelly v2 power-consumption history to SQLite DB')
    p.add_argument('--device', help='device id (overrides DEVICE_IDS)', default=None)
    p.add_argument('--channel', help='channel number', default=0, type=int)
    p.add_argument('--date-from', required=True, help="Start date (YYYY-MM-DD or 'YYYY-MM-DD HH:MM:SS')")
    p.add_argument('--date-to', required=True, help="End date (YYYY-MM-DD or 'YYYY-MM-DD HH:MM:SS')")
    p.add_argument('--output', '-o', default='data/septic_controls_history.db', help='Output SQLite DB path (default: data/septic_controls_history.db)')
    p.add_argument('--sleep', type=float, default=1.05, help='Seconds to sleep between requests (rate limit)')
    args = p.parse_args(argv)

    cfg = load_config()
    host = cfg['host']
    auth = cfg['auth_key']
    if not host or not auth:
        print('Missing SHELLY_HOST or SHELLY_AUTH_KEY in environment or .env', file=sys.stderr)
        sys.exit(2)

    device_arg = args.device
    ids = []
    if device_arg:
        ids = [device_arg.strip()]
    else:
        env_ids = cfg['device_ids']
        if not env_ids:
            print('No device specified and DEVICE_IDS not set in environment/.env', file=sys.stderr)
            sys.exit(2)
        ids = [d.strip() for d in env_ids.split(',') if d.strip()]
        if not ids:
            print('No valid device IDs were found in DEVICE_IDS', file=sys.stderr)
            sys.exit(2)

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

    for device in ids:
        for r in ranges:
            df = r['from'].strftime('%Y-%m-%d %H:%M:%S')
            dt = r['to'].strftime('%Y-%m-%d %H:%M:%S')
            print(f'Fetching {device} {df} -> {dt}')
            try:
                j = fetch_chunk(host, auth, device, args.channel, df, dt)
            except Exception as e:
                print(f'Failed to fetch chunk {df} - {dt} for {device}: {e}', file=sys.stderr)
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
                row = normalize_entry(e)
                row['device_id'] = device
                all_rows.append(row)

            time.sleep(args.sleep)

    # Write to SQLite (auto-assigning integer primary key `id`)
    # If user provided a .csv filename, switch to .db with same base name.
    out_path = args.output
    if out_path.endswith('.csv'):
        out_path = out_path[:-4] + '.db'
    elif not out_path.endswith('.db'):
        out_path = out_path + '.db'

    output_dir = os.path.dirname(out_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)

    conn = sqlite3.connect(out_path)
    try:
        cur = conn.cursor()
        cur.execute(
            '''
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                name TEXT
            )
            '''
        )
        cur.execute(
            '''
            CREATE TABLE IF NOT EXISTS energy_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                datetime TEXT,
                consumption REAL,
                voltage REAL,
                reversed REAL,
                cost REAL,
                purpose TEXT,
                tariff_id TEXT,
                FOREIGN KEY(device_id) REFERENCES devices(device_id)
            )
            '''
        )

        insert_device_sql = 'INSERT OR IGNORE INTO devices (device_id) VALUES (?)'
        cur.executemany(insert_device_sql, [(device,) for device in ids])

        insert_sql = (
            'INSERT INTO energy_history (device_id, datetime, consumption, voltage, reversed, cost, purpose, tariff_id) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?)'
        )

        rows_to_insert = []
        for row in all_rows:
            rows_to_insert.append(
                (
                    row.get('device_id', ''),
                    row.get('datetime', ''),
                    _safe_float(row.get('consumption', '')),
                    _safe_float(row.get('voltage', '')),
                    _safe_float(row.get('reversed', '')),
                    _safe_float(row.get('cost', '')),
                    row.get('purpose', ''),
                    str(row.get('tariff_id', '')),
                )
            )

        if rows_to_insert:
            cur.executemany(insert_sql, rows_to_insert)
            conn.commit()

        print(f'Wrote {len(rows_to_insert)} rows to {out_path}')
    finally:
        conn.close()


if __name__ == '__main__':
    main(sys.argv[1:])
