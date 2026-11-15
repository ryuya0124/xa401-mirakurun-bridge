#!/bin/sh
# The certificate is renewed by the owner's existing ACME job. Read only its
# public chain to detect rotation; never print/read the private key here.
set -eu
certificate=$1
nginx -g 'daemon off;' &
nginx_pid=$!
watch_certificate() {
    previous=$(sha256sum "$certificate")
    while sleep 300; do
        current=$(sha256sum "$certificate")
        if [ "$current" != "$previous" ]; then
            if nginx -t >/dev/null 2>&1 && nginx -s reload; then
                previous=$current
            fi
        fi
    done
}
watch_certificate &
watcher_pid=$!
shutdown() {
    kill -TERM "$watcher_pid" "$nginx_pid" 2>/dev/null || true
    wait "$nginx_pid" 2>/dev/null || true
    exit 0
}
trap shutdown TERM INT
set +e
wait "$nginx_pid"
status=$?
kill "$watcher_pid" 2>/dev/null || true
exit "$status"
