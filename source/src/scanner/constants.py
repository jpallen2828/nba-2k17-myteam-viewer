from __future__ import annotations

APP_NAME = "NBA 2K17 MyTEAM Scanner"
APP_VERSION = "0.7.0"
PROCESS_NAME = "NBA2K17.exe"
STEAM_APP_ID = "385760"

# Guardrails for captures. The GUI asks for explicit confirmation above the
# warning threshold and refuses accidentally absurd captures above the hard cap.
CAPTURE_WARNING_BYTES = 512 * 1024 * 1024
CAPTURE_HARD_LIMIT_BYTES = 4 * 1024 * 1024 * 1024
CAPTURE_CHUNK_BYTES = 4 * 1024 * 1024
SCAN_CHUNK_BYTES = 4 * 1024 * 1024
MAX_SEARCH_RESULTS = 250_000
MAX_COMPARE_RESULTS = 500_000
MAX_GUIDED_CLUSTERS = 250
GUIDED_CLUSTER_BYTES = 0x400
GUIDED_PRIVATE_REGION_CAP = 256 * 1024

CONFIDENCE_LEVELS = ("UNKNOWN", "CANDIDATE", "LOW", "MEDIUM", "HIGH", "VERIFIED")

TIER_NAMES = (
    "BRONZE",
    "SILVER",
    "GOLD",
    "EMERALD",
    "SAPPHIRE",
    "RUBY",
    "AMETHYST",
    "DIAMOND",
    "PINK_DIAMOND",
    "ONYX",
)

BADGE_LEVEL_NAMES = (
    "NONE",
    "BRONZE",
    "SILVER",
    "GOLD",
    "HALL_OF_FAME",
    "UNKNOWN",
)
