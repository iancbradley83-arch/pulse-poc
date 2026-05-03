"""Signal vocabulary — the language the narrative composer speaks.

A "signal" is a single named claim about expected match state. Markets
emit signals when picked in a direction (e.g. Total Goals OVER emits
``goals.high``). Theses emit signals when archetypes fire (e.g.
``KEY_ATTACKER_OUT`` on the away side emits ``team.{home}.dominance``,
``goals.low``, ``btts.no_likely``). Combinations of legs are scored by
how well their emitted signals overlap with the thesis signals.

The vocabulary will grow over time. Keep adding signals as we hit
narratives the existing set can't express. Do NOT collapse signals to
make composition easier — composition with a richer vocabulary is the
product win.

## Naming convention

  scope.attribute[.modifier][.{entity}]

  * `goals.high`
  * `defense.tight.{team}`
  * `tempo.first_half.high`
  * `player.{p}.discipline_pressure`

The `{team}` and `{p}` placeholders are filled at thesis-build time
with the actual team_id or player_id. Composition uses string equality
on the resolved signal — `defense.tight.<team_a_id>` will match
another leg emitting the same resolved string but won't match
`defense.tight.<team_b_id>`. This is what enforces "every leg must
connect to subject" rules without special-case logic.
"""
from __future__ import annotations

# ── Match-level ────────────────────────────────────────────────────────

MATCH_SIGNALS = {
    "goals.high",
    "goals.low",
    "goals.balanced",
    "tempo.high",
    "tempo.low",
    "tempo.first_half.high",
    "tempo.first_half.low",
    "tempo.second_half.high",
    "set_pieces.heavy",
    "set_pieces.light",
    "discipline.heavy",
    "discipline.light",
    "discipline.heavy.first_half",
    "physicality.high",
    "physicality.low",
    "btts.likely.yes",
    "btts.likely.no",
    "fast_start",
    "cagey_opener",
    "late_drama",
    "end_to_end",
    "derby_intensity",
    "style_clash",
    "controlled_match",
    "one_sided_dominance",
    "dominance.balanced",
}


# ── Per-team (`{team}` placeholder filled with team_id) ────────────────

PER_TEAM_TEMPLATES = {
    "dominance.{team}",
    "defense.tight.{team}",
    "defense.leaky.{team}",
    "defense.weakened.{team}",
    "team.{team}.attack.live",
    "team.{team}.attack.weakened",
    "team.{team}.high_press",
    "team.{team}.low_block",
    "team.{team}.must_win_pressure",
    "clean_sheet.{team}",
    "comeback.likely.{team}",
    "team.{team}.scores_first.likely",
    "team.{team}.fast_start",
}


# ── Per-player (`{p}` placeholder filled with player_id) ───────────────

PER_PLAYER_TEMPLATES = {
    "player.{p}.active",
    "player.{p}.suppressed",
    "player.{p}.attacking_role",
    "player.{p}.creative_role",
    "player.{p}.defensive_role",
    "player.{p}.discipline_pressure",
    "player.{p}.discipline_smart_play",
    "player.{p}.targeted_by_opp",
    "player.{p}.starting_confirmed",
    "player.{p}.returning_from_layoff",
    "player.{p}.set_piece_specialist",
    "player.{p}.in_form",
    "player.{p}.out",  # absent — used to remove player markets from pool
}


# ── Per-manager (`{team}` placeholder filled with team_id) ─────────────

PER_MANAGER_TEMPLATES = {
    "manager.{team}.under_pressure",
    "manager.{team}.tactical_change",
    "manager.{team}.mind_game_sent",
    "manager.{team}.mind_game_received",
}


# ── Helpers ────────────────────────────────────────────────────────────


def resolve(template: str, *, team_id: str | None = None,
            player_id: str | None = None) -> str:
    """Fill `{team}` / `{p}` placeholders in a signal template.

    Returns the template unchanged when no placeholder is present.
    Raises `ValueError` when a placeholder is present but the matching
    entity id is `None` — silent fallback would produce signals that
    look correct but never match (`defense.tight.{team}` vs
    `defense.tight.None`).
    """
    out = template
    if "{team}" in out:
        if team_id is None:
            raise ValueError(
                f"signal {template!r} requires team_id but got None"
            )
        out = out.replace("{team}", str(team_id))
    if "{p}" in out:
        if player_id is None:
            raise ValueError(
                f"signal {template!r} requires player_id but got None"
            )
        out = out.replace("{p}", str(player_id))
    return out


def is_known_template(template: str) -> bool:
    """True iff `template` matches a registered template/static signal."""
    if template in MATCH_SIGNALS:
        return True
    return (
        template in PER_TEAM_TEMPLATES
        or template in PER_PLAYER_TEMPLATES
        or template in PER_MANAGER_TEMPLATES
    )


def conflicts(sig_a: str, sig_b: str) -> bool:
    """Return True if two signals directly contradict.

    Conservative — only catches obvious antonyms. The composer also
    weights orthogonal signals down; this function is for hard rejects.
    """
    pairs = [
        ("goals.high", "goals.low"),
        ("tempo.high", "tempo.low"),
        ("tempo.first_half.high", "tempo.first_half.low"),
        ("set_pieces.heavy", "set_pieces.light"),
        ("discipline.heavy", "discipline.light"),
        ("physicality.high", "physicality.low"),
        ("btts.likely.yes", "btts.likely.no"),
        ("fast_start", "cagey_opener"),
    ]
    for a, b in pairs:
        if (sig_a == a and sig_b == b) or (sig_a == b and sig_b == a):
            return True
    # Per-team antonyms
    if sig_a.startswith("defense.tight.") and sig_b.startswith("defense.leaky."):
        if sig_a.split(".", 2)[-1] == sig_b.split(".", 2)[-1]:
            return True
    if sig_a.startswith("defense.leaky.") and sig_b.startswith("defense.tight."):
        if sig_a.split(".", 2)[-1] == sig_b.split(".", 2)[-1]:
            return True
    if sig_a.startswith("player.") and sig_a.endswith(".active") and \
       sig_b.startswith("player.") and sig_b.endswith(".suppressed"):
        if sig_a.split(".")[1] == sig_b.split(".")[1]:
            return True
    return False
