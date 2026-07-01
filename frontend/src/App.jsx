import { useState, useCallback } from "react";
import {
  LineChart, Line, BarChart, Bar,
  XAxis, YAxis, CartesianGrid, Tooltip, Legend,
  ResponsiveContainer, ReferenceLine, Cell,
} from "recharts";
import "./App.css";

const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";

const PHASE_COLORS = { 1: "#10b981", 2: "#60a5fa", 3: "#a78bfa" };

const DEFAULT_CONFIG = {
  starting_capital: 100000,
  num_days: 30,
  markets_per_day: 50,
  seed_per_side: 12.5,
  crowd_volume_mean: 100,
  crowd_volume_std: 25,
  num_clusters: 5,
  fan_bias_mean: 0.55,
  cluster_bias_std: 0.15,
  market_bias_noise: 0.06,
  edge_discount: 0.25,
  contrarian_threshold: 0.70,
  contrarian_activation_prob: 0.50,
  contrarian_pool_min: 50,
  contrarian_pool_max: 150,
  max_contrarian_exposure_pct: 0.30,
  sophistication_decay: 0.02,
  dynamic_exposure: true,
  daily_capital_limit_pct: 0.20,
  rake_pct: 0.025,
  rake_volume_elasticity: 0.5,
  seed: 42,
  // V2-specific
  phase1_threshold: 0.65,
  phase2_threshold: 0.70,
  phase3_threshold: 0.75,
  phase1_kelly_scalar: 1.00,
  phase2_kelly_scalar: 0.70,
  phase3_kelly_scalar: 0.50,
  max_kelly_fraction: 0.25,
  carryover_cap_pct: 0.50,
  volume_cap: 5000000,
  harvest_trigger_pct: 0.80,
};

const FIELD_GROUPS = [
  {
    title: "Core",
    fields: [
      { key: "starting_capital",       label: "Starting Capital ($)",    step: 1000, type: "number" },
      { key: "num_days",               label: "Days to Simulate",        step: 1,    type: "number" },
      { key: "markets_per_day",        label: "Markets per Day",         step: 1,    type: "number" },
      { key: "seed_per_side",          label: "Seed per Side ($)",       step: 0.5,  type: "number" },
      { key: "crowd_volume_mean",      label: "Crowd Volume Mean ($)",   step: 10,   type: "number" },
      { key: "crowd_volume_std",       label: "Crowd Volume Std Dev",    step: 5,    type: "number" },
      { key: "seed",                   label: "Random Seed",             step: 1,    type: "number" },
    ],
  },
  {
    title: "Creator Clusters",
    fields: [
      { key: "num_clusters",      label: "Clusters per Day",          step: 1,    type: "number" },
      { key: "fan_bias_mean",     label: "Global Bias Mean",          step: 0.01, type: "number" },
      { key: "cluster_bias_std",  label: "Between-Cluster Spread",    step: 0.01, type: "number" },
      { key: "market_bias_noise", label: "Within-Cluster Noise",      step: 0.01, type: "number" },
    ],
  },
  {
    title: "Contrarian Edge",
    fields: [
      { key: "edge_discount",               label: "Edge Discount (0=crowd right, 1=crowd wrong)", step: 0.05, type: "number" },
      { key: "contrarian_threshold",        label: "Activation Threshold",                         step: 0.01, type: "number" },
      { key: "contrarian_activation_prob",  label: "Activation Probability",                       step: 0.05, type: "number" },
      { key: "contrarian_pool_min",         label: "Contrarian Pool Min ($)",                      step: 10,   type: "number" },
      { key: "contrarian_pool_max",         label: "Contrarian Pool Max ($)",                      step: 10,   type: "number" },
      { key: "max_contrarian_exposure_pct", label: "Max Exposure % of Pool",                       step: 0.01, type: "number" },
    ],
  },
  {
    title: "Risk Controls",
    fields: [
      { key: "sophistication_decay",    label: "Sophistication Decay / Day", step: 0.01,  type: "number" },
      { key: "dynamic_exposure",        label: "Dynamic Exposure Cap",                    type: "bool" },
      { key: "daily_capital_limit_pct", label: "Daily Capital Limit %",      step: 0.01,  type: "number" },
    ],
  },
  {
    title: "Rake",
    fields: [
      { key: "rake_pct",                label: "Rake % (e.g. 0.025)",     step: 0.001, type: "number" },
      { key: "rake_volume_elasticity",  label: "Volume Elasticity",        step: 0.05,  type: "number" },
    ],
  },
  {
    title: "V2 — Phase Schedule",
    fields: [
      { key: "phase1_threshold",    label: "Phase 1 Threshold (aggressive)", step: 0.01, type: "number" },
      { key: "phase1_kelly_scalar", label: "Phase 1 Kelly Scalar",           step: 0.05, type: "number" },
      { key: "phase2_threshold",    label: "Phase 2 Threshold",              step: 0.01, type: "number" },
      { key: "phase2_kelly_scalar", label: "Phase 2 Kelly Scalar",           step: 0.05, type: "number" },
      { key: "phase3_threshold",    label: "Phase 3 Threshold (selective)",  step: 0.01, type: "number" },
      { key: "phase3_kelly_scalar", label: "Phase 3 Kelly Scalar",           step: 0.05, type: "number" },
    ],
  },
  {
    title: "V2 — Capital Controls",
    fields: [
      { key: "max_kelly_fraction",   label: "Max Kelly Fraction (cap)",        step: 0.01,    type: "number" },
      { key: "carryover_cap_pct",    label: "Carryover Cap (% of daily limit)",step: 0.05,    type: "number" },
      { key: "volume_cap",           label: "Platform Volume Cap ($)",          step: 100000,  type: "number" },
      { key: "harvest_trigger_pct",  label: "Harvest Mode Trigger (%)",         step: 0.05,    type: "number" },
    ],
  },
];

