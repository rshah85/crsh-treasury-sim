# TODOS

## Kelly sizing ignores contrarian bet's own price impact

**What:** `_kelly_bet_size` (backend/sim_v2.py) computes `b` from the pre-bet pool
ratio and doesn't account for the contrarian bet's own price impact on an
already-skewed minority pool.

**Why:** Could overstate the true edge once real bet sizes are large relative to
real Monad pool depth. Untested against real pool depth data.

**Context:** Flagged by an outside-voice cross-model review during `/plan-eng-review`
of the live trading agent design (`~/.gstack/projects/rshah85-crsh-treasury-sim/
rishishah-main-design-20260720-190815.md`). Not blocking v1 — the agent ships against
placeholder contract calls with a "working demo, PnL secondary" bar, so this doesn't
matter until real capital and real pool depth are in play.

**Depends on / blocked by:** Real contract ABI + testnet/mainnet pool depth data
(neither available yet — see the design doc's Open Questions and The Assignment).
