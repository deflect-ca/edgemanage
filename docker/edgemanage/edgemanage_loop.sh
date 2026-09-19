#!/bin/sh
#
# Run edge_manage on a loop, logging to stdout. The production image uses cron
# and tails a logfile; for local testing we want the output in
# `docker compose logs -f edgemanage` instead.

set -u

: "${DNET:=dnet1}"
: "${RUN_INTERVAL:=60}"
: "${EXTRA_ARGS:=}"

echo "edgemanage_loop: dnets=${DNET} interval=${RUN_INTERVAL}s extra_args='${EXTRA_ARGS}'"

# DNET is a space-separated list. One edge_manage invocation per dnet, run in
# order, which is how production does it - a cron line per dnet on one host,
# sharing one config file and one healthdata_store. Sequential rather than
# backgrounded for the same reason the config pins workers to 1, and because
# the lockfile is per-host, not per-dnet: two concurrent runs would have the
# second one die on the lock.
while true; do
    for dnet in $DNET; do
        # -v logs to stderr rather than to `logpath`. Every line is tagged with
        # [<dnet>], so interleaved dnets stay readable.
        edge_manage -A "$dnet" $EXTRA_ARGS || \
            echo "edgemanage_loop: edge_manage -A $dnet exited $?"
    done
    sleep "$RUN_INTERVAL"
done
