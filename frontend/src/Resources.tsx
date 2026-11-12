import { useEffect, useState } from "react";
import { api, Chat, fmtMb, User } from "./api";

interface Snap {
  uptime_s: number;
  load: number[];
  cpu_count: number;
  cpu_percent: number;
  cpu_per_core: number[];
  mem: { total_mb: number; available_mb: number; used_mb: number; cached_mb: number };
  swap: { total_mb: number; used_mb: number };
  disk: { total_gb: number; used_gb: number; free_gb: number; percent: number };
  net: { rx_bps: number; tx_bps: number };
  history: { ts: number; cpu: number; mem_avail_mb: number; mem_used_mb: number }[];
  reserve_mb: number;
  critical_mb: number;
  estimate_mb: number;
  my_sessions: Chat[];
  my_limit: number;
  sessions_total: number;
  top_processes?: { pid: number; name: string; user: string; rss_mb: number; cpu: number; cmd: string }[];
  data_dir_mb?: number | null;
}

export default function Resources({ user }: { user: User }) {
  const [s, setS] = useState<Snap | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let alive = true;
    const load = () =>
      api<Snap>("/resources")
        .then((d) => alive && (setS(d), setErr("")))
        .catch((e) => alive && setErr(e.message));
    load();
    const t = setInterval(() => document.visibilityState === "visible" && load(), 3000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);
  if (!s) return <div className="pad muted">{err || "Загрузка…"}</div>;

  const memPct = (s.mem.used_mb / s.mem.total_mb) * 100;
  const reservePct = (s.reserve_mb / s.mem.total_mb) * 100;
  const free = s.mem.available_mb - s.reserve_mb;
  const canStart = Math.max(0, Math.floor(free / s.estimate_mb));
  const level = s.mem.available_mb < s.critical_mb ? "crit" : s.mem.available_mb < s.reserve_mb ? "warn" : "ok";

  return (
    <div className="page">
      {err && <div className="error">{err}</div>}
      <div className="grid">
        <div className="card stat">
          <div className="stat-label">Память</div>
          <div className="stat-value">{fmtMb(s.mem.used_mb)} <span className="muted">из {fmtMb(s.mem.total_mb)}</span></div>
          <div className="meter" aria-label="Использование памяти">
            <div className={"meter-fill " + level} style={{ width: `${memPct}%` }} />
            <div className="meter-mark" style={{ left: `${100 - reservePct}%` }} title="Граница резерва" />
          </div>
          <div className="small muted">
            Свободно {fmtMb(s.mem.available_mb)} · резерв системы {fmtMb(s.reserve_mb)} · можно запустить ещё ~{canStart} сесс.
          </div>
        </div>
        <div className="card stat">
          <div className="stat-label">Процессор · {s.cpu_count} ядра</div>
          <div className="stat-value">{s.cpu_percent.toFixed(0)}%</div>
          <div className="cores">
            {s.cpu_per_core.map((c, i) => (
              <div key={i} className="core" title={`Ядро ${i + 1}: ${c.toFixed(0)}%`}>
                <div style={{ height: `${c}%` }} />
              </div>
            ))}
          </div>
          <div className="small muted">Нагрузка: {s.load.map((l) => l.toFixed(2)).join(" / ")}</div>
        </div>
        <div className="card stat">
          <div className="stat-label">Диск /</div>
          <div className="stat-value">{s.disk.used_gb} ГБ <span className="muted">из {s.disk.total_gb}</span></div>
          <div className="meter"><div className={"meter-fill " + (s.disk.percent > 90 ? "crit" : s.disk.percent > 80 ? "warn" : "ok")} style={{ width: `${s.disk.percent}%` }} /></div>
          <div className="small muted">
            Свободно {s.disk.free_gb} ГБ{s.data_dir_mb != null && ` · данные webpanel ${s.data_dir_mb < 1 ? "<1 МБ" : fmtMb(s.data_dir_mb)}`}
          </div>
        </div>
        <div className="card stat">
          <div className="stat-label">Сеть и система</div>
          <div className="stat-value small-value"><span className="nowrap">↓ {fmtRate(s.net.rx_bps)}</span> <span className="nowrap">↑ {fmtRate(s.net.tx_bps)}</span></div>
          <div className="small muted">Swap: {s.swap.total_mb ? `${fmtMb(s.swap.used_mb)} из ${fmtMb(s.swap.total_mb)}` : "нет"}</div>
          <div className="small muted">Аптайм: {fmtUptime(s.uptime_s)} · сессий webpanel: {s.sessions_total}</div>
        </div>
      </div>

      <div className="card">
        <div className="row between">
          <strong>Память за последние минуты</strong>
          <span className="small muted">свободно · пунктир — резерв {fmtMb(s.reserve_mb)}</span>
        </div>
        <Spark data={s.history.map((h) => h.mem_avail_mb)} max={s.mem.total_mb} mark={s.reserve_mb} unit="МБ" />
        <div className="row between">
          <strong>Загрузка CPU</strong>
          <span className="small muted">%</span>
        </div>
        <Spark data={s.history.map((h) => h.cpu)} max={100} unit="%" />
      </div>

      <div className="card">
        <strong>Мои активные сессии ({s.my_sessions.length}/{s.my_limit})</strong>
        {s.my_sessions.length === 0 && <div className="muted small">Нет запущенных сессий</div>}
        <table className="table">
          <tbody>
            {s.my_sessions.map((c) => (
              <tr key={c.id}>
                <td>{c.title}</td>
                <td className="num">{fmtMb(c.rss_mb)}</td>
                <td>{c.busy ? "отвечает" : "ожидает"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {user.is_admin && s.top_processes && (
        <div className="card">
          <strong>Процессы с наибольшим потреблением памяти</strong>
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr><th>PID</th><th>Имя</th><th className="num">RSS</th><th className="num">CPU</th><th>Команда</th></tr>
              </thead>
              <tbody>
                {s.top_processes.map((p) => (
                  <tr key={p.pid}>
                    <td className="num">{p.pid}</td>
                    <td>{p.name}</td>
                    <td className="num">{fmtMb(p.rss_mb)}</td>
                    <td className="num">{p.cpu.toFixed(0)}%</td>
                    <td className="cmd" title={p.cmd}>{p.cmd}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}

function Spark({ data, max, mark, unit }: { data: number[]; max: number; mark?: number; unit: string }) {
  const W = 600, H = 70;
  if (data.length < 2) return <div className="muted small pad">Собираю данные…</div>;
  const x = (i: number) => (i / (data.length - 1)) * W;
  const y = (v: number) => H - (Math.min(v, max) / max) * (H - 4) - 2;
  const path = data.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const last = data[data.length - 1];
  return (
    <div className="spark">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label={`Последнее значение ${Math.round(last)} ${unit}`}>
        <path d={`${path}L${W},${H}L0,${H}Z`} className="spark-area" />
        <path d={path} className="spark-line" vectorEffect="non-scaling-stroke" />
        {mark !== undefined && <line x1="0" x2={W} y1={y(mark)} y2={y(mark)} className="spark-mark" vectorEffect="non-scaling-stroke" />}
      </svg>
      <span className="spark-last">{Math.round(last)} {unit}</span>
    </div>
  );
}

function fmtRate(bps: number) {
  if (bps > 1024 * 1024) return `${(bps / 1024 / 1024).toFixed(1)} МБ/с`;
  if (bps > 1024) return `${(bps / 1024).toFixed(0)} КБ/с`;
  return `${bps.toFixed(0)} Б/с`;
}

function fmtUptime(s: number) {
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d} д ${h} ч` : `${h} ч ${m} мин`;
}
