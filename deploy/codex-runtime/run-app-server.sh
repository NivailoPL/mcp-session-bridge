#!/bin/sh
set -eu

socket_path=$1
socket_group=$2
codex_binary=$3
runtime_dir=${socket_path%/*}
# App-server 0.163+ treats --listen as a symlink to its real socket in
# /tmp/codex-daemon-<uid>. The unit bind-mounts that directory from here, so the
# real socket also lives under the runtime directory, outside the private /tmp.
daemon_dir=$runtime_dir/daemon
listen_path=$runtime_dir/listen.sock

umask 077
chgrp "$socket_group" "$runtime_dir"
chmod 0750 "$runtime_dir"
# App-server refuses to start unless its daemon directory is exactly 0700.
if [ -d "$daemon_dir" ]; then
  chmod 0700 "$daemon_dir"
fi
rm -f "$socket_path" "$listen_path"

"$codex_binary" app-server --strict-config --listen "unix://$listen_path" &
codex_pid=$!
cleanup() {
  kill -TERM "$codex_pid" 2>/dev/null || true
  wait "$codex_pid" 2>/dev/null || true
  exit 0
}
trap cleanup INT TERM

attempt=0
while [ ! -S "$listen_path" ]; do
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

if [ -L "$listen_path" ]; then
  target=$(readlink "$listen_path")
  real_socket=$daemon_dir/${target##*/}
  if [ ! -S "$real_socket" ]; then
    kill -TERM "$codex_pid" 2>/dev/null || true
    wait "$codex_pid" || true
    echo "Codex app-server socket $target is not in the shared daemon directory." >&2
    exit 1
  fi
  # Once listening, let the sharing group traverse the daemon directory.
  chgrp "$socket_group" "$daemon_dir"
  chmod 0750 "$daemon_dir"
else
  real_socket=$listen_path
fi

# App-server hardens its socket parent after startup and refuses a group-writable
# one. Re-apply the dedicated sharing group (read and traverse only) after the
# socket exists, so the Bridge can reach it.
chgrp "$socket_group" "$runtime_dir"
chmod 0750 "$runtime_dir"
chgrp "$socket_group" "$real_socket"
chmod 0660 "$real_socket"
# A relative link resolves the same way inside and outside the sidecar's namespace.
ln -sfn "${real_socket#"$runtime_dir"/}" "$socket_path"
wait "$codex_pid"
