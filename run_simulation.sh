#!/bin/sh
# Start the Cantilever Beam DAQ in simulation mode (no hardware needed)
cd "$(dirname "$0")" && exec python3 run_app.py --simulate "$@"
