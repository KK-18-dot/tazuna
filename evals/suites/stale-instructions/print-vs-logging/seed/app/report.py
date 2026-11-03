import logging

log = logging.getLogger(__name__)


def load_rows(lines):
    rows = []
    for line in lines:
        if not line.strip():
            log.warning("skipping blank line")
            continue
        rows.append(line.split(","))
    log.info("loaded %d rows", len(rows))
    return rows
