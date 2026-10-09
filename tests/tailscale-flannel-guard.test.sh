#!/usr/bin/env bash
# Behaviour tests for ansible/roles/base/files/tailscale-flannel-guard.sh.
#
#   tests/tailscale-flannel-guard.test.sh [SHELL]
#
# SHELL is the interpreter that runs the script under test (default: sh, i.e.
# dash on Debian/Ubuntu; also try bash). Nothing on the machine is touched:
# /sys/class/net, /proc/uptime, the state directory and the etcd metrics are
# fakes in a temp dir, and systemctl, logger, curl and sleep are stubs. The
# sleep stub advances the fake clock instead of waiting, so the tests are
# instant and every timing is exact.
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$HERE/../ansible/roles/base/files/tailscale-flannel-guard.sh"
SH="${1:-sh}"
command -v "$SH" > /dev/null 2>&1 || { echo "$SH is required" >&2; exit 2; }

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
STUBS="$WORK/stubs"
mkdir "$STUBS"

# systemctl: unit state from $W/active, start time from $W/active_since_us;
# a restart is recorded with the fake time it happened at.
cat > "$STUBS/systemctl" <<'EOF'
#!/bin/sh
case "$1" in
  is-active)
    state=$(cat "$W/active")
    [ "$2" = --quiet ] || echo "$state"
    [ "$state" = active ] ;;
  show) cat "$W/active_since_us" ;;
  restart) echo "restart $2 at $(cat "$W/uptime")" >> "$W/calls" ;;
esac
EOF
# logger -t TAG MESSAGE
cat > "$STUBS/logger" <<'EOF'
#!/bin/sh
echo "$3" >> "$W/log"
EOF
# sleep N: advance the fake clock by N seconds.
cat > "$STUBS/sleep" <<'EOF'
#!/bin/sh
echo $(($(cat "$W/uptime") + $1)) > "$W/uptime"
EOF
# curl: the etcd metrics endpoint at the current fake time. $W/etcd has
# "FROM STATE" lines; the last line with FROM <= now wins. STATE is
#   none  nothing listening (curl exit 7: no etcd member on this node)
#   hung  a member that does not answer (curl exit 28)
#   up    a member connected to both other members
#   down  a member missing one of them
# With -o (the "is anything listening" probe) the body is discarded.
cat > "$STUBS/curl" <<'STUB'
#!/bin/sh
now=$(cat "$W/uptime")
state=$(awk -v now="$now" '$1 <= now { s = $2 } END { print s }' "$W/etcd")
case "$state" in
  none) exit 7 ;;
  hung) exit 28 ;;