const LAYERS = [
  { key: "base",            label: "Base Seeding Only",    color: "#00ff88", desc: "$12.5 each side, fair-coin outcomes" },
  { key: "plus_contrarian", label: "+ Contrarian Bets",    color: "#60a5fa", desc: "Convex allocation on extreme crowd splits" },
  { key: "plus_rake",       label: "+ Full 2.5% Rake",     color: "#f59e0b", desc: "All rake income flows to treasury" },
];

function fmt(n, d = 0) {
  return Number(n).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d });
}

function buildHistogram(values, bins = 24) {
  if (!values.length) return [];
  const min = Math.min(...values), max = Math.max(...values);
  const width = (max - min) / bins || 1;
  const buckets = Array.from({ length: bins }, (_, i) => ({ bucketStart: min + i * width, count: 0 }));
  values.forEach((v) => {
    let idx = Math.floor((v - min) / width);
    buckets[Math.min(Math.max(idx, 0), bins - 1)].count++;
  });
  return buckets.map((b) => ({ label: fmt(b.bucketStart, 1), count: b.count }));
}

// ── Sub-components ──────────────────────────────────────────────────────────

function LayerCard({ layer, data }) {
  const ok = data.profitable;
  return (
    <div className={`layer-card ${ok ? "layer-profitable" : "layer-loss"}`}>
      <div className="layer-dot" style={{ background: layer.color }} />
      <div className="layer-body">
        <div className="layer-name">{layer.label}</div>
        <div className="layer-desc">{layer.desc}</div>
        <div className="layer-stats">
          <span className="layer-nav">${fmt(data.final_nav)}</span>
          <span className={`layer-return ${ok ? "pos" : "neg"}`}>
            {data.total_return_pct >= 0 ? "+" : ""}{fmt(data.total_return_pct, 2)}%
          </span>
          <span className={`layer-badge ${ok ? "badge-profit" : "badge-loss"}`}>
            {ok ? "PROFITABLE" : "LOSS"}
          </span>
        </div>
      </div>
    </div>
  );
}

