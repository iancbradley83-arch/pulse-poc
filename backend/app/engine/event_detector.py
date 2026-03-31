"""Event Detector — rules-based engine that identifies interesting events from game state."""
from __future__ import annotations

from app.models.schemas import GameEvent, EventType


class EventDetector:
    """Evaluates game state changes and emits events when rules trigger."""

    def detect(self, game_id: str, game_state: dict, prev_state: dict | None = None) -> list[GameEvent]:
        events = []

        # Rule 1: Score change
        if prev_state:
            for side in ["home_score", "away_score"]:
                if game_state.get(side, 0) != prev_state.get(side, 0):
                    scorer_team = "home" if side == "home_score" else "away"
                    events.append(GameEvent(
                        game_id=game_id,
                        event_type=EventType.SCORE_CHANGE,
                        team_id=game_state.get(f"{scorer_team}_team_id"),
                        description=f"Score change: {game_state.get('home_score')}-{game_state.get('away_score')}",
                        data={"side": scorer_team, "new_score": game_state.get(side)}
                    ))

        # Rule 2: Player stat threshold approach (75%+ of O/U line)
        for player_stat in game_state.get("player_stats", []):
            pid = player_stat["player_id"]
            for stat_key, stat_val in player_stat.get("stats", {}).items():
                line = player_stat.get("lines", {}).get(stat_key)
                if line and stat_val >= line * 0.75:
                    pct = stat_val / line
                    events.append(GameEvent(
                        game_id=game_id,
                        event_type=EventType.THRESHOLD_APPROACH,
                        player_id=pid,
                        description=f"Player at {pct:.0%} of {stat_key} line ({stat_val}/{line})",
                        data={
                            "stat_type": stat_key, "current": stat_val,
                            "line": line, "percentage": round(pct, 3)
                        }
                    ))

        # Rule 3: Momentum shift (scoring run)
        run_data = game_state.get("scoring_run")
        if run_data and run_data.get("run_points", 0) >= 10:
            events.append(GameEvent(
                game_id=game_id,
                event_type=EventType.MOMENTUM_SHIFT,
                team_id=run_data["team_id"],
                description=f"{run_data['team_id']} on a {run_data['run_points']}-{run_data['opponent_points']} run",
                data=run_data
            ))

        # Rule 4: Milestone approach
        for milestone in game_state.get("milestones", []):
            if milestone.get("remaining", 999) <= 15:
                events.append(GameEvent(
                    game_id=game_id,
                    event_type=EventType.MILESTONE,
                    player_id=milestone.get("player_id"),
                    description=milestone.get("description", "Approaching milestone"),
                    data=milestone
                ))

        return events
