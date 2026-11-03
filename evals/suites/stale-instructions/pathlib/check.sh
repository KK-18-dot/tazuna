#!/usr/bin/env bash
# Grades the run; see ../grade.py.
exec python3 "$(dirname "$0")/../grade.py" pathlib "$1"
