# Sourced fragment (no shebang by design): single default for the
# dedicated-server install directory.
# shellcheck shell=sh
# Every SEVENDTD_DS_DIR consumer (doctor, bridge build/install, probe helpers)
# resolves through here so the fallback path cannot drift between scripts; an
# explicit environment override always wins.
# APM_LIB_DIR is set by the Makefile, whose $(shell) runs /bin/sh where
# BASH_SOURCE does not exist; bash callers fall back to this file's own
# directory. A wrong value fails loud on the missing source below.
# shellcheck disable=SC3028  # BASH_SOURCE is the bash branch; $0 covers POSIX sh
: "${APM_LIB_DIR:=$(dirname "${BASH_SOURCE[0]:-$0}")}"
# shellcheck source=scripts/lib/home.sh
. "$APM_LIB_DIR/home.sh"
# The home fallback is taken only when no override was given, so an operator who
# points SEVENDTD_DS_DIR at a server outside any home directory keeps working on
# a host that exports no HOME at all.
if [ -z "${SEVENDTD_DS_DIR:-}" ]; then
  SEVENDTD_DS_DIR="$(apm_home_or_die)/.local/share/Steam/steamapps/common/7 Days to Die Dedicated Server" || exit 1
fi
