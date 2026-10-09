#!/bin/sh
# Managed by Ansible (roles/base). Restarts UNIT (k3s or k3s-agent) when
# flannel has lost its binding to tailscale0.
#
#   tailscale-flannel-guard.sh UNIT [STAGGER_SECONDS]
#
# Runs from a 30 s timer, and straight away whenever tailscaled creates a new
# tailscale0 (the service is wanted by tailscale0's device unit).
#
# A Tailscale update restarts tailscaled on every node within seconds. If every
# node running an etcd member then restarted k3s at once, etcd would lose
# quorum, so those nodes take turns: before restarting k3s, a node whose etcd
# member is up waits until it is connected to all the other members, holds off
# for STAGGER_SECONDS, and restarts only if they are all still connected; if
# not, it waits again. Nodes with different STAGGER_SECONDS therefore restart
# one at a time. After MAX_WAIT it restarts regardless: a dead pod network is
# worse than a short loss of quorum.
#
# Whether a node takes turns is decided at run time, not by its role: a server
# can run without etcd (--disable-etcd, an external datastore) and an agent
# never runs it. A node with nothing listening on etcd's metrics endpoint adds
# nothing to quorum right now, so it restarts at once. That includes a server
# whose k3s is already down.
#
# The GUARD_* variables only exist so that tests can point the script at a
# fake /sys, /proc/uptime, state directory and etcd metrics endpoint.
set -eu

unit="$1"
stagger="${2:-0}"
net="${GUARD_SYSFS_NET:-/sys/class/net}"
uptime_file="${GUARD_UPTIME:-/proc/uptime}"
state_dir="${GUARD_STATE_DIR:-/run/tailscale-flannel-guard}"
etcd_metrics="${GUARD_ETCD_METRICS:-http://127.0.0.1:2381/metrics}"
ifindex_state="$state_dir/ifindex"
restart_state="$state_dir/last-restart"
grace=300     # give k3s this long to build flannel.1 before judging it
cooldown=600  # never restart more often than this
max_wait=300  # longest a server waits for its turn

mkdir -p "$state_dir"

# Monotonic seconds since boot. Resets on reboot, which is fine: both state
# files live in /run and are cleared by the same reboot.
uptime_s() {
  cut -d. -f1 "$uptime_file"
}
now=$(uptime_s)

# True when this node's etcd member has a leader and is connected to every
# other member. In etcd's metrics, known peers set to 1 are the current members
# (this one included; removed members stay listed at 0), and active peers set
# to 1 are the members this one is connected to.
etcd_peers_connected() {
  metrics=$(curl -fsS --max-time 3 "$etcd_metrics" 2>/dev/null) || return 1
  printf '%s\n' "$metrics" | grep -qx 'etcd_server_has_leader 1' || return 1
  known=$(printf '%s\n' "$metrics" | grep -c '^etcd_network_known_peers{.*} 1$') || true
  active=$(printf '%s\n' "$metrics" | grep -c '^etcd_network_active_peers{.*} 1$') || true
  [ "$known" -ge 1 ] && [ "$active" -eq "$((known - 1))" ]
}

# True unless nothing is listening on the etcd metrics endpoint, i.e. unless
# this node has no running etcd member. Any other failure (a timeout, an HTTP
# error) means a member that is up but unwell, which still counts.
etcd_running() {
  rc=0
  curl -sS --max-time 3 -o /dev/null "$etcd_metrics" 2>/dev/null || rc=$?
  [ "$rc" -ne 7 ]
}

# Wait until all etcd members are connected, hold off for the stagger and
# check again, so that etcd nodes restart one at a time. Succeeds early if this
# node's own member goes away meanwhile, since restarting no longer costs
# quorum. Fails after max_wait.
wait_for_turn() {
  deadline=$(($(uptime_s) + max_wait))
  while [ "$(uptime_s)" -lt "$deadline" ]; do
    if ! etcd_running; then
      return 0
    fi
    if etcd_peers_connected; then
      sleep "$stagger"
      if etcd_peers_connected; then
        return 0
      fi
    else
      sleep 2
    fi
  done
  return 1
}

restart_unit() {
  if [ -f "$restart_state" ]; then
    last=$(cat "$restart_state")
    if [ "$((now - last))" -lt "$cooldown" ]; then
      logger -t tailscale-flannel-guard \
        "$1, but $unit was restarted $((now - last))s ago; skipping"
      return 0
    fi
  fi
  if etcd_running; then
    if ! wait_for_turn; then
      logger -t tailscale-flannel-guard \
        "$1; etcd members still not all connected after ${max_wait}s, restarting $unit anyway"
    fi
  fi
  logger -t tailscale-flannel-guard "$1; restarting $unit"
  uptime_s > "$restart_state"
  systemctl restart "$unit"
}

current=$(cat "$net/tailscale0/ifindex" 2>/dev/null || true)
if [ -z "$current" ]; then
  # tailscale0 is gone entirely; wait for it to come back rather than
  # restarting k3s into an interface that does not exist yet.
  exit 0
fi

previous=""
if [ -f "$ifindex_state" ]; then
  previous=$(cat "$ifindex_state")
fi
printf '%s\n' "$current" > "$ifindex_state"

if [ -n "$previous" ] && [ "$current" != "$previous" ]; then
  restart_unit "tailscale0 ifindex $previous -> $current"
  exit 0
fi

# Ifindexes can be reused, so also check the symptom directly: k3s settled,
# tailscale0 present, but flannel never built its vxlan device. This is the
# state a missed transition leaves behind, and it is what flannel logs as
# "external interface  not found, retrying in 30s" forever.
if [ -e "$net/flannel.1" ]; then
  exit 0
fi

if [ "$(systemctl is-active "$unit" 2>/dev/null || true)" != "active" ]; then
  exit 0
fi

active_us=$(systemctl show "$unit" -p ActiveEnterTimestampMonotonic --value)
case "$active_us" in
  ''|*[!0-9]*) exit 0 ;;
esac
if [ "$((now - active_us / 1000000))" -lt "$grace" ]; then
  exit 0
fi

restart_unit "flannel.1 missing while tailscale0 is up"
