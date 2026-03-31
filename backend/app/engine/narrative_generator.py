"""Narrative Generator — creates card hooks using Claude API or template fallback."""

from app.models.schemas import GameEvent, Market, EventType
from app.config import ANTHROPIC_API_KEY, USE_LLM


SYSTEM_PROMPT = """You generate short, punchy narrative hooks for a sports betting context feed.
Given structured data about a game event, player stats, and a betting market,
write a 1-2 sentence hook that explains WHY this market is interesting RIGHT NOW.

Rules:
- Be specific with numbers
- Be direct, no fluff
- Reference the market line when relevant
- Make it feel urgent and timely
- Max 25 words
- Do NOT use hashtags or emojis"""


class NarrativeGenerator:
    def __init__(self):
        self._client = None
        if USE_LLM and ANTHROPIC_API_KEY:
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
            except Exception:
                pass

    async def generate(self, event: GameEvent, market: Market, context: dict) -> str:
        """Generate a narrative hook. Uses LLM if available, otherwise template."""
        if self._client:
            return await self._generate_llm(event, market, context)
        return self._generate_template(event, market, context)

    async def _generate_llm(self, event: GameEvent, market: Market, context: dict) -> str:
        """Call Claude API to generate narrative."""
        try:
            user_msg = f"""Event: {event.description}
Market: {market.label}
Current stats: {event.data}
Game clock: {context.get('clock', 'N/A')}
Score: {context.get('score', 'N/A')}

Write a 1-2 sentence narrative hook for this market."""

            response = self._client.messages.create(
                model="claude-3-5-haiku-latest",
                max_tokens=80,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}]
            )
            return response.content[0].text.strip()
        except Exception:
            return self._generate_template(event, market, context)

    def _generate_template(self, event: GameEvent, market: Market, context: dict) -> str:
        """Template-based fallback for narrative generation."""

        if event.event_type == EventType.THRESHOLD_APPROACH:
            current = event.data.get("current", 0)
            line = event.data.get("line", 0)
            remaining = line - current
            stat = event.data.get("stat_type", "stat")
            player = context.get("player_name", "Player")

            if remaining <= 0:
                return f"{player} has cleared the {market.label.split('O/U')[-1].strip() if 'O/U' in market.label else 'line'} — already at {current} {stat}"

            pct = event.data.get("percentage", 0)
            if pct >= 0.9:
                return f"{player} is {remaining:.0f} away from the Over — {current} {stat} and counting"
            return f"{player} at {current} {stat} tonight — closing in on the {line} line"

        elif event.event_type == EventType.SCORE_CHANGE:
            return f"Score update: {context.get('score', '')} — market shifting"

        elif event.event_type == EventType.MOMENTUM_SHIFT:
            run = event.data
            return (
                f"{run.get('team_short', 'Team')} on a {run.get('run_points', 0)}-{run.get('opponent_points', 0)} run "
                f"— line has flipped"
            )

        elif event.event_type == EventType.MILESTONE:
            remaining = event.data.get("remaining", 0)
            player = context.get("player_name", "Player")
            return f"{player} is {remaining} away from {event.data.get('milestone_name', 'the milestone')}"

        return event.description or "Market update"
