import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import type { GateReport, RankReport, RunEvent, RunState, ShortlistRow } from "./api";
import { MarkApplied } from "./MarkApplied";

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

// Each cell states the whole phase rather than one counter: the strip is the
// run's spine, and a bare number there reads as progress toward nothing.
function valueFor(phase: string, state: RunState) {
  if (phase === "sync") {
    const jobs = state.counters.jobs_total ?? 0;
    const boards = state.counters.boards_done ?? 0;
    if (!boards) return "queued";
    return `${jobs.toLocaleString()} jobs · ${boards.toLocaleString()} boards`;
  }
  if (phase === "rank") {
    const rank = state.rank;
    if (!rank?.scored) return "queued";
    return `${rank.passed.toLocaleString()} of ${rank.scored.toLocaleString()} passed`;
  }
  if (phase === "gate") {
    const gate = state.gate;
    if (!gate) return "queued";
    const total = gate.plan.batches.length;
    if (!total) return "nothing to gate";
    const done = gate.plan.batches.filter((batch) => batch.status !== "queued").length;
    return `batch ${Math.max(done, 1)} / ${total}`;
  }
  const kept = state.results.filter((row) => !row.below_bar).length;
  return kept ? `${kept} jobs` : "queued";
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

export function RankPanel({ report, running }: { report: RankReport; running: boolean }) {
  const heaviest = report.reasons[0]?.count ?? 0;
  const markets = Object.entries(report.by_market);
  const rate = report.scored ? Math.round((report.passed / report.scored) * 1000) / 10 : 0;
  const left = Math.max(0, report.corpus - report.scored - report.skipped);

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>Rank</h2>
        <div className="note">
          {running
            ? `${report.scored.toLocaleString()} / ${report.corpus.toLocaleString()} scored`
            : `${report.scored.toLocaleString()} scored · ${report.passed.toLocaleString()} passed`}
        </div>
      </div>

      <div className="funnel">
        <div className="stepbox">
          <div className="k">Active corpus</div>
          <div className="v">{report.corpus.toLocaleString()}</div>
          <div className="s">
            {report.skipped
              ? `${report.skipped.toLocaleString()} scored earlier`
              : "canonical rows only"}
          </div>
        </div>
        <div className="stepbox">
          <div className="k">{running ? "Scored so far" : "Scored"}</div>
          <div className="v">{report.scored.toLocaleString()}</div>
          <div className="s">{running && left ? `${left.toLocaleString()} to go` : "this run"}</div>
        </div>
        <div className="stepbox" data-tone="pass">
          <div className="k">{running ? "Passing" : "Passed"}</div>
          <div className="v">{report.passed.toLocaleString()}</div>
          <div className="s">{report.scored ? `${rate}% of scored` : "—"}</div>
        </div>
        <div className="stepbox" data-tone="drop">
          <div className="k">Dropped</div>
          <div className="v">{report.failed.toLocaleString()}</div>
          <div className="s">by the rules below</div>
        </div>
      </div>

      {running && report.corpus > 0 && (
        <div className="bar live" style={{ marginBottom: 16 }}>
          <i style={{ width: `${Math.round(((report.scored + report.skipped) / report.corpus) * 100)}%` }} />
        </div>
      )}

      {report.reasons.length ? (
        <div className="reasons">
          {report.reasons.map((reason) => (
            <div className="reason" key={reason.code} data-tunable={reason.tunable}>
              <div className="nm" title={reason.label}>
                {reason.label}
              </div>
              <div className="bar">
                <i style={{ width: `${heaviest ? Math.round((reason.count / heaviest) * 100) : 0}%` }} />
              </div>
              <div className="ct">
                <b>{reason.count.toLocaleString()}</b>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <div className="empty">Nothing dropped yet.</div>
      )}

      <div className="splits">
        {markets.map(([market, count]) => (
          <span className="split" key={market}>
            {market} <b>{count.toLocaleString()}</b>
          </span>
        ))}
        {report.reasons.some((reason) => reason.tunable) && (
          <span className="split aside">gold rules can be moved in Filters above</span>
        )}
        {report.reasons.length > 1 && (
          <span className="split aside">a job can fail more than one rule</span>
        )}
        {report.fx_age_hours != null && report.fx_age_hours > 24 && (
          <span className="split aside">
            salaries compared at rates {Math.round(report.fx_age_hours / 24)}d old
          </span>
        )}
      </div>
    </div>
  );
}

// Ten buckets across the 0.0-1.0 scale the gate is contracted to answer on.
const BUCKETS = 10;

export function GatePanel({ report }: { report: GateReport }) {
  const { plan, verdicts } = report;
  if (!plan.batches.length) {
    return (
      <div className="panel">
        <div className="panel-head">
          <h2>Fit gate</h2>
          <div className="note">nothing new to gate</div>
        </div>
        <div className="empty">
          Every job that passed the rules already carries a verdict from an earlier run.
        </div>
      </div>
    );
  }

  const scores = verdicts.map((verdict) => verdict.score).filter((score): score is number => score != null);
  const counts = new Array(BUCKETS).fill(0);
  for (const score of scores) counts[Math.min(BUCKETS - 1, Math.floor(score * BUCKETS))] += 1;
  const tallest = Math.max(...counts, 1);
  const overFrom = plan.bar != null ? Math.floor(plan.bar * BUCKETS) : BUCKETS;

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>Fit gate</h2>
        <div className="note">
          {plan.jobs} jobs · {plan.batches.length} batches · {report.scored} scored
        </div>
      </div>

      <div className="gatehead">
        <div>
          <div className="batches">
            {plan.batches.map((batch) => (
              <div className="batch" key={batch.label} data-status={batch.status}>
                <b>{batch.label}</b>
                {batch.size}
              </div>
            ))}
          </div>
          {report.ungated > 0 && (
            <div className="hint" style={{ marginTop: 12 }}>
              {report.ungated} jobs came back unreadable and were left ungated — they keep their place
              and are scored on the next run.
            </div>
          )}
        </div>

        <div className="gatemeta">
          <div className="metarow">
            <span>Sent to the gate</span>
            <b>{plan.jobs}</b>
          </div>
          {plan.held_by_company_cap > 0 && (
            <div className="metarow">
              <span>Held by the company cap</span>
              <b>{plan.held_by_company_cap}</b>
            </div>
          )}
          <div className="metarow">
            <span>Scored</span>
            <b>{report.scored}</b>
          </div>
          {plan.bar != null && (
            <div className="metarow">
              <span>Above the bar</span>
              <b>{scores.filter((score) => score >= plan.bar!).length}</b>
            </div>
          )}
        </div>
      </div>

      {scores.length > 0 && (
        <div style={{ marginTop: 20 }}>
          <div className="dist">
            {counts.map((count, index) => (
              <div className="col" key={index} data-over={index >= overFrom}>
                {/* An empty bucket draws nothing: a zero-height box still shows
                    its own borders, which reads as a bar that is not there. */}
                {count > 0 && <i style={{ height: `${Math.round((count / tallest) * 74) + 4}px` }} />}
                <span>{count || ""}</span>
              </div>
            ))}
          </div>
          <div className="distfoot">
            <span>0.0</span>
            {plan.bar != null && <span className="at">↑ {plan.bar.toFixed(2)} — the shortlist bar</span>}
            <span>1.0</span>
          </div>
        </div>
      )}

      {verdicts.length > 0 && (
        <div className="tablewrap" style={{ marginTop: 20 }}>
          <table>
            <thead>
              <tr>
                <th>Fit</th>
                <th>Role</th>
                <th>Company</th>
                <th>Why the gate scored it there</th>
                <th>Flags</th>
              </tr>
            </thead>
            <tbody>
              {[...verdicts].reverse().map((verdict) => (
                <tr key={`${verdict.job_id}-${verdict.market}`}>
                  <td className="fit">{verdict.score != null ? verdict.score.toFixed(2) : "—"}</td>
                  <td>
                    <span className="role">{verdict.title}</span>
                  </td>
                  <td>
                    <span className="company">{verdict.company}</span>
                    {verdict.source && <div className="co">via {verdict.source}</div>}
                  </td>
                  <td className="why">{verdict.reasoning || "—"}</td>
                  <td>
                    {verdict.red_flags.map((flag) => (
                      <span className="flag" key={flag}>
                        {flag}
                      </span>
                    ))}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
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
  bar,
  onPick,
  onPickAll,
  applied,
  onApplied,
}: {
  rows: ShortlistRow[];
  picked: Set<number>;
  bar: number | null;
  onPick: (id: number, on: boolean) => void;
  onPickAll: (on: boolean) => void;
  applied: Set<number>;
  onApplied: (id: number, on: boolean) => void;
}) {
  const [query, setQuery] = useState({ role: "", company: "", where: "" });
  // Filtering happens here rather than upstream: the run owns the rows, and a
  // typed narrowing is a way of reading this table, not a change to the run.
  const shown = useMemo(
    () =>
      rows.filter(
        (row) =>
          matches(row.title, query.role) &&
          matches(row.company, query.company) &&
          matches(`${row.remote_type} ${row.location}`, query.where),
      ),
    [rows, query],
  );
  if (!rows.length) {
    return <div className="empty">No jobs above the bar yet. Start a run to fill this.</div>;
  }
  const kept = shown.filter((row) => !row.below_bar).length;
  const missed = shown.length - kept;
  const field = (key: "role" | "company" | "where", label: string) => (
    <input
      className="colfilter"
      type="search"
      value={query[key]}
      placeholder="Filter"
      aria-label={`Filter by ${label}`}
      onChange={(e) => setQuery((q) => ({ ...q, [key]: e.target.value }))}
    />
  );
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
            <th>Sent</th>
          </tr>
          <tr className="filterrow">
            <th className="pick" />
            <th />
            <th>{field("role", "role")}</th>
            <th>{field("company", "company")}</th>
            <th>{field("where", "where")}</th>
            <th />
            <th />
            <th />
          </tr>
        </thead>
        <tbody>
          {!shown.length && (
            <tr>
              <td colSpan={8} className="co">
                No job matches that filter.
              </td>
            </tr>
          )}
          {shown.map((row, index) => (
            <Fragment key={row.job_id}>
              {/* Drawn once, where the threshold actually fell. A run that
                  returns three jobs is usually a bar that moved, not a thin
                  market, and that is invisible if the rejected rows are gone. */}
              {row.below_bar && index > 0 && !shown[index - 1].below_bar && (
                <tr className="cutrow">
                  <td colSpan={8}>
                    <div className="cut">
                      <span>{bar != null ? `The bar · ${bar.toFixed(2)}` : "The bar"}</span>
                      <span className="aside">
                        {kept} kept above · {missed} near {missed === 1 ? "miss" : "misses"} below,
                        not exported
                      </span>
                    </div>
                  </td>
                </tr>
              )}
            <tr
              className={row.below_bar ? "below" : undefined}
              data-picked={picked.has(row.job_id)}
            >
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
              <td className="sent">
                <MarkApplied
                  jobId={row.job_id}
                  applied={applied.has(row.job_id)}
                  onChange={(on) => onApplied(row.job_id, on)}
                />
              </td>
            </tr>
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function matches(value: string, needle: string) {
  const q = needle.trim().toLowerCase();
  return !q || (value ?? "").toLowerCase().includes(q);
}

export function hostOf(url: string) {
  try {
    return new URL(url).host;
  } catch {
    return url.slice(0, 30);
  }
}
