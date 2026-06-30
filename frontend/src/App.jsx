import { useState } from "react";
import {
  LineChart, Line, BarChart, Bar,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer, ReferenceLine,
} from "recharts";
import "./App.css";

const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

const DEFAULT_CONFIG = {
  starting_capital: 100000,
  markets_per_epoch: 50,
  num_epochs: 20,
  rake_pct: 0.025,
  treasury_rake_share: 0.5,
  seed_per_side: 12.5,
  contrarian_aggressiveness: 1.0,
  fan_bias_mean: 0.75,
  fan_bias_std: 0.1,
  crowd_volume_mean: 1000,
  crowd_volume_std: 300,
  max_exposure_pct: 0.05,
  seed: 42,
};

const FIELDS = [
  { key: "starting_capital", label: "Starting Capital ($)", step: 1000 },
  { key: "markets_per_epoch", label: "Markets per Epoch", step: 1 },
  { key: "num_epochs", label: "Number of Epochs", step: 1 },
  { key: "rake_pct", label: "Rake % (e.g. 0.025 = 2.5%)", step: 0.001 },
  { key: "treasury_rake_share", label: "Treasury Share of Rake", step: 0.05 },
  { key: "seed_per_side", label: "Symmetric Seed / Side ($)", step: 0.5 },
  { key: "contrarian_aggressiveness", label: "Contrarian Aggressiveness", step: 0.1 },
  { key: "fan_bias_mean", label: "Fan Bias Mean (favorite skew)", step: 0.05 },
  { key: "fan_bias_std", label: "Fan Bias Std Dev", step: 0.01 },
  { key: "crowd_volume_mean", label: "Avg Crowd Volume / Market ($)", step: 50 },
  { key: "crowd_volume_std", label: "Crowd Volume Std Dev", step: 50 },
  { key: "max_exposure_pct", label: "Max Treasury Exposure % / Side", step: 0.01 },
  { key: "seed", label: "Random Seed", step: 1 },
];

function fmt(n, d = 0) {
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d });
}

function buildHistogram(values, bins = 24) {
  if (!values.length) return [];
  const min = Math.min(...values);
  const max = Math.max(...values);
  const width = (max - min) / bins || 1;
  const buckets = Array.from({ length: bins }, (_, i) => ({
    bucketStart: min + i * width,
    count: 0,
  }));
  values.forEach((v) => {
    let idx = Math.floor((v - min) / width);
    if (idx >= bins) idx = bins - 1;
    if (idx < 0) idx = 0;
    buckets[idx].count += 1;
  });
  return buckets.map((b) => ({
    label: fmt(b.bucketStart, 0),
    count: b.count,
  }));
}

function SummaryCard({ label, value }) {
  return (
    <div className="summary-card">
      <div className="summary-label">{label}</div>
      <div className="summary-value">{value}</div>
    </div>
  );
}

