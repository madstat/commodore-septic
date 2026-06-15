

#%%
import os
import argparse
import json
import requests
from dotenv import load_dotenv

# Always reload .env values so changes are picked up even if the process already had
# a DEVICE_IDS value from an earlier session.
load_dotenv('.env', override=True)


def validate_config():
    missing = []
    if not SHELLY_HOST:
        missing.append("SHELLY_HOST")
    if not SHELLY_AUTH_KEY:
        missing.append("SHELLY_AUTH_KEY")
    if not DEVICE_IDS or DEVICE_IDS.startswith("your_"):
        missing.append("DEVICE_IDS")

    if missing:
        raise RuntimeError(
            "Missing Shelly configuration in .env: " + ", ".join(missing) +
            ". Set them before running get_pm_data.py or the interactive cells."
        )


def get_auth_token():
    return SHELLY_AUTH_KEY


def call_v2_devices_api(host, auth_key, body, timeout=10):
    """Call the curl-style v2 devices API: POST https://<HOST>/v2/devices/api/get?auth_key=<AUTH_KEY>

    The body should follow the Shelly v2 format, for example:
    {
        "ids": ["device1", "device2"],
        "select": ["status"]
    }
    """
    if not host or not auth_key:
        raise RuntimeError("SHELLY_HOST and SHELLY_AUTH_KEY must be set for v2 API calls")

    url = host.rstrip('/') + '/v2/devices/api/get'
    params = {"auth_key": auth_key}
    headers = {"Content-Type": "application/json"}

    r = requests.post(url, params=params, json=body, headers=headers, timeout=timeout)
    try:
        r.raise_for_status()
    except requests.HTTPError as exc:
        response_text = r.text.strip() if r is not None else "<no response body>"
        raise RuntimeError(
            f"Shelly v2 request failed {r.status_code}: {response_text}"
        ) from exc
    return r.json()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Fetch Shelly device state through the v2 devices API.")
    parser.add_argument(
        "--select",
        nargs="+",
        default=["status"],
        help="Select data to fetch for each device: status and/or settings."
    )
    args, _ = parser.parse_known_args(argv)
    return args



# %%
# ============================================================
# SHELLY CONFIGURATION
# ============================================================
SHELLY_HOST = os.getenv("SHELLY_HOST", "")
SHELLY_AUTH_KEY = os.getenv("SHELLY_AUTH_KEY", "")
DEVICE_IDS = os.getenv("DEVICE_IDS", "")
# ============================================================

if __name__ == "__main__":
    args = parse_args()
    validate_config()
    token = get_auth_token()
    # print('DEVICE_IDS raw:', repr(DEVICE_IDS))
    device_list = [d.strip() for d in DEVICE_IDS.split(",") if d.strip()]
    # print('DEVICE_IDS parsed:', device_list)
    body = {"ids": device_list, "select": args.select}
    resp = call_v2_devices_api(SHELLY_HOST, token, body)
    print(json.dumps(resp, indent=2))




# %%
