#!/bin/bash
# Make the src/ layout importable regardless of how the app is installed.
export PYTHONPATH="$(dirname "$0")/src:${PYTHONPATH}"
exec python -m uvicorn wrapper.http_server:app --app-dir src --host 0.0.0.0 --port "${PORT:-8000}"
