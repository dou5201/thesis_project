#!/bin/sh
# Start the Cantilever Beam DAQ (uses the hardware if available)
cd "$(dirname "$0")" && exec python3 run_app.py "$@"
