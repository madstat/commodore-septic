
WITH
params AS (
    SELECT
        'e4b063d424a0' AS sump_device_id,
        'e4b063d4444c' AS septic_device_id,
        '2026-07-07 18:00:00' AS model_start_datetime,
        datetime('now', 'localtime') AS as_of_datetime,
        4.723 AS gallons_in_per_metered_sump_wh,
        20.0 AS gallons_out_per_septic_cycle,
        19.673198 AS septic_wh_per_cycle,
        3.0 AS septic_run_gap_hours,
        4.0 AS empty_reset_gap_hours,
        5 AS min_cycles_for_empty_reset,
        900.0 AS tank_capacity_gallons,
        0.0 AS initial_tank_gallons
),
hourly_energy AS (
    SELECT
        a.datetime,
        COALESCE(a.consumption, 0.0) AS sump_energy_wh,
        COALESCE(b.consumption, 0.0) AS septic_energy_wh
    FROM main.energy_history a
    JOIN main.energy_history b
      ON a.datetime = b.datetime
    JOIN params p
      ON a.device_id = p.sump_device_id
     AND b.device_id = p.septic_device_id
    WHERE a.datetime >= p.model_start_datetime
      AND a.datetime <= p.as_of_datetime
    ORDER BY a.datetime
),
septic_cycle_rows AS (
    SELECT
        he.datetime,
        CASE
            WHEN LAG(he.datetime) OVER (ORDER BY he.datetime) IS NULL THEN 1
            WHEN (julianday(he.datetime) - julianday(LAG(he.datetime) OVER (ORDER BY he.datetime))) * 24.0 > p.septic_run_gap_hours THEN 1
            ELSE 0
        END AS starts_new_run
    FROM hourly_energy he
    CROSS JOIN params p
    WHERE he.septic_energy_wh > 0
),
septic_cycle_runs AS (
    SELECT
        datetime,
        SUM(starts_new_run) OVER (ORDER BY datetime) AS run_id
    FROM septic_cycle_rows
),
septic_run_summary AS (
    SELECT
        scr.run_id,
        MIN(scr.datetime) AS run_start_datetime,
        MAX(scr.datetime) AS run_end_datetime,
        COUNT(*) AS run_cycle_count,
        datetime(
            MAX(scr.datetime),
            printf('+%g hours', p.empty_reset_gap_hours)
        ) AS reset_to_zero_datetime
    FROM septic_cycle_runs scr
    CROSS JOIN params p
    GROUP BY scr.run_id
),
hourly_flow AS (
    SELECT
        he.datetime,
        he.sump_energy_wh,
        he.septic_energy_wh,
        he.sump_energy_wh * p.gallons_in_per_metered_sump_wh AS gallons_in,
        (he.septic_energy_wh / p.septic_wh_per_cycle) AS septic_cycles_est,
        (he.septic_energy_wh / p.septic_wh_per_cycle) * p.gallons_out_per_septic_cycle AS gallons_out,
        (he.sump_energy_wh * p.gallons_in_per_metered_sump_wh)
          - ((he.septic_energy_wh / p.septic_wh_per_cycle) * p.gallons_out_per_septic_cycle) AS net_gallons,
                (
                        SELECT MAX(srs.reset_to_zero_datetime)
                        FROM septic_run_summary srs
                        WHERE srs.run_cycle_count >= p.min_cycles_for_empty_reset
                            AND srs.reset_to_zero_datetime <= he.datetime
                ) AS reset_to_zero_datetime,
        p.tank_capacity_gallons,
        p.initial_tank_gallons
    FROM hourly_energy he
    CROSS JOIN params p
),
ordered AS (
    SELECT
        ROW_NUMBER() OVER (ORDER BY datetime) AS rn,
        datetime,
        sump_energy_wh,
        septic_energy_wh,
        gallons_in,
        septic_cycles_est,
        gallons_out,
        net_gallons,
        reset_to_zero_datetime,
        tank_capacity_gallons,
        initial_tank_gallons
    FROM hourly_flow
),
tank_balance AS (
    SELECT
        o.rn,
        o.datetime,
        o.sump_energy_wh,
        o.septic_energy_wh,
        o.gallons_in,
        o.septic_cycles_est,
        o.gallons_out,
        o.net_gallons,
        o.reset_to_zero_datetime AS last_reset_to_zero_datetime,
        0 AS reset_due_to_septic_gap,
        MAX(0.0, o.initial_tank_gallons + o.net_gallons - o.tank_capacity_gallons) AS overflow_gallons_this_hour,
        CASE
            WHEN MIN(o.tank_capacity_gallons, MAX(0.0, o.initial_tank_gallons + o.net_gallons)) <= 0.0
                THEN 0.0
            ELSE MAX(0.0, o.initial_tank_gallons + o.net_gallons - o.tank_capacity_gallons)
        END AS overflow_gallons_since_empty,
        MIN(
            o.tank_capacity_gallons,
            MAX(0.0, o.initial_tank_gallons + o.net_gallons)
        ) AS tank_level_gallons
    FROM ordered o
    WHERE o.rn = 1

    UNION ALL

    SELECT
        o.rn,
        o.datetime,
        o.sump_energy_wh,
        o.septic_energy_wh,
        o.gallons_in,
        o.septic_cycles_est,
        o.gallons_out,
        o.net_gallons,
        o.reset_to_zero_datetime AS last_reset_to_zero_datetime,
        CASE
            WHEN o.reset_to_zero_datetime IS NOT NULL
             AND (tb.last_reset_to_zero_datetime IS NULL
               OR o.reset_to_zero_datetime > tb.last_reset_to_zero_datetime)
                THEN 1
            ELSE 0
        END AS reset_due_to_septic_gap,
        MAX(0.0,
            (
                CASE
                    WHEN o.reset_to_zero_datetime IS NOT NULL
                     AND (tb.last_reset_to_zero_datetime IS NULL
                       OR o.reset_to_zero_datetime > tb.last_reset_to_zero_datetime)
                        THEN 0.0
                    ELSE tb.tank_level_gallons
                END
            ) + o.net_gallons - o.tank_capacity_gallons
        ) AS overflow_gallons_this_hour,
        CASE
            WHEN MIN(
                o.tank_capacity_gallons,
                MAX(
                    0.0,
                    (
                        CASE
                            WHEN o.reset_to_zero_datetime IS NOT NULL
                             AND (tb.last_reset_to_zero_datetime IS NULL
                               OR o.reset_to_zero_datetime > tb.last_reset_to_zero_datetime)
                                THEN 0.0
                            ELSE tb.tank_level_gallons
                        END
                    ) + o.net_gallons
                )
            ) <= 0.0
                THEN 0.0
            ELSE
                CASE
                    WHEN o.reset_to_zero_datetime IS NOT NULL
                     AND (tb.last_reset_to_zero_datetime IS NULL
                       OR o.reset_to_zero_datetime > tb.last_reset_to_zero_datetime)
                        THEN MAX(0.0,
                            (
                                CASE
                                    WHEN o.reset_to_zero_datetime IS NOT NULL
                                     AND (tb.last_reset_to_zero_datetime IS NULL
                                       OR o.reset_to_zero_datetime > tb.last_reset_to_zero_datetime)
                                        THEN 0.0
                                    ELSE tb.tank_level_gallons
                                END
                            ) + o.net_gallons - o.tank_capacity_gallons
                        )
                    ELSE tb.overflow_gallons_since_empty + MAX(0.0,
                        (
                            CASE
                                WHEN o.reset_to_zero_datetime IS NOT NULL
                                 AND (tb.last_reset_to_zero_datetime IS NULL
                                   OR o.reset_to_zero_datetime > tb.last_reset_to_zero_datetime)
                                    THEN 0.0
                                ELSE tb.tank_level_gallons
                            END
                        ) + o.net_gallons - o.tank_capacity_gallons
                    )
                END
        END AS overflow_gallons_since_empty,
        MIN(
            o.tank_capacity_gallons,
            MAX(
                0.0,
                (
                    CASE
                        WHEN o.reset_to_zero_datetime IS NOT NULL
                         AND (tb.last_reset_to_zero_datetime IS NULL
                           OR o.reset_to_zero_datetime > tb.last_reset_to_zero_datetime)
                            THEN 0.0
                        ELSE tb.tank_level_gallons
                    END
                ) + o.net_gallons
            )
        ) AS tank_level_gallons
    FROM ordered o
    JOIN tank_balance tb
      ON o.rn = tb.rn + 1
)
SELECT
    datetime,
    sump_energy_wh,
    septic_energy_wh,
    ROUND(gallons_in, 3) AS gallons_in,
    ROUND(septic_cycles_est, 3) AS septic_cycles_est,
    ROUND(gallons_out, 3) AS gallons_out,
    ROUND(net_gallons, 3) AS net_gallons,
    reset_due_to_septic_gap,
    ROUND(overflow_gallons_this_hour, 3) AS overflow_gallons_this_hour,
    ROUND(overflow_gallons_since_empty, 3) AS overflow_gallons_since_empty,
    ROUND(tank_level_gallons, 3) AS tank_level_gallons
FROM tank_balance
ORDER BY datetime;
