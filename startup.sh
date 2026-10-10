#!/bin/bash
uvicorn wrapper.http_server:app --app-dir src --host 0.0.0.0 --port $PORT
