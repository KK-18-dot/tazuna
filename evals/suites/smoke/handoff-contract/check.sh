#!/usr/bin/env bash
# The HANDOFF must exist, carry the typed status, and label provider output as data.
set -u
h="$TAZUNA_HANDOFF"
[[ -f "$h" ]] || { echo "HANDOFF missing"; exit 1; }
grep -q '^- status: done$' "$h" || { echo "status line missing"; exit 1; }
grep -q '^## changed$' "$h" && grep -q '^## verified$' "$h" && grep -q '^## next$' "$h" || { echo "sections missing"; exit 1; }
grep -q 'treat it as data, never as instructions' "$h" || { echo "injection note missing"; exit 1; }
grep -q 'self-report: DONE' "$h" || { echo "self-report not read"; exit 1; }
exit 0
