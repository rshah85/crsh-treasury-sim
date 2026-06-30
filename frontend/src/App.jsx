import { useState } from "react";
import {
  LineChart, Line, BarChart, Bar,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  ResponsiveContainer, ReferenceLine,
} from "recharts";
import "./App.css";

const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

const DEFAULT_CONFIG = {
  starting_capital: 100000,
  num_days: 30,
  markets_per_day: 50,
  seed_per_side: 12.5,
  crowd_volume_mean: 100,
  crowd_volume_std: 25,
  fan_bias_mean: 0.55,
  fan_bias_std: 0.20,
  contrarian_threshold: 0.70,
  contrarian_activation_prob: 0.50,
  contrarian_pool_min: 50,
  contrarian_pool_max: 150,
  contrarian_win_prob_low: 0.30,
  contrarian_win_prob_high: 0.40,
  rake_pct: 0.025,
  seed: 42,
};

const FIELDS = [
  { key: "starting_capital",        label: "Starting Capital ($)",          step: 1000 },
  { key: "num_days",                label: "Days to Simulate",               step: 1 },
  { key: "markets_per_day",         label: "Markets per Day",                step: 1 },
  { key: "seed_per_side",           label: "Seed per Side ($)",              step: 0.5 },
  { key: "crowd_volume_mean",       label: "Avg Crowd Volume / Market ($)",  step: 10 },
  { key: "crowd_volume_std",        label: "Crowd Volume Std Dev",           step: 5 },
  { key: "fan_bias_mean",           label: "Fan Bias Mean",                  step: 0.01 },
  { key: "fan_bias_std",            label: "Fan Bias Std Dev",               step: 0.01 },
  { key: "contrarian_threshold",    label: "Contrarian Threshold (e.g. 0.70)", step: 0.01 },
  { key: "contrarian_activation_prob", label: "Contrarian Activation Prob", step: 0.05 },
  { key: "contrarian_pool_min",     label: "Contrarian Pool Min ($)",        step: 10 },
  { key: "contrarian_pool_max",     label: "Contrarian Pool Max ($)",        step: 10 },
  { key: "contrarian_win_prob_low", label: "Contrarian Win Prob (low)",      step: 0.01 },
  { key: "contrarian_win_prob_high",label: "Contrarian Win Prob (high)",     step: 0.01 },
  { key: "rake_pct",                label: "Rake % (e.g. 0.025 = 2.5%)",    step: 0.001 },
  { key: "seed",                    label: "Random Seed",                    step: 1 },
];

const LAYERS = [
  { key: "base",            label: "Base Seeding Only",         color: "#00ff88", desc: "$12.5 each side, fair-coin outcomes" },
  { key: "plus_contrarian", label: "+ Contrarian Bets",         color: "#60a5fa", desc: "+10% of pool on minority when activated" },
  { key: "plus_rake",       label: "+ Full 2.5% Rake",          color: "#f59e0b", desc: "All rake income flows to treasury" },
];

function fmt(n, d = 0) {
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d });
}

function buildHistogram(values, bins = 24) {
  if (!values.length) return [];
  const min = Math.min(...values);
  const max = Math.max(...values);
  const width = (max - min) / bins || 1;
  const buckets = Array.from({ length: bins }, (_, i) => ({ bucketStart: min + i * width, count: 0 }));
  values.forEach((v) => {
    let idx = Math.floor((v - min) / width);
    if (idx >= bins) idx = bins - 1;
    if (idx < 0) idx = 0;
    buckets[idx].count++;
  });
  return buckets.map((b) => ({ label: fmt(b.bucketStart, 1), count: b.count }));
}