function VarianceCard({ label, color, tag, tagClass, mean, std, total }) {
  const cv = mean !== 0 ? Math.abs(std / mean) * 100 : 0;
  return (
    <div className="variance-card">
      <div className="variance-header">
        <div className="variance-dot" style={{ background: color }} />
        <span className="variance-label">{label}</span>
        <span className={`variance-tag ${tagClass}`}>{tag}</span>
      </div>
      <div className="variance-stats">
        <div className="variance-stat">
          <div className="variance-stat-label">Avg / Day</div>
          <div className="variance-stat-value" style={{ color }}>${fmt(mean, 1)}</div>
        </div>
        <div className="variance-stat">
          <div className="variance-stat-label">Std Dev / Day</div>
          <div className="variance-stat-value">±${fmt(std, 1)}</div>
        </div>
        <div className="variance-stat">
          <div className="variance-stat-label">Total</div>
          <div className="variance-stat-value">${fmt(total, 0)}</div>
        </div>
      </div>
      <div className="variance-bar-track">
        <div className="variance-bar-label">Volatility (CV = {fmt(cv, 0)}%)</div>
        <div className="variance-bar-bg">
          <div className="variance-bar-fill"
            style={{ width: `${Math.min(cv, 300) / 300 * 100}%`, background: color }} />
        </div>
      </div>
    </div>
  );
}

function ModelAssumptions({ config }) {
  // Derive implied win probs from current edge_discount
  const fanBias70 = 0.70;
  const fanBias90 = 0.90;
  const impliedWin70 = (1 - fanBias70) + config.edge_discount * (fanBias70 - (1 - fanBias70));
  const impliedWin90 = (1 - fanBias90) + config.edge_discount * (fanBias90 - (1 - fanBias90));
  const daysToZeroEdge = config.sophistication_decay > 0
    ? Math.ceil(1 / config.sophistication_decay) : "∞";
  const volFactor = config.rake_pct > 0
    ? Math.pow(0.025 / config.rake_pct, config.rake_volume_elasticity) : 2;
  const effVol = config.crowd_volume_mean * volFactor;

  const assumptions = [
    {
      label: "Minority win prob at 70/30 crowd",
      value: `${fmt(impliedWin70 * 100, 1)}%`,
      sub: `crowd-implied: 30.0% → treasury model: ${fmt(impliedWin70 * 100, 1)}%`,
      risk: impliedWin70 > 0.55 ? "aggressive" : impliedWin70 > 0.40 ? "moderate" : "conservative",
    },
    {
      label: "Minority win prob at 90/10 crowd",
      value: `${fmt(impliedWin90 * 100, 1)}%`,
      sub: `crowd-implied: 10.0% → treasury model: ${fmt(impliedWin90 * 100, 1)}%`,
      risk: impliedWin90 > 0.70 ? "aggressive" : impliedWin90 > 0.50 ? "moderate" : "conservative",
    },
    {
      label: "Days until edge decays to zero",
      value: `${daysToZeroEdge}`,
      sub: config.sophistication_decay === 0 ? "no decay — edge held forever" : `decay: ${fmt(config.sophistication_decay * 100, 0)}% of edge per day`,
      risk: config.sophistication_decay === 0 ? "aggressive" : config.sophistication_decay < 0.05 ? "moderate" : "conservative",
    },
    {
      label: "Effective crowd volume (after rake)",
      value: `$${fmt(effVol, 1)}`,
      sub: `rake elasticity ${config.rake_volume_elasticity} → ${fmt(volFactor * 100, 0)}% of base volume`,
      risk: volFactor > 0.9 ? "aggressive" : volFactor > 0.6 ? "moderate" : "conservative",
    },
    {
      label: "Cluster correlation",
      value: `${config.num_clusters} clusters`,
      sub: `cluster spread ±${fmt(config.cluster_bias_std * 100, 0)}% — ${config.num_clusters < 4 ? "high" : config.num_clusters < 8 ? "moderate" : "low"} intra-day correlation`,
      risk: config.num_clusters < 4 ? "aggressive" : config.num_clusters < 8 ? "moderate" : "conservative",
    },
    {
      label: "Max daily capital deployed",
      value: `${fmt(config.daily_capital_limit_pct * 100, 0)}% of NAV`,
      sub: config.dynamic_exposure ? "dynamic exposure cap enabled" : "static exposure cap",
      risk: config.daily_capital_limit_pct > 0.25 ? "aggressive" : config.daily_capital_limit_pct > 0.10 ? "moderate" : "conservative",
    },
  ];

  const riskColor = { aggressive: "#f87171", moderate: "#f59e0b", conservative: "#10b981" };

  return (
    <section className="assumptions-section">
      <div className="section-label">Model Assumptions</div>
      <div className="assumptions-grid">
        {assumptions.map((a) => (
          <div key={a.label} className="assumption-card">
            <div className="assumption-top">
              <span className="assumption-label">{a.label}</span>
              <span className="assumption-risk-dot" style={{ background: riskColor[a.risk] }} title={a.risk} />
            </div>
            <div className="assumption-value">{a.value}</div>
            <div className="assumption-sub">{a.sub}</div>
          </div>
        ))}
      </div>
      <div className="assumption-legend">
        <span style={{ color: "#10b981" }}>● Conservative</span>
        <span style={{ color: "#f59e0b" }}>● Moderate</span>
        <span style={{ color: "#f87171" }}>● Aggressive</span>
      </div>
    </section>
  );
}

