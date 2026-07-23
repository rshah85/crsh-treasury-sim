"""
Live rich terminal dashboard.

Observability only — this module reads Daemon's already-public state
(risk_guard, ledger, market_snapshots) once per refresh and renders it. It never
gates or alters control flow: decision.py and risk_guard.py behave identically
whether or not a dashboard is attached. Because rich's Live display takes over
terminal redraws, daemon.py redirects the structured JSON event log to a file
(logs/daemon_events.jsonl by default) whenever --dashboard is used, so ordinary
print() output can't tear the display (see daemon.py's `_run_live`).
"""

from __future__ import annotations

import asyncio
import time

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table


class Dashboard:
    def __init__(self, daemon, refresh_hz: float = 1.0):
        self._daemon = daemon
        self._interval = 1.0 / refresh_hz

    def _render(self) -> Group:
        daemon = self._daemon
        clock = daemon.risk_guard.launch_clock
        phase = clock.phase()

        markets_table = Table(title="Active Markets", expand=True)
        for col in ("Market", "YES pool", "NO pool", "Majority", "T-close (s)", "Status"):
            markets_table.add_column(col)

        for market_id, snap in sorted(daemon.market_snapshots.items()):
            total = snap["yes_pool"] + snap["no_pool"]
            majority_pct = (max(snap["yes_pool"], snap["no_pool"]) / total * 100) if total else 0.0
            seconds_remaining = snap["seconds_remaining"] - (time.time() - snap["updated_at"])
            if market_id in daemon.ledger.open_positions:
                status = "[bold green]POSITION OPEN[/bold green]"
            elif daemon.risk_guard.is_market_bet(market_id):
                status = "bet placed"
            else:
                status = "watching"
            markets_table.add_row(
                market_id,
                f"{snap['yes_pool']:.1f}",
                f"{snap['no_pool']:.1f}",
                f"{majority_pct:.1f}%",
                f"{max(seconds_remaining, 0.0):.1f}",
                status,
            )

        positions_table = Table(title="Open Positions", expand=True)
        for col in ("Market", "Side", "Amount (USDC)", "Opened"):
            positions_table.add_column(col)
        for pos in daemon.ledger.open_positions.values():
            positions_table.add_row(
                pos.market_id,
                pos.side,
                f"${pos.amount_usdc:,.2f}",
                time.strftime("%H:%M:%S", time.localtime(pos.opened_at)),
            )

        pnl_color = "green" if daemon.ledger.realized_pnl >= 0 else "red"
        kill_switch = "[bold red]ENGAGED[/bold red]" if daemon._kill_switch_active else "[green]clear[/green]"
        circuit = (
            "[bold red]TRIPPED[/bold red]"
            if daemon.risk_guard.circuit_breaker_tripped()
            else "[green]ok[/green]"
        )
        stats = (
            f"Phase: [bold]{phase}[/bold]   "
            f"NAV: [bold]${daemon.ledger.nav:,.2f}[/bold]   "
            f"Realized P&L: [bold {pnl_color}]${daemon.ledger.realized_pnl:,.2f}[/bold {pnl_color}]   "
            f"Positions closed: {daemon.ledger.closed_count}   "
            f"Kill switch: {kill_switch}   "
            f"Circuit breaker: {circuit}"
        )

        return Group(Panel(stats, title="CRSH Contrarian Agent", border_style="blue"), markets_table, positions_table)

    async def run(self) -> None:
        with Live(self._render(), refresh_per_second=4, screen=True) as live:
            while not self._daemon._stop_event.is_set():
                live.update(self._render())
                await asyncio.sleep(self._interval)