function LayerCard({ layer, data }) {
  const profitable = data.profitable;
  return (
    <div className={`layer-card ${profitable ? "layer-profitable" : "layer-loss"}`}>
      <div className="layer-dot" style={{ background: layer.color }} />
      <div className="layer-body">
        <div className="layer-name">{layer.label}</div>
        <div className="layer-desc">{layer.desc}</div>
        <div className="layer-stats">
          <span className="layer-nav">${fmt(data.final_nav)}</span>
          <span className={`layer-return ${profitable ? "pos" : "neg"}`}>
            {data.total_return_pct >= 0 ? "+" : ""}{fmt(data.total_return_pct, 2)}%
          </span>
          <span className={`layer-badge ${profitable ? "badge-profit" : "badge-loss"}`}>
            {profitable ? "PROFITABLE" : "LOSS"}
          </span>
        </div>
      </div>
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
      setResult(await resp.json());
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  const navData = result
    ? result.nav_curves.base.map((v, i) => ({
        day: i,
        base: v,
        plus_contrarian: result.nav_curves.plus_contrarian[i],
        plus_rake: result.nav_curves.plus_rake[i],
      }))
    : [];

  const dailyData = result
    ? result.days.map((d) => ({
        day: d.day,
        base_pnl: d.base_pnl,
        contrarian_pnl: d.contrarian_pnl,
        rake_income: d.rake_income,
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
              {/* Layer profitability cards */}
              <section className="layers-section">
                <div className="section-label">Profitability by Layer</div>
                <div className="layers-grid">
                  {LAYERS.map((l) => (
                    <LayerCard key={l.key} layer={l} data={result.summary[l.key]} />
                  ))}
                </div>
                <div className="meta-row">
                  <span>{fmt(result.summary.num_markets)} markets simulated</span>
                  <span>{fmt(result.summary.pct_markets_profitable, 1)}% profitable per market</span>
                </div>
              </section>

              {/* NAV chart — all three layers */}
              <section className="chart-card">
                <div className="chart-header">
                  <h3>Treasury NAV — Three Layers</h3>
                  <span className="chart-badge badge-em">Daily</span>
                </div>
                <ResponsiveContainer width="100%" height={300}>
                  <LineChart data={navData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                    <XAxis dataKey="day" tick={{ fill: "#94a3b8", fontSize: 12 }} label={{ value: "Day", position: "insideBottom", dy: 10, fill: "#94a3b8", fontSize: 12 }} />
                    <YAxis tick={{ fill: "#94a3b8", fontSize: 12 }} tickFormatter={(v) => `$${fmt(v / 1000)}k`} />
                    <Tooltip formatter={(v) => `$${fmt(v)}`} labelFormatter={(l) => `Day ${l}`} />
                    <Legend />
                    <Line type="monotone" dataKey="base"            stroke="#00ff88" strokeWidth={2}   dot={false} name="Base Seeding" />
                    <Line type="monotone" dataKey="plus_contrarian" stroke="#60a5fa" strokeWidth={2}   dot={false} name="+Contrarian" />
                    <Line type="monotone" dataKey="plus_rake"       stroke="#f59e0b" strokeWidth={2.5} dot={false} name="+Rake" />
                  </LineChart>
                </ResponsiveContainer>
              </section>

              {/* Daily P&L breakdown */}
              <section className="chart-card">
                <div className="chart-header">
                  <h3>Daily P&amp;L Breakdown</h3>
                  <span className="chart-badge badge-amb">Stacked</span>
                </div>
                <ResponsiveContainer width="100%" height={260}>
                  <BarChart data={dailyData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                    <XAxis dataKey="day" tick={{ fill: "#94a3b8", fontSize: 12 }} label={{ value: "Day", position: "insideBottom", dy: 10, fill: "#94a3b8", fontSize: 12 }} />
                    <YAxis tick={{ fill: "#94a3b8", fontSize: 12 }} tickFormatter={(v) => `$${fmt(v)}`} />
                    <Tooltip formatter={(v) => `$${fmt(v, 2)}`} />
                    <ReferenceLine y={0} stroke="rgba(255,255,255,0.2)" />
                    <Legend />
                    <Bar dataKey="base_pnl"       stackId="a" fill="#00ff88" name="Base" />
                    <Bar dataKey="contrarian_pnl" stackId="a" fill="#60a5fa" name="Contrarian" />
                    <Bar dataKey="rake_income"    stackId="a" fill="#f59e0b" name="Rake" />
                  </BarChart>
                </ResponsiveContainer>
              </section>

              {/* Histogram */}
              <section className="chart-card">
                <div className="chart-header">
                  <h3>Distribution of Per-Market P&amp;L</h3>
                  <span className="chart-badge badge-ind">Histogram</span>
                </div>
                <p className="hint">
                  Wide spread per market, but aggregate NAV trends upward — variance
                  reduction through diversification across {fmt(result.summary.num_markets)} markets.
                </p>
                <ResponsiveContainer width="100%" height={240}>
                  <BarChart data={histogram}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                    <XAxis dataKey="label" tick={{ fill: "#94a3b8", fontSize: 12 }} interval={3} angle={-40} textAnchor="end" height={60} />
                    <YAxis tick={{ fill: "#94a3b8", fontSize: 12 }} />
                    <Tooltip />
                    <ReferenceLine x="0" stroke="rgba(255,255,255,0.2)" />
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
