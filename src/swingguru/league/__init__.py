"""Weekly leagues (1.0.1, staging): see docs/agent-work/release-1.0.1/LEAGUE_DESIGN.md."""

from .service import League, LeagueError
from .store import LeagueStore

__all__ = ["League", "LeagueError", "LeagueStore"]
