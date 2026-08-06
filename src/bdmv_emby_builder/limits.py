"""Shared safety limits for configuration and serialized build plans."""

MAX_COPY_BOUNDARY_TOLERANCE_SECONDS = 0.5
MAX_DURATION_TOLERANCE_SECONDS = 5.0

# Shared planner/builder recognition boundaries. Keeping the producer and the
# untrusted-plan validator on the same constants prevents semantic drift.
EPISODE_MIN_SECONDS = 10 * 60
EPISODE_MAX_SECONDS = 60 * 60
EPISODE_DURATION_RATIO = 1.75
PLAY_ALL_MAX_ITEM_SECONDS = 5 * 60
IGNORABLE_EXTRA_SUBPATH_TYPES = frozenset({3})
