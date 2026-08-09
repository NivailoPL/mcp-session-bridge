#!/bin/sh
set -eu

socket_path=$1
socket_group=$2
codex_binary=$3
runtime_dir=${socket_path%/*}

umask 077
chgrp "$socket_group" "$runtime_dir"
chmod 0770 "$runtime_dir"

"$codex_binary" app-server --strict-config --listen "unix://$socket_path" &
codex_pid=$!
cleanup() {
  kill -TERM "$codex_pid" 2>/dev/null || true
  wait "$codex_pid" 2>/dev/null || true
  exit 0
}
trap cleanup INT TERM

attempt=0
while [ ! -S "$socket_path" ]; do
  if ! kill -0 "$codex_pid" 2>/dev/null; then
    wait "$codex_pid"
    exit $?
  fi
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 100 ]; then
    kill -TERM "$codex_pid" 2>/dev/null || true
    wait "$codex_pid" || true
    echo "Codex app-server did not create its Unix socket." >&2
    exit 1
  fi
  sleep 0.05
done

# App-server hardens its socket parent after startup. Re-apply the dedicated
# sharing group only after the socket exists, so the Bridge can traverse it.
chgrp "$socket_group" "$runtime_dir"
chmod 0770 "$runtime_dir"
chgrp "$socket_group" "$socket_path"
chmod 0660 "$socket_path"
wait "$codex_pid"
