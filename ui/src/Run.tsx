import { useEffect, useRef } from "react";
import type { RunEvent, RunState, ShortlistRow } from "./api";

const PHASES: [string, string][] = [
  ["sync", "Sync"],
  ["rank", "Rank"],
  ["gate", "Fit gate"],
  ["shortlist", "Shortlist"],
];

type SourceProgress = { done: number; total: number; jobs: number; status: string };

export function PhaseStrip({ state }: { state: RunState }) {
  const order = PHASES.map(([key]) => key);
  const at = order.indexOf(state.phase);
  return (
    <div className="phases">
      {PHASES.map(([key, label], index) => {
        const status =
          state.phase === "failed" && index === at
            ? "failed"
            : at < 0
              ? state.outcome
                ? "done"
                : "pending"
              : index < at
                ? "done"
                : index === at
                  ? "active"
                  : "pending";
        return (
          <div className="phase" key={key} data-status={status}>
            <div className="nm">{label}</div>
            <div className="val">{valueFor(key, state)}</div>
          </div>
        );
      })}
    </div>
  );
}

function valueFor(phase: string, state: RunState) {
  if (phase === "sync") {
    const boards = state.counters.boards_done ?? 0;
    return boards ? `${boards.toLocaleString()} boards` : "queued";
  }
  if (phase === "rank") return state.counters.passed ? `${state.counters.passed} passed` : "queued";
  if (phase === "gate") return state.counters.gated ? `${state.counters.gated} batches` : "queued";
  return state.results.length ? `${state.results.length} jobs` : "queued";
}

export function SourceRail({ sources }: { sources: Record<string, SourceProgress> }) {
  const names = Object.keys(sources);
  if (!names.length) return <div className="empty">Nothing fetched yet.</div>;
  return (
    <div className="rail">
      {names.map((name) => {
        const row = sources[name];
        const pct = row.total ? Math.round((row.done / row.total) * 100) : 0;
        return (
          <div className="src" key={name} data-status={row.status}>
            <div className="nm">{name}</div>
            <div className="bar">
              <i style={{ width: `${pct}%` }} />
            </div>
            <div className="ct">
              {row.done}/{row.total} · <b>{row.jobs.toLocaleString()}</b>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function Log({ events }: { events: RunEvent[] }) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [events.length]);

  if (!events.length) return null;
  return (
    <div className="log" ref={box}>
      {events.slice(-60).map((event) => (
        <div key={event.seq} className={event.level ?? "info"}>
          <span>{event.message}</span>
        </div>
      ))}
    </div>
  );
}

export function Shortlist({
  rows,
  picked,
  onPick,
  onPickAll,
}: {
  rows: ShortlistRow[];
  picked: Set<number>;
  onPick: (id: number, on: boolean) => void;
  onPickAll: (on: boolean) => void;
}) {
  if (!rows.length) {
    return <div className="empty">No jobs above the bar yet. Start a run to fill this.</div>;
  }
  return (
    <div className="tablewrap">
      <table>
        <thead>
          <tr>
            <th className="pick">
              <input
                type="checkbox"
                aria-label="Select every job"
                checked={picked.size === rows.length}
                onChange={(e) => onPickAll(e.target.checked)}
              />
            </th>
            <th>Fit</th>
            <th>Role</th>
            <th>Company</th>
            <th>Where</th>
            <th>Why it ranked here</th>
            <th>Posting</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.job_id} data-picked={picked.has(row.job_id)}>
              <td className="pick">
                <input
                  type="checkbox"
                  aria-label={`Select ${row.company}`}
                  checked={picked.has(row.job_id)}
                  onChange={(e) => onPick(row.job_id, e.target.checked)}
                />
              </td>
              <td className="fit">{row.fit != null ? row.fit.toFixed(2) : "—"}</td>
              <td>
                <span className="role">{row.title}</span>
              </td>
              <td>
                <span className="company">{row.company}</span>
                <div className="co">via {row.source}</div>
              </td>
              <td>
                <span className="tag">{row.remote_type || "—"}</span>
                <div className="co" style={{ marginTop: 5 }}>
                  {row.location}
                </div>
              </td>
              <td className="why">{row.reasoning ?? "—"}</td>
              <td className="posting">
                {row.url ? (
                  <a href={row.url} target="_blank" rel="noreferrer" title={row.url}>
                    <span className="host">{hostOf(row.url)}</span>
                    <span className="go">Open ↗</span>
                  </a>
                ) : (
                  <span className="co">no link</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function hostOf(url: string) {
  try {
    return new URL(url).host;
  } catch {
    return url.slice(0, 30);
  }
}
