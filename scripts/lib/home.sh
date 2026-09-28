# Sourced fragment (no shebang by design): one resolved home directory for every
# shell entry point that falls back to a per-user path.
# shellcheck shell=sh
# HOME is unset in a bare systemd unit, under cron, and under `env -i`, and every
# consumer here runs with `set -u`, so an unguarded $HOME aborts the command with
# "HOME: unbound variable" instead of naming the fix. APM_HOME is therefore
# resolved once, and callers ask for it through apm_home_or_die at the point
# where the per-user fallback is actually taken: a caller given an explicit
# path override must keep working on a host with no HOME at all.
# shellcheck disable=SC2034  # APM_HOME is read by the sourcing script
: "${APM_HOME:=${HOME:-}}"

# Echo the resolved home, or fail with the fix spelled out. POSIX, so the
# Makefile's $(shell) and every bash script share one answer.
apm_home_or_die() {
  if [ -z "$APM_HOME" ]; then
    echo "ERROR: HOME is unset and no path override applies; export HOME, or set the override this command takes (SEVENDTD_DS_DIR, SEVENDTD_GAME_DIR, SEVENDTD_APM_DIR, XDG_CACHE_HOME)" >&2
    exit 1
  fi
  printf '%s' "$APM_HOME"
}
