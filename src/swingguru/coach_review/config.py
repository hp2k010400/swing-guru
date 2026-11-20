"""Constants for the coach-review workflow. No scoring numbers live here."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]

SCHEMA_VERSION = 1

# -- storage -----------------------------------------------------------------

DEFAULT_DB = ROOT / "var" / "coach_review" / "coach_review.sqlite3"
DEFAULT_REPORT = ROOT / "docs" / "agent-work" / "swing-score" / "VALIDATION_REPORT.md"
DEFAULT_REPORT_JSON = ROOT / "var" / "coach_review" / "report" / "validation_report.json"
DEFAULT_QA_DIR = ROOT / "var" / "qa" / "coach_review"

# -- review inputs -----------------------------------------------------------

RATING_FIELDS = ("setup", "backswing", "top", "downswing", "overall")
RATING_LABELS = {
    "setup": "Setup quality",
    "backswing": "Backswing / plane",
    "top": "Top position",
    "downswing": "Downswing / impact",
    "overall": "Overall technical quality",
}
RATING_MIN = 1
RATING_MAX = 10

CONFIDENCE_LEVELS = ("LOW", "MEDIUM", "HIGH")

MAX_NOTES = 2000
MAX_REVIEWER_ID = 64
MAX_REVIEWER_NAME = 80
MAX_BATCH_NAME = 120
MAX_BODY_BYTES = 16_384

# -- local server ------------------------------------------------------------

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8791
ALLOWED_HOSTS = ("127.0.0.1", "localhost")
VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v")
TOKEN_HEADER = "x-coach-review-token"

# -- report thresholds (documented in VALIDATION_GUIDE.md) -------------------

# Below this many rated swings the aggregate is prominently marked insufficient.
MIN_RATED_SWINGS_FOR_SUMMARY = 30
# Correlations need at least this many pairs with both values present and non-constant.
MIN_PAIRS_FOR_CORRELATION = 3

# Review flags. Point units: the model is 0-10 and coach ratings are 1-10, so they are
# compared as they are (no rescaling) and the floor difference is called out in the report.
FLAG_HIGH_MODEL_LOW_COACH = {"model_min": 8.0, "coach_max": 5.0}
FLAG_LOW_MODEL_HIGH_COACH = {"model_max": 5.0, "coach_min": 8.0}
FLAG_LARGE_DIFFERENCE = 2.0          # |model - coach| at or above this is listed as a disagreement
FLAG_LOW_COVERAGE = 0.75             # fewer than 6 of 8 checks measured
FLAG_DOMINANT_SHARE = 0.35           # one check carrying at least this share of the weight
FLAG_PLANE_SHARE = 0.30              # the two plane checks together carrying at least this share
# Similar-group instability review flag: the model spreads on swings the coach rates alike.
FLAG_INSTABILITY_MODEL_SPREAD = 1.5
FLAG_INSTABILITY_COACH_CLOSENESS = 1.0

# The pilot planned to collect this many blind coach-rated swings with stored scores before
# reading the aggregate. A planning convention for this work, not a universal threshold.
PLANNED_MIN_RATED_SWINGS = MIN_RATED_SWINGS_FOR_SUMMARY

__all__ = [name for name in dir() if not name.startswith("_")]
