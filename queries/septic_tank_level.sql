
WITH RECURSIVE day_expansion AS (
    -- Start with each booking's arrival date
    SELECT
        booking_id,
        arrival_date AS day,
        arrival_date,
        departure_date,
        check_in_time,
        check_out_time,
        adults,
        children,
        infants,
        pets
    FROM bookings

    UNION ALL

    -- Add one day at a time, up to 14 days
    SELECT
        booking_id,
        date(day, '+1 day'),
        arrival_date,
        departure_date,
        check_in_time,
        check_out_time,
        adults,
        children,
        infants,
        pets
    FROM day_expansion
    WHERE day < departure_date
),

expanded AS (
    SELECT
        booking_id,
        day AS stay_date,

        -- Start timestamp for this day
        datetime(
            CASE
                WHEN day = arrival_date THEN arrival_date || ' ' || check_in_time
                ELSE day || ' 00:00:00'
            END
        ) AS start_ts,

        -- End timestamp for this day
        datetime(
            CASE
                WHEN day = departure_date THEN departure_date || ' ' || check_out_time
                ELSE date(day, '+1 day') || ' 00:00:00'
            END
        ) AS end_ts,

        -- Weighted occupants
        (adults * 1.0) +
        (children * 1.0) +
        (infants * 0.5) +
        (pets * 0.0) AS occupants
    FROM day_expansion
),

septic_pump_log AS (

    SELECT
        eh.device_id,
        DATE(eh.datetime) AS date,
        SUM(eh.consumption) AS total_energy,
        ROUND(SUM(eh.consumption) / 0.21) AS pump_cycles,
        ROUND(SUM(eh.consumption) / 0.21)*25 AS gallons_pumped
    FROM main.energy_history eh
    JOIN main.devices d ON eh.device_id = d.device_id
    AND d.name = 'Septic Controls'
    WHERE eh.datetime >= '2025-05-01 00:00:00'
    GROUP BY eh.device_id, DATE(eh.datetime)
), 

daily_net_gallons AS (
    SELECT
        spl.date,

        -- SUM occupant-hours across all bookings touching this date
        SUM(
            COALESCE(
                e.occupants * ((julianday(e.end_ts) - julianday(e.start_ts)) * 24),
                0
            )
        ) AS occupant_hours,

        -- Weighted occupants not needed after grouping (hours already weighted)

        -- Gallons in = occupant-hours × (40 gallons per day / 24 hours)
        SUM(
            COALESCE(
                e.occupants * ((julianday(e.end_ts) - julianday(e.start_ts)) * 24) * (40.0 / 24.0),
                0
            )
        ) AS gallons_in,

        -- Pump data: take MAX because pump_cycles and gallons_pumped
        -- are already aggregated per day in septic_pump_log
        MAX(spl.pump_cycles) AS pump_cycles,
        MAX(spl.gallons_pumped) AS gallons_pumped,

        -- Net gallons = gallons_in - gallons_pumped
        SUM(
            COALESCE(
                e.occupants * ((julianday(e.end_ts) - julianday(e.start_ts)) * 24) * (40.0 / 24.0),
                0
            )
        ) - MAX(spl.gallons_pumped) AS net_gallons

    FROM septic_pump_log spl
    LEFT JOIN expanded e ON e.stay_date = spl.date

    GROUP BY spl.date
    ORDER BY spl.date
),

ordered AS (
    SELECT
        ROW_NUMBER() OVER (ORDER BY date) AS rn,
        date,
        occupant_hours,
        pump_cycles,
        net_gallons
    FROM daily_net_gallons
),

tank AS (
    -- Base row
    SELECT
        rn,
        date,
        net_gallons,
        pump_cycles,
        occupant_hours,
        CASE
            WHEN pump_cycles = 0 AND occupant_hours = 0 THEN 0
            ELSE MAX(0, net_gallons)
        END AS tank_level
    FROM ordered
    WHERE rn = 1

    UNION ALL

    SELECT
        o.rn,
        o.date,
        o.net_gallons,
        o.pump_cycles,
        o.occupant_hours,
        CASE
            WHEN o.pump_cycles = 0 AND o.occupant_hours = 0 THEN 0
            ELSE MAX(0, t.tank_level + o.net_gallons)
        END AS tank_level
    FROM ordered o
    JOIN tank t
      ON o.rn = t.rn + 1
)

SELECT
    dng.date,
    dng.occupant_hours,
    dng.gallons_in,
    dng.pump_cycles,
    dng.gallons_pumped,
    dng.net_gallons,
    t.tank_level
FROM daily_net_gallons dng
LEFT JOIN tank t ON dng.date = t.date
ORDER BY dng.date;
