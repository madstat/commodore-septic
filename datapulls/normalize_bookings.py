#!/usr/bin/env python3
"""Normalize Lodgify reservations JSON into a SQLite `bookings` table.

Usage example:
    python3 datapulls/normalize_bookings.py \
    --input data/lodgify_reservations.json \
    --db data/commodore_history.db
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple


def _to_int(v: Any) -> Any:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except Exception:
        return None


def _to_float(v: Any) -> Any:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except Exception:
        return None


def _to_bool_int(v: Any) -> int:
    return 1 if bool(v) else 0


def _parse_external_booking(item: Dict[str, Any]) -> Dict[str, Any]:
    raw = item.get("external_booking")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _extract_room_metrics(item: Dict[str, Any]) -> Dict[str, Any]:
    rooms = item.get("rooms")
    if not isinstance(rooms, list) or not rooms:
        return {
            "room_type_id": None,
            "adults": None,
            "children": None,
            "infants": None,
            "pets": None,
            "people": None,
        }

    first_room = rooms[0] if isinstance(rooms[0], dict) else {}
    breakdown = first_room.get("guest_breakdown") if isinstance(first_room.get("guest_breakdown"), dict) else {}

    return {
        "room_type_id": _to_int(first_room.get("room_type_id")),
        "adults": _to_int(breakdown.get("adults")),
        "children": _to_int(breakdown.get("children")),
        "infants": _to_int(breakdown.get("infants")),
        "pets": _to_int(breakdown.get("pets")),
        "people": _to_int(first_room.get("people")),
    }


def _flatten_booking(item: Dict[str, Any], pulled_at: str) -> Dict[str, Any]:
    guest = item.get("guest") if isinstance(item.get("guest"), dict) else {}
    external = _parse_external_booking(item)
    quote = item.get("quote") if isinstance(item.get("quote"), dict) else {}
    check_in = item.get("check_in") if isinstance(item.get("check_in"), dict) else {}
    check_out = item.get("check_out") if isinstance(item.get("check_out"), dict) else {}
    room = _extract_room_metrics(item)

    external_email = external.get("Guest Email") or external.get("guest_email")
    external_phone = external.get("Phone Numbers") or external.get("phone_numbers") or external.get("Phone Number")
    if isinstance(external_phone, list):
        external_phone = external_phone[0] if external_phone else None

    guest_email = guest.get("email") or external_email
    guest_phone = guest.get("phone") or external_phone

    return {
        "booking_id": _to_int(item.get("id")),
        "user_id": _to_int(item.get("user_id")),
        "property_id": _to_int(item.get("property_id")),
        "arrival_date": item.get("arrival"),
        "departure_date": item.get("departure"),
        "check_in_time": check_in.get("time"),
        "check_out_time": check_out.get("time"),
        "status": item.get("status"),
        "source": item.get("source"),
        "source_text": item.get("source_text"),
        "language": item.get("language"),
        "guest_name": guest.get("name"),
        "guest_email": guest_email,
        "guest_phone": guest_phone,
        "guest_country_code": guest.get("country_code"),
        "room_type_id": room["room_type_id"],
        "adults": room["adults"],
        "children": room["children"],
        "infants": room["infants"],
        "pets": room["pets"],
        "people": room["people"],
        "is_unavailable": _to_bool_int(item.get("is_unavailable")),
        "is_overbooked": _to_bool_int(item.get("is_overbooked")),
        "is_new": _to_bool_int(item.get("is_new")),
        "is_deleted": _to_bool_int(item.get("is_deleted")),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "canceled_at": item.get("canceled_at"),
        "currency_code": item.get("currency_code"),
        "total_amount": _to_float(item.get("total_amount")),
        "amount_paid": _to_float(item.get("amount_paid")),
        "amount_due": _to_float(item.get("amount_due")),
        "quote_id": _to_int(quote.get("id")),
        "quote_status": quote.get("status"),
        "notes": item.get("notes"),
        "thread_uid": item.get("thread_uid"),
        "raw_json": json.dumps(item, ensure_ascii=True),
        "pulled_at": pulled_at,
    }


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            booking_id INTEGER PRIMARY KEY,
            user_id INTEGER,
            property_id INTEGER,
            arrival_date TEXT,
            departure_date TEXT,
            check_in_time TEXT,
            check_out_time TEXT,
            status TEXT,
            source TEXT,
            source_text TEXT,
            language TEXT,
            guest_name TEXT,
            guest_email TEXT,
            guest_phone TEXT,
            guest_country_code TEXT,
            room_type_id INTEGER,
            adults INTEGER,
            children INTEGER,
            infants INTEGER,
            pets INTEGER,
            people INTEGER,
            is_unavailable INTEGER,
            is_overbooked INTEGER,
            is_new INTEGER,
            is_deleted INTEGER,
            created_at TEXT,
            updated_at TEXT,
            canceled_at TEXT,
            currency_code TEXT,
            total_amount REAL,
            amount_paid REAL,
            amount_due REAL,
            quote_id INTEGER,
            quote_status TEXT,
            notes TEXT,
            thread_uid TEXT,
            raw_json TEXT NOT NULL,
            pulled_at TEXT NOT NULL
        )
        """
    )

    # Keep existing databases forward-compatible with new columns.
    try:
        conn.execute("ALTER TABLE bookings ADD COLUMN check_in_time TEXT")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE bookings ADD COLUMN check_out_time TEXT")
    except sqlite3.OperationalError:
        pass