function App() {
  const [config, setConfig] = useState(DEFAULT_CONFIG);
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  const handleChange = (key, value) => {
    setConfig((c) => ({ ...c, [key]: value === "" ? "" : Number(value) }));
  };

  const runSimulation = async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await fetch(`${API_URL}/api/simulate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(config),
      });
      if (!resp.ok) throw new Error(`Server error: ${resp.status}`);
      const data = await resp.json();
      setResult(data);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  const navData = result
    ? result.nav_curve.map((nav, i) => ({ epoch: i, nav }))
    : [];

  const epochData = result
    ? result.epochs.map((e) => ({
        epoch: e.epoch,
        fee_income: e.fee_income,
        contrarian_edge: e.contrarian_edge,
      }))
    : [];

  const histogram = result ? buildHistogram(result.market_pnls) : [];

  return (
    <div className="app">
      <header>
        <div className="header-logo">
          <div className="header-logo-mark">
            <svg viewBox="0 0 14 14" xmlns="http://www.w3.org/2000/svg">
              <polyline points="1,10 4,5 7,8 10,3 13,6" />
            </svg>
          </div>
          <h1>CRSH · Treasury Sim</h1>
        </div>
        <span className="header-tag">Monte Carlo</span>
        <p className="subtitle">Pari-mutuel creator markets · community-owned contrarian treasury</p>
      </header>

      <div className="layout">
        <aside className="panel">
          <div className="panel-heading">Parameters</div>
          {FIELDS.map((f) => (
            <div className="field" key={f.key}>
              <label htmlFor={f.key}>{f.label}</label>
              <input
                id={f.key}
                type="number"
                step={f.step}
                value={config[f.key]}
                onChange={(e) => handleChange(f.key, e.target.value)}
              />
            </div>
          ))}
          <button className="run-btn" onClick={runSimulation} disabled={loading}>
            {loading ? "Running…" : "Run Simulation"}
          </button>
          {error && <p className="error">{error}</p>}
        </aside>

        <main className="content">
          {!result && !loading && (
            <p className="placeholder">Configure parameters and run the simulation.</p>
          )}

          {result && (
            <>
              <section className="summary-grid">
                <SummaryCard label="Final NAV" value={`$${fmt(result.summary.final_nav)}`} />
                <SummaryCard label="Total Return" value={`${fmt(result.summary.total_return_pct, 2)}%`} />
                <SummaryCard label="Total Fee Income" value={`$${fmt(result.summary.total_fee_income)}`} />
                <SummaryCard label="Total Contrarian Edge" value={`$${fmt(result.summary.total_contrarian_edge)}`} />
                <SummaryCard label="Markets Simulated" value={fmt(result.summary.num_markets_simulated)} />
                <SummaryCard label="% Markets Profitable" value={`${fmt(result.summary.pct_markets_profitable, 1)}%`} />
              </section>

              <section className="chart-card">
                <div className="chart-header">
                  <h3>Treasury NAV Over Time</h3>
                  <span className="chart-badge badge-em">NAV</span>
                </div>
                <ResponsiveContainer width="100%" height={280}>
                  <LineChart data={navData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                    <XAxis dataKey="epoch" tick={{ fill: "#94a3b8", fontSize: 13 }} label={{ value: "Epoch", position: "insideBottom", dy: 10, fill: "#94a3b8", fontSize: 13 }} />
                    <YAxis tick={{ fill: "#94a3b8", fontSize: 13 }} tickFormatter={(v) => `$${fmt(v / 1000)}k`} />
                    <Tooltip formatter={(v) => `$${fmt(v)}`} labelFormatter={(l) => `Epoch ${l}`} />
                    <Line type="monotone" dataKey="nav" stroke="#00ff88" strokeWidth={2.5} dot={false} />
                  </LineChart>
                </ResponsiveContainer>
              </section>

              <section className="chart-card">
                <div className="chart-header">
                  <h3>Per-Epoch: Fee Income vs. Contrarian Edge</h3>
                  <span className="chart-badge badge-amb">Breakdown</span>
                </div>
                <ResponsiveContainer width="100%" height={280}>
                  <BarChart data={epochData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                    <XAxis dataKey="epoch" tick={{ fill: "#94a3b8", fontSize: 13 }} label={{ value: "Epoch", position: "insideBottom", dy: 10, fill: "#94a3b8", fontSize: 13 }} />
                    <YAxis tick={{ fill: "#94a3b8", fontSize: 13 }} tickFormatter={(v) => `$${fmt(v)}`} />
                    <Tooltip formatter={(v) => `$${fmt(v)}`} />
                    <Legend />
                    <Bar dataKey="fee_income" stackId="a" fill="#00ff88" name="Fee Income" />
                    <Bar dataKey="contrarian_edge" stackId="a" fill="#007a42" name="Contrarian Edge" />
                  </BarChart>
                </ResponsiveContainer>
              </section>

              <section className="chart-card">
                <div className="chart-header">
                  <h3>Distribution of Individual Market P&amp;L</h3>
                  <span className="chart-badge badge-ind">Histogram</span>
                </div>
                <p className="hint">
                  Wide spread per market, but aggregate NAV grows steadily — variance
                  reduction through diversification across many independent markets.
                </p>
                <ResponsiveContainer width="100%" height={280}>
                  <BarChart data={histogram}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                    <XAxis dataKey="label" tick={{ fill: "#94a3b8", fontSize: 13 }} interval={2} angle={-40} textAnchor="end" height={60} />
                    <YAxis tick={{ fill: "#94a3b8", fontSize: 13 }} />
                    <Tooltip />
                    <ReferenceLine x={0} stroke="rgba(255,255,255,0.2)" />
                    <Bar dataKey="count" fill="#00cc6e" name="Market Count" />
                  </BarChart>
                </ResponsiveContainer>
              </section>
            </>
          )}
        </main>
      </div>
    </div>
  );
}

export default App;
