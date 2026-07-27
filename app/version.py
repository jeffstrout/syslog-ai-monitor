"""Build provenance, baked into the Docker image by CI and exposed at /api/version.

Mirrors ac-monitor's version.py so both Python appliances report the same shape
(see jeffstrout/homelab-standards).
"""
from __future__ import annotations

import os


def get_version() -> dict[str, str]:
    return {
        "commit": os.environ.get("APP_COMMIT", "dev"),
        "built_at": os.environ.get("APP_BUILD_TIME", ""),
    }