function SensitivityTable({ data }) {
  const { edge_values, decay_values, grid } = data;
  const allVals = grid.flat();
  const minV = Math.min(...allVals), maxV = Math.max(...allVals);

  function cellColor(v) {
    const t = maxV === minV ? 0.5 : (v - minV) / (maxV - minV);
    const r = Math.round(248 - t * (248 - 16));
    const g = Math.round(113 + t * (185 - 113));
    const b = Math.round(113 - t * (113 - 129));
    return `rgb(${r},${g},${b})`;
  }

  return (
    <section className="sensitivity-section">
      <div className="section-label">Sensitivity — Total Return (%) vs Edge Discount × Sophistication Decay</div>
      <p className="hint">
        Row = edge_discount (how wrong the crowd is assumed to be). Column = sophistication_decay (how fast that edge erodes).
        Green = higher return. The two bottom-right risks: treasury assumes crowd is wrong but crowds learn fast.
      </p>
      <div className="sensitivity-wrap">
        <table className="sensitivity-table">
          <thead>
            <tr>
              <th className="sens-corner">edge \ decay</th>
              {decay_values.map((d) => (
                <th key={d}>{fmt(d * 100, 0)}%/day</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {grid.map((row, ri) => (
              <tr key={ri}>
                <td className="sens-row-label">{fmt(edge_values[ri] * 100, 0)}%</td>
                {row.map((v, ci) => (
                  <td key={ci} className="sens-cell" style={{ background: cellColor(v), color: "#080c14" }}>
                    {v >= 0 ? "+" : ""}{fmt(v, 1)}%
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// ── Main App ────────────────────────────────────────────────────────────────

export default function App() {
  const [config, setConfig] = useState(DEFAULT_CONFIG);
  const [result, setResult] = useState(null);
  const [sensitivity, setSensitivity] = useState(null);
  const [bothResult, setBothResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [sensLoading, setSensLoading] = useState(false);
  const [bothLoading, setBothLoading] = useState(false);
  const [error, setError] = useState(null);

  const handleChange = (key, value, type) => {
    if (type === "bool") {
      setConfig((c) => ({ ...c, [key]: !c[key] }));
    } else {
      setConfig((c) => ({ ...c, [key]: value === "" ? "" : Number(value) }));
    }
  };

  const runBoth = useCallback(async () => {
    setBothLoading(true); setError(null);
    try {
      const resp = await fetch(`${API_URL}/api/simulate/both`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(config),
      });
      if (!resp.ok) throw new Error(`Server error: ${resp.status}`);
      setBothResult(await resp.json());
    } catch (e) { setError(e.message); }
    finally { setBothLoading(false); }
  }, [config]);

  const runSim = useCallback(async () => {
    setLoading(true); setError(null); setSensitivity(null);
    try {
      const resp = await fetch(`${API_URL}/api/simulate`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(config),
      });
      if (!resp.ok) throw new Error(`Server error: ${resp.status}`);
      setResult(await resp.json());
    } catch (e) { setError(e.message); }
    finally { setLoading(false); }
  }, [config]);

  const runSensitivity = useCallback(async () => {
    setSensLoading(true);
    try {
      const resp = await fetch(`${API_URL}/api/sensitivity`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(config),
      });
      if (!resp.ok) throw new Error(`Sensitivity error: ${resp.status}`);
      setSensitivity(await resp.json());
    } catch (e) { setError(e.message); }
    finally { setSensLoading(false); }
  }, [config]);

  const navData = result?.nav_curves.base.map((v, i) => ({
    day: i,
    base:            v,
    plus_contrarian: result.nav_curves.plus_contrarian[i],
    plus_rake:       result.nav_curves.plus_rake[i],
    rake_floor:      result.nav_curves.rake_floor[i],
  })) ?? [];

  const dailyData = result?.days.map((d) => ({
    day:             d.day,
    base_pnl:        d.base_pnl,
    contrarian_pnl:  d.contrarian_pnl,
    rake_income:     d.rake_income,
  })) ?? [];

  const edgeDecayData = result?.days.map((d) => ({
    day:  d.day,
    edge: +(d.effective_edge * 100).toFixed(2),
  })) ?? [];

  const histogram = result ? buildHistogram(result.market_pnls) : [];

  // V1 vs V2 comparison data
  const compNavData = bothResult
    ? bothResult.v1.nav_curves.plus_rake.map((v1val, i) => ({
        day: i,
        v1_rake:       v1val,
        v2_rake:       bothResult.v2.nav_curves.plus_rake[i],
        v1_floor:      bothResult.v1.nav_curves.rake_floor[i],
        v2_floor:      bothResult.v2.nav_curves.rake_floor[i],
      }))
    : [];

  const utilizationData = bothResult
    ? bothResult.v2.days.map((d) => ({
        day:        d.day,
        utilization: d.utilization_pct,
        carryover:  d.carryover,
        phase:      d.phase,
        budget:     d.capital_budget,
        deployed:   d.capital_deployed,
        harvest:    d.harvest_mode,
      }))
    : [];

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
        <span className="header-tag">Monte Carlo v3</span>
        <p className="subtitle">Pari-mutuel creator markets · community-owned contrarian treasury</p>
      </header>

      <div className="layout">
        {/* ── Sidebar ── */}
        <aside className="panel">
          {FIELD_GROUPS.map((group) => (
            <div key={group.title} className="param-group">
              <div className="panel-heading">{group.title}</div>
              {group.fields.map((f) => (
                <div className="field" key={f.key}>
                  <label htmlFor={f.key}>{f.label}</label>
                  {f.type === "bool" ? (
                    <label className="toggle">
                      <input
                        id={f.key} type="checkbox"
                        checked={!!config[f.key]}
                        onChange={() => handleChange(f.key, null, "bool")}
                      />
                      <span className="toggle-track">
                        <span className="toggle-thumb" />
                      </span>
                      <span className="toggle-label">{config[f.key] ? "ON" : "OFF"}</span>
                    </label>
                  ) : (
                    <input
                      id={f.key} type="number" step={f.step}
                      value={config[f.key]}
                      onChange={(e) => handleChange(f.key, e.target.value, "number")}
                    />
                  )}
                </div>
              ))}
            </div>
          ))}

          <div className="sidebar-actions">
            <button className="run-btn" onClick={runSim} disabled={loading}>
              {loading ? "Running…" : "Run Simulation (V1)"}
            </button>
            <button className="v2-btn" onClick={runBoth} disabled={bothLoading}>
              {bothLoading ? "Computing…" : "Run V1 vs V2"}
            </button>
            {result && (
              <button className="sens-btn" onClick={runSensitivity} disabled={sensLoading}>
                {sensLoading ? "Computing…" : "Run Sensitivity"}
              </button>
            )}
          </div>
          {error && <p className="error">{error}</p>}
        </aside>

        {/* ── Main content ── */}
        <main className="content">
          {!result && !loading && (
            <p className="placeholder">Configure parameters and run the simulation.</p>
          )}

          {result && (
            <>
              {/* Model Assumptions */}
              <ModelAssumptions config={config} />

              {/* Layer profitability */}
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

              {/* Variance breakdown */}
              <section className="layers-section">
                <div className="section-label">Stable Floor vs Volatile Overlay</div>
                <div className="variance-grid">
                  <VarianceCard label="Rake Income"      color="#f59e0b" tag="STABLE FLOOR"      tagClass="tag-stable"
                    mean={result.daily_stats.rake.mean}        std={result.daily_stats.rake.std}        total={result.daily_stats.rake.total} />
                  <VarianceCard label="Contrarian Bets"  color="#60a5fa" tag="HIGH VARIANCE"     tagClass="tag-volatile"
                    mean={result.daily_stats.contrarian.mean}  std={result.daily_stats.contrarian.std}  total={result.daily_stats.contrarian.total} />
                  <VarianceCard label="Base Seeding"     color="#00ff88" tag="MODERATE VARIANCE" tagClass="tag-moderate"
                    mean={result.daily_stats.base.mean}        std={result.daily_stats.base.std}        total={result.daily_stats.base.total} />
                </div>
              </section>

              {/* NAV chart */}
              <section className="chart-card">
                <div className="chart-header">
                  <h3>Treasury NAV — Three Layers + Rake Floor</h3>
                  <span className="chart-badge badge-em">Daily</span>
                </div>
                <ResponsiveContainer width="100%" height={280}>
                  <LineChart data={navData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                    <XAxis dataKey="day" tick={{ fill: "#94a3b8", fontSize: 12 }} label={{ value: "Day", position: "insideBottom", dy: 10, fill: "#94a3b8", fontSize: 12 }} />
                    <YAxis tick={{ fill: "#94a3b8", fontSize: 12 }} tickFormatter={(v) => `$${fmt(v / 1000)}k`} />
                    <Tooltip formatter={(v) => `$${fmt(v)}`} labelFormatter={(l) => `Day ${l}`} />
                    <Legend />
                    <Line type="monotone" dataKey="rake_floor"       stroke="#f59e0b" strokeWidth={1.5} strokeDasharray="5 3" dot={false} name="Rake Floor" />
                    <Line type="monotone" dataKey="base"             stroke="#00ff88" strokeWidth={2}   dot={false} name="Base" />
                    <Line type="monotone" dataKey="plus_contrarian"  stroke="#60a5fa" strokeWidth={2}   dot={false} name="+Contrarian" />
                    <Line type="monotone" dataKey="plus_rake"        stroke="#f59e0b" strokeWidth={2.5} dot={false} name="+Rake" />
                  </LineChart>
                </ResponsiveContainer>
              </section>

              {/* Edge decay chart */}
              {config.sophistication_decay > 0 && (
                <section className="chart-card">
                  <div className="chart-header">
                    <h3>Effective Edge Over Time (Sophistication Decay)</h3>
                    <span className="chart-badge badge-ind">Decay</span>
                  </div>
                  <ResponsiveContainer width="100%" height={180}>
                    <LineChart data={edgeDecayData}>
                      <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                      <XAxis dataKey="day" tick={{ fill: "#94a3b8", fontSize: 12 }} />
                      <YAxis tick={{ fill: "#94a3b8", fontSize: 12 }} tickFormatter={(v) => `${v}%`} />
                      <Tooltip formatter={(v) => `${v}%`} labelFormatter={(l) => `Day ${l}`} />
                      <Line type="monotone" dataKey="edge" stroke="#a78bfa" strokeWidth={2} dot={false} name="Effective Edge %" />
                    </LineChart>
                  </ResponsiveContainer>
                </section>
              )}

              {/* Daily P&L */}
              <section className="chart-card">
                <div className="chart-header">
                  <h3>Daily P&amp;L Breakdown</h3>
                  <span className="chart-badge badge-amb">Stacked</span>
                </div>
                <ResponsiveContainer width="100%" height={240}>
                  <BarChart data={dailyData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                    <XAxis dataKey="day" tick={{ fill: "#94a3b8", fontSize: 12 }} />
                    <YAxis tick={{ fill: "#94a3b8", fontSize: 12 }} tickFormatter={(v) => `$${fmt(v)}`} />
                    <Tooltip formatter={(v) => `$${fmt(v, 2)}`} />
                    <ReferenceLine y={0} stroke="rgba(255,255,255,0.2)" />
                    <Legend />
                    <Bar dataKey="rake_income"    stackId="a" fill="#f59e0b" name="Rake" />
                    <Bar dataKey="base_pnl"       stackId="a" fill="#00ff88" name="Base" />
                    <Bar dataKey="contrarian_pnl" stackId="a" fill="#60a5fa" name="Contrarian" />
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
                  Wide per-market spread; aggregate NAV trends up via diversification across {fmt(result.summary.num_markets)} markets.
                </p>
                <ResponsiveContainer width="100%" height={220}>
                  <BarChart data={histogram}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                    <XAxis dataKey="label" tick={{ fill: "#94a3b8", fontSize: 12 }} interval={3} angle={-40} textAnchor="end" height={60} />
                    <YAxis tick={{ fill: "#94a3b8", fontSize: 12 }} />
                    <Tooltip />
                    <ReferenceLine x="0" stroke="rgba(255,255,255,0.2)" />
                    <Bar dataKey="count" fill="#00cc6e" name="Count" />
                  </BarChart>
                </ResponsiveContainer>
              </section>

              {/* Sensitivity table */}
              {sensitivity && <SensitivityTable data={sensitivity} />}
            </>
          )}

          {/* ── V1 vs V2 comparison (shown independently of v1 single run) ── */}
          {bothResult && (
            <>
              {/* Summary comparison cards */}
              <section className="layers-section" style={{ marginTop: result ? 0 : 0 }}>
                <div className="section-label">V1 vs V2 — Final NAV Comparison</div>
                <div className="v2-compare-grid">
                  {["base", "plus_contrarian", "plus_rake"].map((key) => {
                    const v1 = bothResult.v1.summary[key];
                    const v2 = bothResult.v2.summary[key];
                    const delta = v2.total_return_pct - v1.total_return_pct;
                    return (
                      <div key={key} className="v2-compare-card">
                        <div className="v2-compare-label">
                          {key === "base" ? "Base Only" : key === "plus_contrarian" ? "+Contrarian" : "+Rake (Full)"}
                        </div>
                        <div className="v2-compare-row">
                          <div className="v2-compare-col">
                            <span className="v2-engine-tag v2-tag-v1">V1</span>
                            <div className="v2-nav">${fmt(v1.final_nav)}</div>
                            <div className={`v2-ret ${v1.profitable ? "pos" : "neg"}`}>
                              {v1.total_return_pct >= 0 ? "+" : ""}{fmt(v1.total_return_pct, 2)}%
                            </div>
                          </div>
                          <div className="v2-compare-arrow">→</div>
                          <div className="v2-compare-col">
                            <span className="v2-engine-tag v2-tag-v2">V2</span>
                            <div className="v2-nav">${fmt(v2.final_nav)}</div>
                            <div className={`v2-ret ${v2.profitable ? "pos" : "neg"}`}>
                              {v2.total_return_pct >= 0 ? "+" : ""}{fmt(v2.total_return_pct, 2)}%
                            </div>
                          </div>
                        </div>
                        <div className={`v2-delta ${delta >= 0 ? "pos" : "neg"}`}>
                          V2 {delta >= 0 ? "+" : ""}{fmt(delta, 2)}pp vs V1
                        </div>
                      </div>
                    );
                  })}
                </div>
              </section>

              {/* NAV comparison chart */}
              <section className="chart-card">
                <div className="chart-header">
                  <h3>V1 vs V2 — NAV Over Time (+Rake layer)</h3>
                  <span className="chart-badge badge-v2">V1 vs V2</span>
                </div>
                <p className="hint">
                  Solid = full strategy (+rake). Dashed = rake-floor only.
                  V2 uses Kelly sizing + priority ordering + phase schedule.
                </p>
                <ResponsiveContainer width="100%" height={280}>
                  <LineChart data={compNavData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                    <XAxis dataKey="day" tick={{ fill: "#94a3b8", fontSize: 12 }}
                      label={{ value: "Day", position: "insideBottom", dy: 10, fill: "#94a3b8", fontSize: 12 }} />
                    <YAxis tick={{ fill: "#94a3b8", fontSize: 12 }} tickFormatter={(v) => `$${fmt(v / 1000)}k`} />
                    <Tooltip formatter={(v) => `$${fmt(v)}`} labelFormatter={(l) => `Day ${l}`} />
                    <Legend />
                    <Line type="monotone" dataKey="v1_floor" stroke="#94a3b8" strokeWidth={1} strokeDasharray="4 3" dot={false} name="V1 Rake Floor" />
                    <Line type="monotone" dataKey="v2_floor" stroke="#a78bfa" strokeWidth={1} strokeDasharray="4 3" dot={false} name="V2 Rake Floor" />
                    <Line type="monotone" dataKey="v1_rake"  stroke="#f59e0b" strokeWidth={2.5} dot={false} name="V1 +Rake" />
                    <Line type="monotone" dataKey="v2_rake"  stroke="#a78bfa" strokeWidth={2.5} dot={false} name="V2 +Rake" />
                  </LineChart>
                </ResponsiveContainer>
              </section>

              {/* Capital utilization chart */}
              <section className="chart-card">
                <div className="chart-header">
                  <h3>V2 — Daily Capital Utilization</h3>
                  <span className="chart-badge badge-v2">V2 Only</span>
                </div>
                <p className="hint">
                  Bar height = % of daily budget deployed (including carryover).
                  Color = phase (
                  <span style={{ color: PHASE_COLORS[1] }}>■ P1 aggressive</span>,{" "}
                  <span style={{ color: PHASE_COLORS[2] }}>■ P2</span>,{" "}
                  <span style={{ color: PHASE_COLORS[3] }}>■ P3 selective</span>).
                  Harvest days are outlined in amber.
                </p>
                <ResponsiveContainer width="100%" height={220}>
                  <BarChart data={utilizationData}>
                    <CartesianGrid strokeDasharray="3 3" stroke="rgba(255,255,255,0.05)" />
                    <XAxis dataKey="day" tick={{ fill: "#94a3b8", fontSize: 12 }} />
                    <YAxis tick={{ fill: "#94a3b8", fontSize: 12 }} tickFormatter={(v) => `${v}%`} domain={[0, 100]} />
                    <Tooltip
                      formatter={(v, name) => name === "utilization" ? `${v}%` : `$${fmt(v)}`}
                      labelFormatter={(l) => {
                        const d = utilizationData[l - 1];
                        return d ? `Day ${l} · Phase ${d.phase}${d.harvest ? " · HARVEST" : ""}` : `Day ${l}`;
                      }}
                    />
                    <ReferenceLine y={100} stroke="rgba(248,113,113,0.4)" strokeDasharray="3 2" />
                    <Bar dataKey="utilization" name="utilization" radius={[2, 2, 0, 0]}>
                      {utilizationData.map((d, i) => (
                        <Cell
                          key={i}
                          fill={PHASE_COLORS[d.phase]}
                          stroke={d.harvest ? "#f59e0b" : "transparent"}
                          strokeWidth={d.harvest ? 2 : 0}
                        />
                      ))}
                    </Bar>
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
