-- Remove phantom baseline from hourly rows by subtraction (clamped at zero).
-- Rule:
-- - Subtract 4 Wh from each full hour block 10:00 through 20:59.
-- - Subtract 4 * (19/60) Wh from the 21:00 block because EM reset at 21:19.

-- 1) Preview before applying.
SELECT
    datetime,
    consumption AS raw_wh,
    CASE
        WHEN datetime >= '2026-08-06 10:00:00' AND datetime < '2026-08-06 21:00:00' THEN 4.0
        WHEN datetime = '2026-08-06 21:00:00' THEN 4.0 * (19.0 / 60.0)
        ELSE 0.0
    END AS subtract_wh,
    MAX(
        0.0,
        COALESCE(consumption, 0) - CASE
            WHEN datetime >= '2026-08-06 10:00:00' AND datetime < '2026-08-06 21:00:00' THEN 4.0
            WHEN datetime = '2026-08-06 21:00:00' THEN 4.0 * (19.0 / 60.0)
            ELSE 0.0
        END
    ) AS corrected_wh
FROM main.energy_history
WHERE device_id = 'e4b063d424a0'
    AND datetime >= '2026-08-06 10:00:00'
    AND datetime <= '2026-08-06 21:00:00'
ORDER BY datetime;

-- 2) Apply correction in place.
BEGIN TRANSACTION;

UPDATE main.energy_history
SET consumption = MAX(
    0.0,
    COALESCE(consumption, 0) - CASE
        WHEN datetime >= '2026-08-06 10:00:00' AND datetime < '2026-08-06 21:00:00' THEN 4.0
        WHEN datetime = '2026-08-06 21:00:00' THEN 4.0 * (19.0 / 60.0)
        ELSE 0.0
    END
)
WHERE device_id = 'e4b063d424a0'
    AND datetime >= '2026-08-06 10:00:00'
    AND datetime <= '2026-08-06 21:00:00';

-- 3) Sanity check removed energy in this correction window.
-- Expected theoretical removal (before clamping) = 11 * 4 + 4*(19/60) = 45.2667 Wh.
SELECT
    COUNT(*) AS corrected_rows,
    SUM(
        CASE
            WHEN datetime >= '2026-08-06 10:00:00' AND datetime < '2026-08-06 21:00:00' THEN 4.0
            WHEN datetime = '2026-08-06 21:00:00' THEN 4.0 * (19.0 / 60.0)
            ELSE 0.0
        END
    ) AS target_subtract_wh
FROM main.energy_history
WHERE device_id = 'e4b063d424a0'
    AND datetime >= '2026-08-06 10:00:00'
    AND datetime <= '2026-08-06 21:00:00';

-- 4) Final cleanup: zero any tiny hourly residual under 0.5 Wh.
UPDATE main.energy_history
SET consumption = 0.0
WHERE device_id = 'e4b063d424a0'
    AND datetime >= '2026-08-06 10:00:00'
    AND datetime <= '2026-08-06 21:00:00'
    AND COALESCE(consumption, 0) < 0.5;

-- Optional check: confirm no sub-0.5 Wh rows remain in this window.
SELECT COUNT(*) AS sub_half_wh_rows_remaining
FROM main.energy_history
WHERE device_id = 'e4b063d424a0'
    AND datetime >= '2026-08-06 10:00:00'
    AND datetime <= '2026-08-06 21:00:00'
    AND COALESCE(consumption, 0) > 0
    AND COALESCE(consumption, 0) < 0.5;

COMMIT;

