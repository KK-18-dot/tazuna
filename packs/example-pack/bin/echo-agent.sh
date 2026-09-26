#!/usr/bin/env bash
# A stand-in "agent": prints what it received and reports completion in the tazuna
# vocabulary. Replace the body with a call to any CLI to turn this pack into a real provider.
set -euo pipefail
effort="-"
args=()
while (($#)); do
  case "$1" in
    --effort) effort="$2"; shift 2 ;;
    *) args+=("$1"); shift ;;
  esac
done
echo "[echo-agent] cwd=$(pwd) effort=$effort"
echo "[echo-agent] task follows:"
printf '%s\n' "${args[@]:-}"
echo
echo "No files were changed."
echo "TAZUNA-STATUS: DONE"
