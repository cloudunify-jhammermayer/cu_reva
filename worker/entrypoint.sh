#!/bin/sh
# Fix volume ownership then hand off to the worker process as the worker user.
chown -R worker:worker /repos 2>/dev/null || true
exec gosu worker "$@"
