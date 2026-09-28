"""Runtime defaults shared by every command that talks to the server.

The telnet target used to be a literal in each call site, so `capture` honored
`--telnet-port` while `scenario run` and `scenario matrix` talked to 127.0.0.1:8081
no matter what the operator asked for. One home for these values keeps the flag
defaults, the capture context, and the CLI honest about each other.

Leaf module: no imports, so collectors, capture, and the CLI can all depend on
it without a cycle.
"""

from __future__ import annotations

DEFAULT_TELNET_HOST = "127.0.0.1"
DEFAULT_TELNET_PORT = 8081
DEFAULT_GAME_PORT = 26902
