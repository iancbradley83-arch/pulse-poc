"""Game Simulator — steps through mock game events to demonstrate the live pipeline."""
from __future__ import annotations

import asyncio
import json
import copy
from pathlib import Path
from app.models.schemas import (
    Game, GameEvent, EventType, StatDisplay, ProgressDisplay, Tweet, BadgeType
)
from app.engine.event_detector import EventDetector
from app.engine.market_matcher import MarketMatcher
from app.engine.relevance_scorer import RelevanceScorer
from app.engine.narrative_generator import NarrativeGenerator
from app.engine.card_assembler import CardAssembler
from app.services.market_catalog import MarketCatalog
from app.services.feed_manager import FeedManager
from app.config import SIMULATOR_SPEED, SIMULATOR_EVENT_DELAY

DATA_DIR = Path(__file__).parent.parent / "data"


# Pre-scripted timeline for the LAL vs BOS game
LAL_BOS_TIMELINE = [
    {
        "clock": "Q1 8:00", "period": "Q1", "minutes_remaining": 40,
        "home_score": 12, "away_score": 10,
        "player_stats": [
            {"player_id": "lebron", "stats": {"points": 8, "threes": 1},
             "lines": {"points": 27.5, "threes": 2.5}}
        ],
        "desc": "LeBron 8 pts early in Q1"
    },
    {
        "clock": "Q2 6:30", "period": "Q2", "minutes_remaining": 30,
        "home_score": 38, "away_score": 35,
        "player_stats": [
            {"player_id": "lebron", "stats": {"points": 16, "threes": 2},
             "lines": {"points": 27.5, "threes": 2.5}}
        ],
        "desc": "LeBron heating up — 16 pts at the half"
    },
    {
        "clock": "Q3 10:00", "period": "Q3", "minutes_remaining": 22,
        "home_score": 56, "away_score": 54,
        "player_stats": [
            {"player_id": "lebron", "stats": {"points": 21, "threes": 3},
             "lines": {"points": 27.5, "threes": 2.5}}
        ],
        "milestones": [
            {"player_id": "lebron", "remaining": 12,
             "milestone_name": "the all-time NBA scoring record",
             "description": "LeBron 12 points from all-time scoring record"}
        ],
        "desc": "Q3 starts — LeBron at 21, milestone in reach"
    },
    {
        "clock": "Q3 8:22", "period": "Q3", "minutes_remaining": 20,
        "home_score": 66, "away_score": 62,
        "scoring_run": {
            "team_id": "lal", "team_short": "LAL",
            "run_points": 12, "opponent_points": 2,
            "duration": "3:15"
        },
        "player_stats": [
            {"player_id": "lebron", "stats": {"points": 26, "threes": 3},
             "lines": {"points": 27.5, "threes": 2.5}}
        ],
        "milestones": [
            {"player_id": "lebron", "remaining": 7,
             "milestone_name": "the all-time NBA scoring record",
             "description": "LeBron 7 points from all-time scoring record"}
        ],
        "desc": "Lakers on a 12-2 run, LeBron at 26"
    },
    {
        "clock": "Q3 4:10", "period": "Q3", "minutes_remaining": 16,
        "home_score": 78, "away_score": 74,
        "player_stats": [
            {"player_id": "lebron", "stats": {"points": 30, "threes": 4},
             "lines": {"points": 27.5, "threes": 2.5}}
        ],
        "milestones": [
            {"player_id": "lebron", "remaining": 3,
             "milestone_name": "the all-time NBA scoring record",
             "description": "LeBron 3 points from all-time scoring record"}
        ],
        "desc": "LeBron clears the Over at 30 — record within reach"
    },
    {
        "clock": "Q4 9:15", "period": "Q4", "minutes_remaining": 9,
        "home_score": 92, "away_score": 88,
        "player_stats": [
            {"player_id": "lebron", "stats": {"points": 35, "threes": 4},
             "lines": {"points": 27.5, "threes": 2.5}}
        ],
        "milestones": [
            {"player_id": "lebron", "remaining": 0,
             "milestone_name": "the all-time NBA scoring record",
             "description": "HISTORY! LeBron breaks the all-time scoring record!"}
        ],
        "desc": "RECORD BROKEN — LeBron is the all-time leading scorer"
    },
]


