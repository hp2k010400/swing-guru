"""Internal blind coach-review workflow for validating the Swing Score.

Separate from the product: a local, loopback-only review server, its own SQLite
database under ``var/coach_review``, and an evidence-only comparison report.
Nothing here reads or writes the analysis pipeline, the production service, the
mobile app or the stored Swing Score model.
"""

from . import config

__all__ = ["config"]
