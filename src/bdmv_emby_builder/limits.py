"""Shared safety limits for configuration and serialized build plans."""

MAX_COPY_BOUNDARY_TOLERANCE_SECONDS = 0.5
MAX_DURATION_TOLERANCE_SECONDS = 5.0

# Shared planner/builder recognition boundaries. Keeping the producer and the
# untrusted-plan validator on the same constants prevents semantic drift.
EPISODE_MIN_SECONDS = 10 * 60
EPISODE_MAX_SECONDS = 60 * 60
EPISODE_DURATION_RATIO = 1.75
EPISODE_PROFILE_DURATION_RATIO = 1.25
EPISODE_GROUP_DURATION_RATIO = 1.15
SEPARATE_EPISODE_DURATION_RATIO = 1.15
EPISODE_BOUNDARY_SHORT_ITEM_SECONDS = 30
# Bounds the quadratic dynamic programming used only for conservative episode
# inference. Real authored episode/chapter counts are far below this; unusual
# playlists safely remain unsplit instead of consuming unbounded planning CPU.
MAX_EPISODE_INFERENCE_BOUNDARIES = 1024
PLAY_ALL_MAX_ITEM_SECONDS = 5 * 60
IGNORABLE_EXTRA_SUBPATH_TYPES = frozenset({3})

# Lightweight review hints for short, low-confidence extras. These do not
# authorize automatic exclusion; they only decide when the plan should ask for
# human review. FFmpeg already belongs to the core workflow, so no OCR or
# additional runtime is required.
EXTRA_CONTENT_ANALYSIS_MAX_SECONDS = 5 * 60
EXTRA_NEAR_SILENCE_MAX_DB = -55.0
EXTRA_STATIC_SAMPLE_SECONDS = 3.0
EXTRA_STATIC_SAMPLE_RATIOS = (0.2, 0.5, 0.8)
EXTRA_STATIC_MIN_SAMPLES = 2
# Keep freezedetect conservative: larger thresholds such as -20 dB can classify
# clearly animated frames as frozen. The FFmpeg default (-60 dB) still detects
# authored static cards while avoiding that false-positive class.
EXTRA_FREEZE_NOISE_DB = -60.0
EXTRA_FREEZE_MIN_SECONDS = 1.5