# Pre-scripted timeline for Arsenal vs Chelsea
ARS_CHE_TIMELINE = [
    {
        "clock": "15'", "period": "1H", "minutes_remaining": 75,
        "home_score": 0, "away_score": 0,
        "player_stats": [
            {"player_id": "saka", "stats": {"goals": 0, "shots": 1},
             "lines": {"goals": 0.5}}
        ],
        "desc": "Early stages — Arsenal pressing high"
    },
    {
        "clock": "32'", "period": "1H", "minutes_remaining": 58,
        "home_score": 1, "away_score": 0,
        "player_stats": [
            {"player_id": "saka", "stats": {"goals": 0, "shots": 2},
             "lines": {"goals": 0.5}}
        ],
        "desc": "GOAL! Arsenal take the lead"
    },
    {
        "clock": "45'", "period": "1H", "minutes_remaining": 45,
        "home_score": 1, "away_score": 1,
        "player_stats": [
            {"player_id": "palmer", "stats": {"goals": 1, "shots": 3},
             "lines": {"goals": 0.5}}
        ],
        "desc": "Chelsea equalise just before half-time"
    },
    {
        "clock": "58'", "period": "2H", "minutes_remaining": 32,
        "home_score": 1, "away_score": 1,
        "player_stats": [
            {"player_id": "saka", "stats": {"goals": 0, "shots": 4},
             "lines": {"goals": 0.5}}
        ],
        "scoring_run": {
            "team_id": "ars", "team_short": "ARS",
            "run_points": 0, "opponent_points": 0,
            "duration": ""
        },
        "desc": "Arsenal dominating possession, looking for the winner"
    },
    {
        "clock": "62'", "period": "2H", "minutes_remaining": 28,
        "home_score": 2, "away_score": 1,
        "player_stats": [
            {"player_id": "saka", "stats": {"goals": 1, "shots": 5},
             "lines": {"goals": 0.5}}
        ],
        "desc": "GOAL! Saka makes it 2-1 — first goal back from injury!"
    },
    {
        "clock": "78'", "period": "2H", "minutes_remaining": 12,
        "home_score": 2, "away_score": 1,
        "player_stats": [
            {"player_id": "saka", "stats": {"goals": 1, "shots": 5},
             "lines": {"goals": 0.5}}
        ],
        "desc": "Arsenal holding firm — Chelsea running out of time"
    },
    {
        "clock": "90+2'", "period": "2H", "minutes_remaining": 0,
        "home_score": 3, "away_score": 1,
        "player_stats": [
            {"player_id": "saka", "stats": {"goals": 2, "shots": 7},
             "lines": {"goals": 0.5}}
        ],
        "desc": "GOAL! Saka doubles up in stoppage time — Arsenal cruise"
    },
]


