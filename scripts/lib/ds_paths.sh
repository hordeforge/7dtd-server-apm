# Sourced fragment (no shebang by design): single default for the
# dedicated-server install directory.
# shellcheck shell=sh
# Every SEVENDTD_DS_DIR consumer (doctor, bridge build/install, probe helpers)
# resolves through here so the fallback path cannot drift between scripts; an
# explicit environment override always wins.
: "${SEVENDTD_DS_DIR:=$HOME/.local/share/Steam/steamapps/common/7 Days to Die Dedicated Server}"
