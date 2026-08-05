#!/usr/bin/env python3
"""Estimate sump pump calibration metrics from hourly energy history.

This script segments septic pump activity into windows separated by large gaps,
then assumes gallons pumped out by the septic pump within each window are roughly
equal to gallons pumped in by the sump pump during the same window.

Default assumptions:
- Septic pump device consumes ~20 Wh per cycle.
- Each septic pump cycle ejects ~20 gallons.
- Sump pump meter is typically one-leg-only and reports ~5.5 Wh per cycle.
- A septic non-zero gap > 4 hours indicates the septic timer/float is off.

If your DB was populated with the older generic endpoint values, pass:
- --septic-wh-per-cycle 0.33
- --sump-wh-per-cycle 0.09
"""

from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Sequence


def parse_dt(value: str) -> datetime:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        f"Invalid datetime: {value}. Use 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM:SS'."
    )


@dataclass
class WindowResult:
    index: int
    start_ts: str
    end_ts: str
    septic_wh: float
    septic_cycles_est: float
    gallons_out_est: float
    sump_wh: float
    sump_cycles_est: float
    gal_per_sump_cycle: Optional[float]
    gal_per_sump_wh: Optional[float]


def find_windows(septic_rows: Sequence[tuple[str, float]], gap_hours: float) -> List[tuple[str, str, float]]:
    if not septic_rows:
        return []

    windows: List[tuple[str, str, float]] = []
    start_ts = septic_rows[0][0]
    prev_ts = septic_rows[0][0]
    septic_wh = float(septic_rows[0][1] or 0.0)

    for ts, wh in septic_rows[1:]:
        gap = (datetime.fromisoformat(ts) - datetime.fromisoformat(prev_ts)).total_seconds() / 3600.0
        if gap > gap_hours:
            windows.append((start_ts, prev_ts, septic_wh))
            start_ts = ts
            septic_wh = 0.0

        septic_wh += float(wh or 0.0)
        prev_ts = ts

    windows.append((start_ts, prev_ts, septic_wh))
    return windows