# Pre-scripted timeline for Chiefs vs Eagles
KC_PHI_TIMELINE = [
    {
        "clock": "Q1 5:30", "period": "Q1", "minutes_remaining": 53,
        "home_score": 7, "away_score": 0,
        "player_stats": [
            {"player_id": "mahomes", "stats": {"touchdowns": 1, "pass_yards": 85},
             "lines": {"touchdowns": 2.5, "pass_yards": 275.5}}
        ],
        "desc": "Mahomes opens with a TD pass — looks healthy"
    },
    {
        "clock": "Q1 1:20", "period": "Q1", "minutes_remaining": 47,
        "home_score": 7, "away_score": 7,
        "player_stats": [
            {"player_id": "hurts", "stats": {"touchdowns": 1, "pass_yards": 62},
             "lines": {}}
        ],
        "desc": "Eagles respond — Hurts rushing TD ties it"
    },
    {
        "clock": "Q2 8:45", "period": "Q2", "minutes_remaining": 39,
        "home_score": 14, "away_score": 7,
        "player_stats": [
            {"player_id": "mahomes", "stats": {"touchdowns": 2, "pass_yards": 165},
             "lines": {"touchdowns": 2.5, "pass_yards": 275.5}}
        ],
        "desc": "Mahomes TD #2 to Kelce — vintage connection"
    },
    {
        "clock": "Q2 4:10", "period": "Q2", "minutes_remaining": 34,
        "home_score": 14, "away_score": 14,
        "player_stats": [
            {"player_id": "mahomes", "stats": {"touchdowns": 2, "pass_yards": 178},
             "lines": {"touchdowns": 2.5, "pass_yards": 275.5}}
        ],
        "desc": "Tied up at 14 — back and forth"
    },
    {
        "clock": "Q3 6:00", "period": "Q3", "minutes_remaining": 18,
        "home_score": 21, "away_score": 17,
        "player_stats": [
            {"player_id": "mahomes", "stats": {"touchdowns": 3, "pass_yards": 248},
             "lines": {"touchdowns": 2.5, "pass_yards": 275.5}}
        ],
        "desc": "Mahomes clears the TD Over — 3 TDs and counting"
    },
    {
        "clock": "Q4 8:30", "period": "Q4", "minutes_remaining": 8,
        "home_score": 24, "away_score": 20,
        "player_stats": [
            {"player_id": "mahomes", "stats": {"touchdowns": 3, "pass_yards": 310},
             "lines": {"touchdowns": 2.5, "pass_yards": 275.5}}
        ],
        "scoring_run": {
            "team_id": "kc", "team_short": "KC",
            "run_points": 10, "opponent_points": 3,
            "duration": "8:00"
        },
        "desc": "Chiefs pulling ahead late — Mahomes over 300 yards"
    },
    {
        "clock": "Q4 0:45", "period": "Q4", "minutes_remaining": 1,
        "home_score": 27, "away_score": 20,
        "player_stats": [
            {"player_id": "mahomes", "stats": {"touchdowns": 4, "pass_yards": 342},
             "lines": {"touchdowns": 2.5, "pass_yards": 275.5}}
        ],
        "desc": "Mahomes seals it — 4 TDs in his return game"
    },
]


