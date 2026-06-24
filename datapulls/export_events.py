#!/usr/bin/env python3
"""Fetch Shelly Cloud activity events and write them to SQLite.

Reads `SHELLY_HOST`, `SHELLY_AUTH_KEY`, and optional `SHELLY_EVENT_TAGS`
from environment or `.env`.

Tag specs use `[label=]tag` format and can be supplied either by repeating
`--tag` or through `SHELLY_EVENT_TAGS` as a comma-separated list.

Usage example:
    python3 datapulls/export_events.py \
        --tag grinder=1780293407369 \
        --tag septic=d48afc79f588 \
        --date-from 2026-06-19 \
        --date-to 2026-06-21 \
        --output data/commodore_history.db
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List

import requests
from dotenv import load_dotenv


def load_config() -> Dict[str, str]:
    load_dotenv('.env', override=True)
    return {
        'host': os.getenv('SHELLY_HOST', '').strip(),
        'auth_key': os.getenv('SHELLY_AUTH_KEY', '').strip(),
        'event_tags': os.getenv('SHELLY_EVENT_TAGS', '').strip(),
        'event_extra_params': os.getenv('SHELLY_EVENT_LOG_EXTRA_PARAMS', '').strip(),
        'event_limit': os.getenv('SHELLY_EVENT_LIMIT', '').strip(),
    }


def parse_date(s: str) -> datetime:
    for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Invalid date format: {s}. Use YYYY-MM-DD or 'YYYY-MM-DD HH:MM:SS'.")

def parse_tag_specs(tag_args: List[str], env_value: str) -> List[Dict[str, str]]:
    raw_specs = list(tag_args)
    if not raw_specs and env_value:
        raw_specs = [part.strip() for part in env_value.split(',') if part.strip()]

    parsed: List[Dict[str, str]] = []
    for spec in raw_specs:
        label = ''
        tag = spec.strip()
        if '=' in tag:
            label_part, tag_part = tag.split('=', 1)
            label = label_part.strip()
            tag = tag_part.strip()
        if not tag:
            continue
        parsed.append(
            {
                'label': label or tag,
                'tag': tag,
                'has_explicit_label': '1' if label else '0',
            }
        )
    return parsed


def parse_limit(cli_value: int | None, env_value: str) -> int:
    if cli_value is not None:
        limit = cli_value
    elif env_value:
        try:
            limit = int(env_value)
        except ValueError as exc:
            raise ValueError('SHELLY_EVENT_LIMIT must be an integer') from exc
    else:
        limit = 100

    if limit <= 0:
        raise ValueError('Event limit must be > 0')
    return limit


def parse_extra_params(raw_value: str) -> Dict[str, Any]:
    if not raw_value:
        return {}
    try:
        parsed = json.loads(raw_value)
    except json.JSONDecodeError as exc:
        raise ValueError(f'Invalid JSON for event extra params: {exc}') from exc
    if not isinstance(parsed, dict):
        raise ValueError('SHELLY_EVENT_LOG_EXTRA_PARAMS must decode to a JSON object')
    return parsed


def fetch_chunk(
    host: str,
    auth_key: str,
    tags: List[str],
    limit: int,
    extra_params: Dict[str, Any],
    retries: int = 3,
    timeout: int = 60,
) -> Any:
    url = host.rstrip('/') + '/statistics/event-log'
    payload: Dict[str, Any] = {
        'tags': tags,
        'limit': limit,
        'auth_key': auth_key,
    }
    payload.update(extra_params)

    backoff = 1.0
    for attempt in range(1, retries + 1):
        try:
            response = requests.post(url, json=payload, timeout=timeout)
        except Exception:
            if attempt == retries:
                raise
            time.sleep(backoff)
            backoff *= 2
            continue

        if response.status_code == 200:
            data = response.json()
            if isinstance(data, dict) and data.get('isok') is False:
                raise RuntimeError(json.dumps(data))
            return data

        if attempt == retries:
            raise RuntimeError(f'HTTP {response.status_code}: {response.text}')

        time.sleep(backoff)
        backoff *= 2

    raise RuntimeError('Failed to fetch event log after retries')


def extract_tagged_items(payload: Any) -> Dict[str, List[Dict[str, Any]]]:
    if not isinstance(payload, dict):
        return {}

    result = payload.get('result')
    if not isinstance(result, dict):
        return {}

    extracted: Dict[str, List[Dict[str, Any]]] = {}
    for tag, value in result.items():
        if isinstance(value, list):
            extracted[str(tag)] = [item for item in value if isinstance(item, dict)]
    return extracted


def _first_value(entry: Dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = entry.get(key)
        if value is None or value == '':
            continue
        if isinstance(value, (dict, list)):
            return json.dumps(value, sort_keys=True, default=str)
        return str(value)
    return ''


def _parse_embedded_payload(raw_payload: Any) -> Any:
    if raw_payload is None or raw_payload == '':
        return None
    if isinstance(raw_payload, (list, dict)):
        return raw_payload
    if isinstance(raw_payload, str):
        try:
            return json.loads(raw_payload)
        except json.JSONDecodeError:
            return raw_payload
    return raw_payload


def _event_datetime_text(timestamp_ms: int | None) -> str:
    if timestamp_ms is None:
        return ''
    return datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat(sep=' ')


def _parse_channel(payload_value: Any) -> str:
    if isinstance(payload_value, list) and len(payload_value) > 2:
        value = payload_value[1]
        if isinstance(value, bool):
            return ''
        return str(value)
    return ''


def _parse_action(payload_value: Any) -> str:
    if isinstance(payload_value, list) and len(payload_value) > 2:
        value = payload_value[2]
        if isinstance(value, bool):
            return 'on' if value else 'off'
        return str(value)
    return ''


def _parse_device_id(entry: Dict[str, Any], payload_value: Any) -> str:
    tag = _first_value(entry, 'tag')
    if tag:
        return tag
    if isinstance(payload_value, list) and payload_value:
        return str(payload_value[0])
    return ''


def normalize_entry(entry: Dict[str, Any], tag_label: str, tag: str) -> Dict[str, str]:
    payload_value = _parse_embedded_payload(entry.get('p'))
    timestamp_ms: int | None = None
    raw_timestamp = entry.get('t')
    if raw_timestamp is not None and raw_timestamp != '':
        try:
            timestamp_ms = int(raw_timestamp)
        except (TypeError, ValueError):
            timestamp_ms = None

    raw_json = json.dumps(entry, sort_keys=True, separators=(',', ':'), default=str)
    return {
        'tag': tag,
        'tag_label': tag_label,
        'event_id': _first_value(entry, 'rowid', 'event_id', 'eventId', 'id', 'uuid', '_id'),
        'event_timestamp_ms': '' if timestamp_ms is None else str(timestamp_ms),
        'event_datetime': _event_datetime_text(timestamp_ms),
        'device_id': _parse_device_id(entry, payload_value),
        'component': _first_value(entry, 'tag'),
        'channel': _parse_channel(payload_value),
        'event_type': _first_value(entry, 'e', 'event_type', 'eventType', 'type', 'event', 'activity_type', 'kind'),
        'action': _parse_action(payload_value),
        'message': '',
        'value_text': json.dumps(payload_value, sort_keys=True, default=str) if payload_value is not None else '',
        'raw_json': raw_json,
        'payload_hash': hashlib.sha256(raw_json.encode('utf-8')).hexdigest(),
    }


def is_in_range(entry: Dict[str, Any], start: datetime, end: datetime) -> bool:
    raw_timestamp = entry.get('t')
    if raw_timestamp is None or raw_timestamp == '':
        return False
    try:
        event_dt = datetime.fromtimestamp(int(raw_timestamp) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return False
    return start <= event_dt <= end


def upsert_event_tags(
    cur: sqlite3.Cursor,
    tag_specs: List[Dict[str, str]],
    tagged_items: Dict[str, List[Dict[str, Any]]],
) -> None:
    tag_specs_by_tag = {spec['tag']: spec for spec in tag_specs}

    cur.execute(
        '''
        CREATE TABLE IF NOT EXISTS event_tags (
            tag TEXT PRIMARY KEY,
            display_name TEXT,
            room_name TEXT,
            notes TEXT,
            first_seen_at TEXT,
            last_seen_at TEXT,
            sample_event_type TEXT,
            sample_payload TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        '''
    )

    insert_sql = (
        'INSERT INTO event_tags ('
        'tag, display_name, first_seen_at, last_seen_at, sample_event_type, sample_payload, updated_at'
        ') VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP) '
        'ON CONFLICT(tag) DO UPDATE SET '
        'display_name = CASE '
        '  WHEN event_tags.display_name IS NULL OR event_tags.display_name = event_tags.tag THEN excluded.display_name '
        '  ELSE event_tags.display_name '
        'END, '
        'first_seen_at = CASE '
        '  WHEN event_tags.first_seen_at IS NULL THEN excluded.first_seen_at '
        '  WHEN excluded.first_seen_at IS NULL THEN event_tags.first_seen_at '
        '  ELSE MIN(event_tags.first_seen_at, excluded.first_seen_at) '
        'END, '
        'last_seen_at = CASE '
        '  WHEN event_tags.last_seen_at IS NULL THEN excluded.last_seen_at '
        '  WHEN excluded.last_seen_at IS NULL THEN event_tags.last_seen_at '
        '  ELSE MAX(event_tags.last_seen_at, excluded.last_seen_at) '
        'END, '
        'sample_event_type = COALESCE(event_tags.sample_event_type, excluded.sample_event_type), '
        'sample_payload = COALESCE(event_tags.sample_payload, excluded.sample_payload), '
        'updated_at = CURRENT_TIMESTAMP'
    )

    rows = []
    for spec in tag_specs:
        tag = spec['tag']
        items = tagged_items.get(tag, [])
        timestamps = []
        for item in items:
            raw_timestamp = item.get('t')
            if raw_timestamp is None or raw_timestamp == '':
                continue
            try:
                timestamps.append(int(raw_timestamp))
            except (TypeError, ValueError):
                continue

        first_seen_at = ''
        last_seen_at = ''
        if timestamps:
            first_seen_at = _event_datetime_text(min(timestamps))
            last_seen_at = _event_datetime_text(max(timestamps))

        sample_item = items[0] if items else {}
        sample_event_type = _first_value(sample_item, 'e', 'event_type', 'eventType', 'type', 'event', 'activity_type', 'kind')
        sample_payload = _first_value(sample_item, 'p')
        display_name = spec['label'] if spec.get('has_explicit_label') == '1' else None

        rows.append(
            (
                tag,
                display_name,
                first_seen_at or None,
                last_seen_at or None,
                sample_event_type or None,
                sample_payload or None,
            )
        )

    if rows:
        cur.executemany(insert_sql, rows)


def main(argv: List[str]) -> None:
    parser = argparse.ArgumentParser(description='Download Shelly Cloud activity log events to SQLite DB')
    parser.add_argument('--tag', action='append', default=[], help='Tag spec in [label=]tag format; can be repeated')
    parser.add_argument('--date-from', required=True, help="Start date (YYYY-MM-DD or 'YYYY-MM-DD HH:MM:SS')")
    parser.add_argument('--date-to', required=True, help="End date (YYYY-MM-DD or 'YYYY-MM-DD HH:MM:SS')")
    parser.add_argument('--output', '-o', default='data/commodore_history.db', help='Output SQLite DB path (default: data/commodore_history.db)')
    parser.add_argument('--limit', type=int, default=None, help='Max events per tag to request from Shelly (default: 100 or SHELLY_EVENT_LIMIT)')
    parser.add_argument('--sleep', type=float, default=1.05, help='Seconds to sleep between requests (rate limit)')
    args = parser.parse_args(argv)

    cfg = load_config()
    host = cfg['host']
    auth = cfg['auth_key']
    if not host or not auth:
        print('Missing SHELLY_HOST or SHELLY_AUTH_KEY in environment or .env', file=sys.stderr)
        sys.exit(2)

    try:
        tag_specs = parse_tag_specs(args.tag, cfg['event_tags'])
        extra_params = parse_extra_params(cfg['event_extra_params'])
        limit = parse_limit(args.limit, cfg['event_limit'])
    except ValueError as exc:
        print(exc, file=sys.stderr)
        sys.exit(2)

    if not tag_specs:
        print('No event tags configured. Use --tag or SHELLY_EVENT_TAGS.', file=sys.stderr)
        sys.exit(2)

    try:
        start = parse_date(args.date_from).replace(tzinfo=timezone.utc)
        end = parse_date(args.date_to).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        sys.exit(2)

    if end < start:
        print('date-to must be >= date-from', file=sys.stderr)
        sys.exit(2)

    all_rows: List[Dict[str, str]] = []
    tags = [spec['tag'] for spec in tag_specs]

    print(f'Fetching {len(tags)} tags with limit={limit}')
    try:
        payload = fetch_chunk(
            host=host,
            auth_key=auth,
            tags=tags,
            limit=limit,
            extra_params=extra_params,
        )
    except Exception as exc:
        print(f'Failed to fetch event log: {exc}', file=sys.stderr)
        sys.exit(1)

    tagged_items = extract_tagged_items(payload)
    for tag in tags:
        items = tagged_items.get(tag, [])
        filtered_items = [item for item in items if is_in_range(item, start, end)]
        for item in filtered_items:
            all_rows.append(normalize_entry(item, tag, tag))
        if len(items) == limit and items:
            oldest = min((item.get('t') for item in items if item.get('t') is not None), default=None)
            if oldest is not None:
                try:
                    oldest_dt = datetime.fromtimestamp(int(oldest) / 1000, tz=timezone.utc)
                except (TypeError, ValueError, OSError):
                    oldest_dt = None
                if oldest_dt is not None and oldest_dt > start:
                    print(
                        f'Warning: tag {tag} returned the limit ({limit}) and the oldest fetched event is {oldest_dt.isoformat()}; older in-range events may be missing.',
                        file=sys.stderr,
                    )

    time.sleep(args.sleep)

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
        upsert_event_tags(cur, tag_specs, tagged_items)
        cur.execute(
            '''
            CREATE TABLE IF NOT EXISTS event_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag TEXT NOT NULL,
                tag_label TEXT,
                event_id TEXT,
                event_timestamp_ms INTEGER,
                event_datetime TEXT,
                device_id TEXT,
                component TEXT,
                channel TEXT,
                event_type TEXT,
                action TEXT,
                message TEXT,
                value_text TEXT,
                raw_json TEXT NOT NULL,
                payload_hash TEXT NOT NULL
            )
            '''
        )
        cur.execute(
            '''
            CREATE UNIQUE INDEX IF NOT EXISTS ux_event_log_payload_hash
            ON event_log (payload_hash)
            '''
        )

        insert_sql = (
            'INSERT INTO event_log ('
            'tag, tag_label, event_id, event_timestamp_ms, event_datetime, device_id, component, channel, event_type, action, message, value_text, raw_json, payload_hash'
            ') VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(payload_hash) DO UPDATE SET '
            'tag = excluded.tag, '
            'tag_label = excluded.tag_label, '
            'event_id = excluded.event_id, '
            'event_timestamp_ms = excluded.event_timestamp_ms, '
            'event_datetime = excluded.event_datetime, '
            'device_id = excluded.device_id, '
            'component = excluded.component, '
            'channel = excluded.channel, '
            'event_type = excluded.event_type, '
            'action = excluded.action, '
            'message = excluded.message, '
            'value_text = excluded.value_text, '
            'raw_json = excluded.raw_json'
        )

        rows_to_insert = [
            (
                row['tag'],
                row['tag_label'],
                row['event_id'],
                None if not row['event_timestamp_ms'] else int(row['event_timestamp_ms']),
                row['event_datetime'],
                row['device_id'],
                row['component'],
                row['channel'],
                row['event_type'],
                row['action'],
                row['message'],
                row['value_text'],
                row['raw_json'],
                row['payload_hash'],
            )
            for row in all_rows
        ]

        if rows_to_insert:
            cur.executemany(insert_sql, rows_to_insert)

        conn.commit()

        print(f'Upserted {len(rows_to_insert)} rows to {out_path}')
    finally:
        conn.close()


if __name__ == '__main__':
    main(sys.argv[1:])