def upsert_bookings(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> Tuple[int, int]:
    existing_ids = set()
    for row in rows:
        bid = row.get("booking_id")
        if bid is None:
            continue
        cur = conn.execute("SELECT 1 FROM bookings WHERE booking_id = ?", (bid,))
        if cur.fetchone() is not None:
            existing_ids.add(bid)

    sql = """
    INSERT INTO bookings (
        booking_id, user_id, property_id, arrival_date, departure_date,
        check_in_time, check_out_time,
        status, source, source_text, language,
        guest_name, guest_email, guest_phone, guest_country_code,
        room_type_id, adults, children, infants, pets, people,
        is_unavailable, is_overbooked, is_new, is_deleted,
        created_at, updated_at, canceled_at,
        currency_code, total_amount, amount_paid, amount_due,
        quote_id, quote_status, notes, thread_uid,
        raw_json, pulled_at
    ) VALUES (
        :booking_id, :user_id, :property_id, :arrival_date, :departure_date,
        :check_in_time, :check_out_time,
        :status, :source, :source_text, :language,
        :guest_name, :guest_email, :guest_phone, :guest_country_code,
        :room_type_id, :adults, :children, :infants, :pets, :people,
        :is_unavailable, :is_overbooked, :is_new, :is_deleted,
        :created_at, :updated_at, :canceled_at,
        :currency_code, :total_amount, :amount_paid, :amount_due,
        :quote_id, :quote_status, :notes, :thread_uid,
        :raw_json, :pulled_at
    )
    ON CONFLICT(booking_id) DO UPDATE SET
        user_id = excluded.user_id,
        property_id = excluded.property_id,
        arrival_date = excluded.arrival_date,
        departure_date = excluded.departure_date,
        check_in_time = excluded.check_in_time,
        check_out_time = excluded.check_out_time,
        status = excluded.status,
        source = excluded.source,
        source_text = excluded.source_text,
        language = excluded.language,
        guest_name = excluded.guest_name,
        guest_email = excluded.guest_email,
        guest_phone = excluded.guest_phone,
        guest_country_code = excluded.guest_country_code,
        room_type_id = excluded.room_type_id,
        adults = excluded.adults,
        children = excluded.children,
        infants = excluded.infants,
        pets = excluded.pets,
        people = excluded.people,
        is_unavailable = excluded.is_unavailable,
        is_overbooked = excluded.is_overbooked,
        is_new = excluded.is_new,
        is_deleted = excluded.is_deleted,
        created_at = excluded.created_at,
        updated_at = excluded.updated_at,
        canceled_at = excluded.canceled_at,
        currency_code = excluded.currency_code,
        total_amount = excluded.total_amount,
        amount_paid = excluded.amount_paid,
        amount_due = excluded.amount_due,
        quote_id = excluded.quote_id,
        quote_status = excluded.quote_status,
        notes = excluded.notes,
        thread_uid = excluded.thread_uid,
        raw_json = excluded.raw_json,
        pulled_at = excluded.pulled_at
    """

    conn.executemany(sql, rows)

    insert_count = 0
    update_count = 0
    for row in rows:
        bid = row.get("booking_id")
        if bid is None:
            continue
        if bid in existing_ids:
            update_count += 1
        else:
            insert_count += 1

    return insert_count, update_count


def main(argv: List[str]) -> int:
    p = argparse.ArgumentParser(description="Normalize Lodgify reservations JSON into SQLite bookings table")
    p.add_argument("--input", required=True, help="Path to Lodgify reservations JSON export")
    p.add_argument("--db", default="data/commodore_history.db", help="SQLite DB path")
    args = p.parse_args(argv)

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Input file not found: {input_path}", file=sys.stderr)
        return 2

    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"Failed to parse JSON {input_path}: {exc}", file=sys.stderr)
        return 1

    reservations = payload.get("reservations") if isinstance(payload, dict) else None
    if not isinstance(reservations, list):
        print("Input JSON is missing 'reservations' list", file=sys.stderr)
        return 2

    pulled_at = datetime.now(timezone.utc).isoformat()
    rows = []
    skipped = 0
    for item in reservations:
        if not isinstance(item, dict):
            skipped += 1
            continue
        row = _flatten_booking(item, pulled_at)
        if row.get("booking_id") is None:
            skipped += 1
            continue
        rows.append(row)

    db_path = Path(args.db)
    if db_path.parent:
        db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        ensure_schema(conn)
        inserted, updated = upsert_bookings(conn, rows)
        conn.commit()
    finally:
        conn.close()

    print(
        f"bookings normalized: inserted={inserted}, updated={updated}, "
        f"skipped={skipped}, total_input={len(reservations)}, db={db_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
