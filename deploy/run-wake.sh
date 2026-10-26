#!/bin/sh
# One bounded claim/context handoff for a runtime hook. This does not start
# a listener/model, install a scheduler, or mark any work complete.
set -eu
exec "$(dirname "$0")/run-worker.sh" "$@" next --with-context
