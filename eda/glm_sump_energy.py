"""
GLM: predict average hourly sump pump energy usage per booking stay.

Target  : avg Wh/hr from energy_history (device given by SUMP_DEVICE_ID env var) during booking window
Predictors: guest_count (adults + children)
Family  : Gamma(log) — positive continuous outcome, right-skewed
"""

import os
import sqlite3

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import statsmodels.api as sm
from dotenv import load_dotenv

load_dotenv(".env", override=True)

DB_PATH = "data/commodore_history.db"
DEVICE_ID = os.getenv("SUMP_DEVICE_ID", "").strip()

QUERY = """
SELECT
    b.booking_id,
    b.adults + b.children AS guest_count,
    COUNT(e.id)          AS energy_hours,
    AVG(e.consumption)   AS avg_hourly_wh
FROM bookings b
JOIN energy_history e
    ON  e.device_id = :device
    AND e.datetime >= (b.arrival_date   || ' ' || COALESCE(b.check_in_time,  '16:00:00'))
    AND e.datetime <  (b.departure_date || ' ' || COALESCE(b.check_out_time, '11:00:00'))
    AND e.consumption IS NOT NULL
WHERE b.is_deleted = 0
  AND b.status = 'Booked'
  AND b.source = 'AirbnbIntegration'
GROUP BY b.booking_id
HAVING energy_hours > 0
   AND avg_hourly_wh  > 0   -- Gamma requires strictly positive response
ORDER BY b.arrival_date
"""

def load_data() -> pd.DataFrame:
    with sqlite3.connect(DB_PATH) as con:
        return pd.read_sql_query(QUERY, con, params={"device": DEVICE_ID})


def fit_glm(df: pd.DataFrame):
    y = df["avg_hourly_wh"]
    X = df[["guest_count"]]  # no intercept: zero guests → zero predicted consumption

    model = sm.GLM(
        y,
        X,
        family=sm.families.Gamma(link=sm.families.links.Log()),
    )
    result = model.fit()
    return result


def main():
    if not DEVICE_ID:
        raise SystemExit("Error: SUMP_DEVICE_ID environment variable is not set")
    df = load_data()
    print(f"Bookings with energy overlap: {len(df)}\n")
    print(df[["booking_id", "guest_count", "energy_hours", "avg_hourly_wh"]].to_string(index=False))
    print()

    result = fit_glm(df)
    print(result.summary())

    plot_scatter(df, result)

    # Exponentiated coefficients → multiplicative effect on mean energy usage
    ci = result.conf_int()
    coef_table = pd.DataFrame({
        "coef":          result.params,
        "exp(coef)":     np.exp(result.params),
        "p-value":       result.pvalues,
        "95% CI lower":  np.exp(ci[0]),
        "95% CI upper":  np.exp(ci[1]),
    })
    print("\nExponentiated coefficients (multiplicative effect on mean Wh/hr):")
    print(coef_table.to_string(float_format="{:.4f}".format))


def plot_scatter(df: pd.DataFrame, result) -> None:
    x = df["guest_count"]
    y = df["avg_hourly_wh"]

    x_range = np.linspace(1, x.max() + 1, 200)
    y_hat = np.exp(result.params["guest_count"] * x_range)

    fig, ax = plt.subplots(figsize=(6, 4))
    ax.scatter(x, y, zorder=3, label="Observed bookings")
    ax.plot(x_range, y_hat, label="GLM fit (Gamma/log, no intercept)")
    ax.set_xlabel("Guest count (adults + children)")
    ax.set_ylabel("Avg hourly sump pump usage (Wh/hr)")
    ax.legend()
    fig.tight_layout()
    plt.show()


if __name__ == "__main__":
    main()