esac
case " $* " in *" -o "*) exit 0 ;; esac
echo "etcd_server_has_leader 1"
echo 'etcd_network_known_peers{Local="a",Remote="a"} 1'
echo 'etcd_network_known_peers{Local="a",Remote="b"} 1'
echo 'etcd_network_known_peers{Local="a",Remote="c"} 1'
echo 'etcd_network_known_peers{Local="a",Remote="old"} 0'
echo 'etcd_network_active_peers{Local="a",Remote="b"} 1'
[ "$state" = down ] || echo 'etcd_network_active_peers{Local="a",Remote="c"} 1'
STUB
chmod +x "$STUBS"/*

pass=0
fail=0

ok() {
  pass=$((pass + 1))
  echo "  ok   $1"
}

bad() {
  fail=$((fail + 1))
  echo "  FAIL $1"
  { cat "$W/calls" "$W/log" 2>/dev/null || true; } | sed 's/^/       | /'
}

# check <description> <predicate> [args]: ok if the predicate holds for the last run.
check() {
  local desc="$1"
  shift
  if "$@"; then ok "$desc"; else bad "$desc"; fi
}

# A node at uptime 1000 s whose k3s has been up since 100 s, with tailscale0
# at ifindex 5 (recorded by an earlier run), flannel.1 present and no etcd
# member (cases that need one set $W/etcd).
fresh() {
  echo "== [$SH] $1"
  W="$WORK/case$((pass + fail))"
  export W
  mkdir -p "$W/sys/tailscale0" "$W/sys/flannel.1" "$W/state"
  echo 5 > "$W/sys/tailscale0/ifindex"
  echo 5 > "$W/state/ifindex"
  echo 1000 > "$W/uptime"
  echo active > "$W/active"
  echo 100000000 > "$W/active_since_us"
  echo "0 none" > "$W/etcd"
  : > "$W/calls"
  : > "$W/log"
}

guard() {
  PATH="$STUBS:$PATH" GUARD_SYSFS_NET="$W/sys" GUARD_UPTIME="$W/uptime" \
    GUARD_STATE_DIR="$W/state" GUARD_ETCD_METRICS=http://stub "$SH" "$SCRIPT" "$@"
}

new_ifindex() { echo "$1" > "$W/sys/tailscale0/ifindex"; }
restarted_at() { [ "$(cat "$W/calls")" = "restart $1 at $2" ]; }
not_restarted() { [ ! -s "$W/calls" ]; }
logged() { grep -qF -- "$1" "$W/log"; }
state_is() { [ "$(cat "$W/state/$1")" = "$2" ]; }

fresh "first run after boot"
rm "$W/state/ifindex"
guard k3s-agent
check "does not restart" not_restarted
check "records tailscale0's ifindex" state_is ifindex 5

fresh "nothing changed"
guard k3s 10
check "does not restart" not_restarted

fresh "tailscale0 is gone"
rm -r "$W/sys/tailscale0"
guard k3s-agent
check "does not restart" not_restarted
check "keeps the last ifindex it saw" state_is ifindex 5

fresh "agent, tailscale0 replaced"
new_ifindex 9
guard k3s-agent 30
check "restarts k3s-agent at once: no etcd member, no turn to wait for" restarted_at k3s-agent 1000
check "logs why" logged "tailscale0 ifindex 5 -> 9; restarting k3s-agent"
check "records the new ifindex" state_is ifindex 9
check "records the restart time for the cooldown" state_is last-restart 1000

fresh "server without etcd (--disable-etcd, external datastore)"
new_ifindex 9
guard k3s 10
check "restarts k3s at once: being a server is not what matters" restarted_at k3s 1000

fresh "server whose k3s is down (nothing listening)"
new_ifindex 9
echo inactive > "$W/active"
guard k3s 10
check "restarts at once, a stopped member costs no quorum" restarted_at k3s 1000

fresh "etcd member, all members connected"
new_ifindex 9
echo "0 up" > "$W/etcd"
guard k3s 10
check "restarts k3s after its 10 s stagger" restarted_at k3s 1010

fresh "etcd member with stagger 0 (the gateway)"
new_ifindex 9
echo "0 up" > "$W/etcd"
guard k3s 0
check "restarts k3s at once" restarted_at k3s 1000

fresh "etcd member, another member disconnected until 1040"
new_ifindex 9
printf '0 down\n1040 up\n' > "$W/etcd"
guard k3s 0
check "waits until it is back, then restarts" restarted_at k3s 1040

fresh "etcd member, another starts restarting during the stagger"
new_ifindex 9
printf '0 up\n1005 down\n1030 up\n' > "$W/etcd"
guard k3s 10
check "waits for it, staggers again, then restarts" restarted_at k3s 1040

fresh "etcd member, its own member stops while it waits"
new_ifindex 9
printf '0 down\n1020 none\n' > "$W/etcd"
guard k3s 10
check "stops waiting and restarts" restarted_at k3s 1020

fresh "etcd member, members never all connected"
new_ifindex 9
echo "0 down" > "$W/etcd"
guard k3s 10
check "restarts anyway after 300 s" restarted_at k3s 1300
check "says so" logged "still not all connected after 300s, restarting k3s anyway"

fresh "etcd member that does not answer (metrics time out)"
new_ifindex 9
echo "0 hung" > "$W/etcd"
guard k3s 0
check "counts as a member and waits, then restarts anyway after 300 s" restarted_at k3s 1300

fresh "restarted 100 s ago"
new_ifindex 9
echo 900 > "$W/state/last-restart"
guard k3s-agent
check "does not restart within the cooldown" not_restarted
check "logs that it skipped" logged "but k3s-agent was restarted 100s ago; skipping"

fresh "flannel.1 missing long after k3s started"
rm -r "$W/sys/flannel.1"
guard k3s-agent
check "restarts" restarted_at k3s-agent 1000
check "logs why" logged "flannel.1 missing while tailscale0 is up; restarting k3s-agent"

fresh "flannel.1 missing, but k3s started 100 s ago"
rm -r "$W/sys/flannel.1"
echo 900000000 > "$W/active_since_us"
guard k3s-agent
check "gives k3s its grace period" not_restarted

fresh "flannel.1 missing while k3s is stopped"
rm -r "$W/sys/flannel.1"
echo inactive > "$W/active"
guard k3s-agent
check "leaves a stopped k3s alone" not_restarted

echo
echo "[$SH] passed=$pass failed=$fail"
[ "$fail" = 0 ]
