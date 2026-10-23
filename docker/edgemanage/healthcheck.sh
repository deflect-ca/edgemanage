#!/bin/sh
#
# Unhealthy if any dnet's statefile hasn't had a last_run written within two
# run intervals - i.e. edge_manage is wedged or crashing every iteration for
# that dnet.

set -u

: "${DNET:=dnet1}"
: "${RUN_INTERVAL:=60}"

# DNET is a space-separated list, as in edgemanage_loop.sh; the statefile is
# per dnet (`statefile` in docker/conf/edgemanage.yaml).
exec python3 - "$RUN_INTERVAL" $DNET <<'EOF'
import json
import sys
import time

interval, dnets = int(sys.argv[1]), sys.argv[2:]

problems, ages = [], []
for dnet in dnets:
    statefile = "/var/lib/edgemanage/%s.state" % dnet
    try:
        with open(statefile) as state_f:
            last_run = json.load(state_f).get("last_run")
    except (OSError, ValueError) as e:
        problems.append("no usable statefile at %s: %s" % (statefile, e))
        continue

    if not last_run:
        problems.append("statefile %s has no last_run" % statefile)
        continue

    age = time.time() - float(last_run)
    if age > interval * 2:
        problems.append("%s: last run was %ds ago, more than two intervals" % (dnet, age))
    else:
        ages.append("%s: last run was %ds ago" % (dnet, age))

if problems:
    sys.exit("; ".join(problems))

print("; ".join(ages))
EOF
