# commodore-septic
A septic system risk model for The Commodore Hood Canal

## Usage
-`dvc repro snapshot_commodore_history` to backup local db
-Github commit and push before `dvc-push-box` to commit data to dvc remote on box account

## Notes
-`export_energy.py` pulls sump and septic pump energy usage from shelly cloud
-`septic_tank_level.py` estimate of septic tank level
-`calc_sump_calibration.py` fits gallons per Wh for sump pump