class GameSimulator:
    def __init__(
        self,
        catalog: MarketCatalog,
        feed: FeedManager,
        detector: EventDetector,
        matcher: MarketMatcher,
        scorer: RelevanceScorer,
        narrator: NarrativeGenerator,
        assembler: CardAssembler,
    ):
        self.catalog = catalog
        self.feed = feed
        self.detector = detector
        self.matcher = matcher
        self.scorer = scorer
        self.narrator = narrator
        self.assembler = assembler
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._games: dict[str, Game] = {}
        self._players: dict = {}
        self._tweets: list[Tweet] = []
        self._load_data()

    def _load_data(self):
        games_raw = json.loads((DATA_DIR / "mock_games.json").read_text())
        for g in games_raw:
            game = Game(**g)
            self._games[game.id] = game

        players_raw = json.loads((DATA_DIR / "mock_players.json").read_text())
        self._players = {p["id"]: p for p in players_raw}

        tweets_raw = json.loads((DATA_DIR / "mock_tweets.json").read_text())
        self._tweets = [Tweet(**t) for t in tweets_raw]

    def _get_tweets_for(self, player_id: str | None = None, team_id: str | None = None, game_id: str | None = None) -> list[Tweet]:
        result = []
        for tw in self._tweets:
            if player_id and player_id in tw.player_ids:
                result.append(tw)
            elif team_id and team_id in tw.team_ids:
                result.append(tw)
            elif game_id and tw.game_id == game_id:
                result.append(tw)
        return result[:2]  # max 2 tweets per card

    async def start(self):
        if self._running:
            return
        self._running = True
        # Run all 3 games concurrently with slight offsets so cards interleave
        self._tasks = [
            asyncio.create_task(self._run_game("game_lal_bos", LAL_BOS_TIMELINE, offset=0)),
            asyncio.create_task(self._run_game("game_ars_che", ARS_CHE_TIMELINE, offset=4)),
            asyncio.create_task(self._run_game("game_kc_phi", KC_PHI_TIMELINE, offset=8)),
        ]

    async def stop(self):
        self._running = False
        for task in self._tasks:
            task.cancel()
        self._tasks = []

    @property
    def is_running(self) -> bool:
        return self._running

    async def _run_game(self, game_id: str, timeline: list, offset: float = 0):
        """Step through a scripted timeline for any game."""
        # Stagger game starts so cards don't all arrive simultaneously
        if offset > 0:
            await asyncio.sleep(offset)

        game = self._games[game_id]
        game.status = "live"
        prev_state = None

        for step in timeline:
            if not self._running:
                break

            # Update game state
            game.home_score = step["home_score"]
            game.away_score = step["away_score"]
            game.clock = step["clock"]
            game.period = step["period"]

            # Build state dict for the engine
            state = {
                **step,
                "home_team_id": game.home_team.id,
                "away_team_id": game.away_team.id,
            }

            # Broadcast game state update
            await self.feed.broadcast_game_update(game.model_dump())

            # Detect events
            events = self.detector.detect(game.id, state, prev_state)

            # Deduplicate — only keep the highest-priority event per entity
            # to avoid flooding (e.g., threshold + milestone for the same player)
            seen_entities = set()
            filtered_events = []
            # Prioritise milestones and momentum over threshold
            priority = {
                EventType.MILESTONE: 0,
                EventType.MOMENTUM_SHIFT: 1,
                EventType.SCORE_CHANGE: 2,
                EventType.THRESHOLD_APPROACH: 3,
                EventType.STAT_UPDATE: 4,
            }
            events.sort(key=lambda e: priority.get(e.event_type, 5))
            for event in events:
                entity_key = event.player_id or event.team_id or event.game_id
                if entity_key not in seen_entities:
                    filtered_events.append(event)
                    seen_entities.add(entity_key)

            for i, event in enumerate(filtered_events):
                if not self._running:
                    break

                # Match to markets
                markets = self.matcher.match(event)
                if not markets:
                    continue

                market = markets[0]  # primary market

                # Get relevant tweets
                tweets = self._get_tweets_for(
                    player_id=event.player_id,
                    team_id=event.team_id,
                    game_id=game.id,
                )

                # Score relevance
                relevance = self.scorer.score(
                    event, market, state, tweet_count=len(tweets)
                )

                # Build context for narrative
                player_name = ""
                if event.player_id and event.player_id in self._players:
                    player_name = self._players[event.player_id]["name"]

                context = {
                    "player_name": player_name,
                    "clock": game.clock,
                    "score": f"{game.home_team.short_name} {game.home_score} - {game.away_score} {game.away_team.short_name}",
                }

                # Generate narrative
                narrative = await self.narrator.generate(event, market, context)

                # Build stats display
                stats = []
                for ps in step.get("player_stats", []):
                    if ps["player_id"] == event.player_id:
                        for stat_key, stat_val in ps["stats"].items():
                            stats.append(StatDisplay(
                                label=stat_key.replace("_", " ").title(),
                                value=str(stat_val),
                                color="green" if ps["lines"].get(stat_key) and stat_val >= ps["lines"][stat_key] * 0.9 else None,
                            ))

                # Build progress display
                progress = None
                if event.event_type == EventType.THRESHOLD_APPROACH:
                    progress = ProgressDisplay(
                        label=f"{event.data.get('stat_type', 'stat').title()} Tonight",
                        current=event.data.get("current", 0),
                        target=event.data.get("line", 0),
                        fill_color="green" if event.data.get("percentage", 0) >= 0.9 else "accent",
                    )

                # Assemble card
                card = self.assembler.assemble_live(
                    event=event,
                    game=game,
                    market=market,
                    narrative=narrative,
                    stats=stats,
                    tweets=tweets,
                    relevance=relevance,
                    progress=progress,
                )

                # Add to feed and broadcast
                self.feed.add_live_card(card)
                await self.feed.broadcast_card(card)

                # Stagger multiple cards within the same game tick
                if i < len(filtered_events) - 1:
                    await asyncio.sleep(SIMULATOR_EVENT_DELAY)

            prev_state = copy.deepcopy(state)

            # Wait between major game moments
            await asyncio.sleep(SIMULATOR_SPEED)

        # Mark game as finished
        game.status = "finished"
        await self.feed.broadcast_game_update(game.model_dump())

        # Only mark simulator as stopped when ALL games are done
        all_done = all(
            g.status == "finished" for g in self._games.values()
            if g.status in ("live", "finished")
        )
        if all_done:
            self._running = False