def main() -> int:
    parser = argparse.ArgumentParser(description="Calculate sump gallons-per-cycle and gallons-per-Wh calibration.")
    parser.add_argument("--db", default="data/commodore_history.db", help="Path to SQLite DB.")
    parser.add_argument("--start", required=True, type=parse_dt, help="Start datetime.")
    parser.add_argument("--end", required=True, type=parse_dt, help="End datetime.")
    parser.add_argument("--septic-device", default="e4b063d4444c", help="Septic pump device_id.")
    parser.add_argument("--sump-device", default="e4b063d424a0", help="Sump pump device_id.")
    parser.add_argument("--gap-hours", type=float, default=4.0, help="Gap threshold for separating windows.")
    parser.add_argument("--septic-wh-per-cycle", type=float, default=20.0, help="Septic Wh used per cycle.")
    parser.add_argument("--septic-gallons-per-cycle", type=float, default=20.0, help="Gallons ejected per septic cycle.")
    parser.add_argument("--sump-wh-per-cycle", type=float, default=5.5, help="Metered Wh per sump cycle.")
    parser.add_argument(
        "--min-sump-wh",
        type=float,
        default=0.0,
        help="Only include windows with sump_wh >= this value in overall averages.",
    )

    args = parser.parse_args()

    if args.end < args.start:
        parser.error("--end must be >= --start")

    start_str = args.start.strftime("%Y-%m-%d %H:%M:%S")
    end_str = args.end.strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(args.db)
    cur = conn.cursor()

    septic_rows = cur.execute(
        """
        SELECT datetime, consumption
        FROM energy_history
        WHERE device_id = ?
          AND datetime >= ?
          AND datetime <= ?
          AND COALESCE(consumption, 0) > 0
        ORDER BY datetime
        """,
        (args.septic_device, start_str, end_str),
    ).fetchall()

    if not septic_rows:
        print("No non-zero septic rows found in the selected range.")
        conn.close()
        return 0

    windows = find_windows(septic_rows, args.gap_hours)

    results: List[WindowResult] = []
    for idx, (wstart, wend, septic_wh) in enumerate(windows, start=1):
        sump_wh = cur.execute(
            """
            SELECT COALESCE(SUM(consumption), 0)
            FROM energy_history
            WHERE device_id = ?
              AND datetime >= ?
              AND datetime <= ?
            """,
            (args.sump_device, wstart, wend),
        ).fetchone()[0]
        sump_wh = float(sump_wh or 0.0)

        septic_cycles = septic_wh / args.septic_wh_per_cycle if args.septic_wh_per_cycle else 0.0
        gallons_out = septic_cycles * args.septic_gallons_per_cycle
        sump_cycles = sump_wh / args.sump_wh_per_cycle if args.sump_wh_per_cycle else 0.0

        gal_per_cycle = gallons_out / sump_cycles if sump_cycles > 0 else None
        gal_per_wh = gallons_out / sump_wh if sump_wh > 0 else None

        results.append(
            WindowResult(
                index=idx,
                start_ts=wstart,
                end_ts=wend,
                septic_wh=septic_wh,
                septic_cycles_est=septic_cycles,
                gallons_out_est=gallons_out,
                sump_wh=sump_wh,
                sump_cycles_est=sump_cycles,
                gal_per_sump_cycle=gal_per_cycle,
                gal_per_sump_wh=gal_per_wh,
            )
        )

    filtered = [r for r in results if r.sump_wh >= args.min_sump_wh and r.sump_wh > 0]

    total_gallons = sum(r.gallons_out_est for r in filtered)
    total_sump_wh = sum(r.sump_wh for r in filtered)
    total_sump_cycles = sum(r.sump_cycles_est for r in filtered)

    avg_gal_per_cycle = total_gallons / total_sump_cycles if total_sump_cycles > 0 else None
    avg_gal_per_wh = total_gallons / total_sump_wh if total_sump_wh > 0 else None

    print("Calibration Inputs")
    print(f"  db: {args.db}")
    print(f"  start: {start_str}")
    print(f"  end:   {end_str}")
    print(f"  septic_device: {args.septic_device}")
    print(f"  sump_device:   {args.sump_device}")
    print(f"  gap_hours: {args.gap_hours}")
    print(f"  septic_wh_per_cycle: {args.septic_wh_per_cycle}")
    print(f"  septic_gallons_per_cycle: {args.septic_gallons_per_cycle}")
    print(f"  sump_wh_per_cycle: {args.sump_wh_per_cycle}")
    print(f"  min_sump_wh filter: {args.min_sump_wh}")

    print("\nOverall")
    print(f"  total_windows: {len(results)}")
    print(f"  included_windows: {len(filtered)}")
    print(f"  estimated_gallons_out: {total_gallons:.3f}")
    print(f"  metered_sump_wh: {total_sump_wh:.3f}")
    print(f"  estimated_sump_cycles: {total_sump_cycles:.3f}")
    print(
        "  avg_gallons_per_sump_cycle: "
        + (f"{avg_gal_per_cycle:.3f}" if avg_gal_per_cycle is not None else "NA")
    )
    print(
        "  avg_gallons_per_metered_sump_wh: "
        + (f"{avg_gal_per_wh:.3f}" if avg_gal_per_wh is not None else "NA")
    )

    print("\nPer Window")
    print(
        "index,start,end,septic_wh,septic_cycles_est,gallons_out_est,"
        "sump_wh,sump_cycles_est,gal_per_sump_cycle,gal_per_metered_sump_wh"
    )
    for r in results:
        print(
            ",".join(
                [
                    str(r.index),
                    r.start_ts,
                    r.end_ts,
                    f"{r.septic_wh:.3f}",
                    f"{r.septic_cycles_est:.3f}",
                    f"{r.gallons_out_est:.3f}",
                    f"{r.sump_wh:.3f}",
                    f"{r.sump_cycles_est:.3f}",
                    "NA" if r.gal_per_sump_cycle is None else f"{r.gal_per_sump_cycle:.3f}",
                    "NA" if r.gal_per_sump_wh is None else f"{r.gal_per_sump_wh:.3f}",
                ]
            )
        )

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())