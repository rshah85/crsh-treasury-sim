"""
Realized bet PnL — reuses the exact pari-mutuel payout math from sim_v2's
contrarian-bet block (backend/sim_v2.py:_simulate_day_v2, the `con_pnl_m` /
`c_payout` lines) applied to a single live bet once its market's true outcome is
known, instead of re-deriving new payout math for the live agent.

sim_v2 computes this inline, per-market, inside one big loop — there's no
standalone function to import, so this reproduces the same formula:
    c_dist   = c_total - (c_total * rake_pct)
    c_payout = c_dist * (c_bet / c_pool_<side>)   if that side wins
    pnl      = c_payout - c_bet                    if won, else -c_bet
generalized to whichever side the live bet landed on (sim_v2 only ever bets NO).
"""

from __future__ import annotations


def compute_bet_pnl(
    pool_yes_at_bet: float,
    pool_no_at_bet: float,
    side: str,
    amount_usdc: float,
    outcome_yes: bool,
    rake_pct: float,
) -> float:
    """PnL for one bet, using the pool snapshot captured AT BET TIME — a real
    pari-mutuel contract locks in your share of the pool the moment your bet
    lands, so later pool movement from other bettors doesn't change your payout
    ratio, only the total pool size does."""
    pool_yes = pool_yes_at_bet + (amount_usdc if side == "YES" else 0.0)
    pool_no = pool_no_at_bet + (amount_usdc if side == "NO" else 0.0)
    total_pool = pool_yes + pool_no
    rake = total_pool * rake_pct
    dist = total_pool - rake

    won = (side == "YES") == outcome_yes
    if not won:
        return -amount_usdc

    win_pool = pool_yes if side == "YES" else pool_no
    if win_pool <= 0:
        return -amount_usdc

    payout = dist * (amount_usdc / win_pool)
    return payout - amount_usdc
