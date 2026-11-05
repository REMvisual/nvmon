"""nvmon — monitoring and control daemon for NVIDIA Windows render nodes."""

from __future__ import annotations

# Bump this on every shipped change so the dashboard footer + /health both
# show which build is running. If the user reports a fix isn't taking effect,
# the version below is the first thing to check — the controller and the
# agents must report the SAME string before anyone debugs further.
__version__ = "0.9.11"
__all__ = ["__version__"]
