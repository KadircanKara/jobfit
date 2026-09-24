import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { Check, ExternalLink, Sparkles, X } from "lucide-react";
import type { GateReport, OutreachStates, RankReport, RunEvent, RunState, ShortlistRow } from "./api";
import type { SourceProgress } from "./app/store";
import { MarkApplied } from "./MarkApplied";

const PHASES: [string, string][] = [
  ["sync", "Sync"],
  ["rank", "Rank"],
  ["gate", "Fit gate"],
  ["shortlist", "Shortlist"],
];

const STATE_WORD: Record<string, string> = {
  pending: "Waiting",
  active: "Running",
  done: "Done",
  failed: "Failed",
  idle: "Not run",
  unrecorded: "No record",
  onfile: "On file",
};

/** Where each phase stands. A phase only reads as done when it left something
 *  behind, so a stopped or failed run never claims work it did not do. */
function statusesFor(state: RunState): string[] {
  const order = PHASES.map(([key]) => key);
  const has = [
    Boolean(state.counters.boards_done),
    Boolean(state.rank),
    Boolean(state.gate),
    state.results.some((row) => !row.below_bar),
  ];
  const at = order.indexOf(state.phase);
  if (state.running && at >= 0) {
    return order.map((_, i) => (i < at ? "done" : i === at ? "active" : "pending"));
  }
  if (state.phase === "failed") {
    const failedAt = has.findIndex((done) => !done);
    return order.map((_, i) => (failedAt < 0 || i < failedAt ? "done" : i === failedAt ? "failed" : "idle"));
  }
  if (state.outcome === "restored" || !state.outcome) {
    // A shortlist on file came from some run; with no record of it, its phases
    // are unknown rather than never run.
    return order.map((_, i) => (i === 3 && has[3] ? "onfile" : has[3] ? "unrecorded" : "idle"));
  }
  return order.map((_, i) => (has[i] ? "done" : "idle"));
}

/** The run's four phases as one strip: where it is, and what each phase found. */
export function Pipeline({ state }: { state: RunState }) {
  const statuses = statusesFor(state);
  return (
    <div className="pipeline" role="list" aria-label="Run phases">
      {PHASES.map(([key, label], index) => {
        const status = statuses[index];
        const shown = status === "onfile" ? "done" : status === "unrecorded" ? "idle" : status;
        return (
          <div className="stage" key={key} data-status={shown} role="listitem">
            <div className="stage-top">
              <span className="stage-icon" aria-hidden="true">
                {shown === "done" && <Check />}
                {shown === "failed" && <X />}
                {shown === "active" && <span className="pulse" />}
              </span>
              {label}
              <span className="state">{STATE_WORD[status]}</span>
            </div>
            <div className="stage-val">{valueFor(key, state)}</div>
          </div>
        );
      })}
    </div>
  );
}

// Each cell states the whole phase rather than one counter: a bare number here
// reads as progress toward nothing.
function valueFor(phase: string, state: RunState) {
  if (phase === "sync") {
    const jobs = state.counters.jobs_total ?? 0;
    const boards = state.counters.boards_done ?? 0;
    if (!boards) return "—";
    return `${plural(jobs, "job")} · ${plural(boards, "board")}`;
  }
  if (phase === "rank") {
    const rank = state.rank;
    if (!rank?.scored) return "—";
    return `${rank.passed.toLocaleString()} of ${rank.scored.toLocaleString()} passed`;
  }
  if (phase === "gate") {
    const gate = state.gate;
    if (!gate) return "—";
    const total = gate.plan.batches.length;
    if (!total) return "nothing to gate";
    const done = gate.plan.batches.filter((batch) => batch.status !== "queued").length;
    return `batch ${Math.max(done, 1)} of ${total}`;
  }
  const kept = state.results.filter((row) => !row.below_bar).length;
  return kept ? `${kept} jobs above the bar` : "—";
}

export function SourceRail({ sources }: { sources: Record<string, SourceProgress> }) {
  const names = Object.keys(sources);
  if (!names.length) return <div className="empty">Nothing fetched yet in this session.</div>;
  return (
    <div className="rail">
      {names.map((name) => {
        const row = sources[name];
        const pct = row.total ? Math.round((row.done / row.total) * 100) : 0;
        const tone = row.status === "degraded" ? "warn" : row.status === "done" ? "ok" : "accent";
        return (
          <div className="src" key={name} data-status={row.status}>
            <div className="nm" data-tone={tone}>
              <span className="dot" data-live={row.status === "active" ? "true" : undefined} />
              {name}
              {row.status === "degraded" && <span className="tag">degraded</span>}
            </div>
            <div className="progress" data-tone={tone === "accent" ? undefined : tone}>
              <i style={{ width: `${pct}%` }} />
            </div>
            <div className="ct">
              {row.done}/{row.total} boards · {row.jobs.toLocaleString()} jobs
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
    <section className="panel">
      <div className="panel-head">
        <h2>Rank</h2>
        <span className="note">
          {running
            ? `${report.scored.toLocaleString()} of ${report.corpus.toLocaleString()} scored`
            : `${report.scored.toLocaleString()} scored · ${report.passed.toLocaleString()} passed`}
        </span>
      </div>

      <div className="metrics">
        <div className="metric">
          <div className="k">Active corpus</div>
          <div className="v">{report.corpus.toLocaleString()}</div>
          <div className="s">
            {report.skipped ? `${report.skipped.toLocaleString()} scored earlier` : "canonical rows only"}
          </div>
        </div>
        <div className="metric">
          <div className="k">{running ? "Scored so far" : "Scored"}</div>
          <div className="v">{report.scored.toLocaleString()}</div>
          <div className="s">{running && left ? `${left.toLocaleString()} to go` : "this run"}</div>
        </div>
        <div className="metric" data-tone="ok">
          <div className="k">{running ? "Passing" : "Passed"}</div>
          <div className="v">{report.passed.toLocaleString()}</div>
          <div className="s">{report.scored ? `${rate}% of scored` : "—"}</div>
        </div>
        <div className="metric">
          <div className="k">Dropped</div>
          <div className="v">{report.failed.toLocaleString()}</div>
          <div className="s">by the rules below</div>
        </div>
      </div>

      {running && report.corpus > 0 && (
        <div className="progress" style={{ marginBottom: 14 }} aria-label="Rank progress">
          <i style={{ width: `${Math.round(((report.scored + report.skipped) / report.corpus) * 100)}%` }} />
        </div>
      )}

      {report.reasons.length ? (
        <div className="reasons">
          {report.reasons.map((reason) => (
            <div className="reason" key={reason.code} data-tunable={reason.tunable}>
              <div className="nm" title={reason.tunable ? `${reason.label} · adjustable in Filters` : reason.label}>
                {reason.tunable && <Sparkles aria-label="adjustable in Filters" />}
                {reason.label}
              </div>
              <div className="progress">
                <i style={{ width: `${heaviest ? Math.round((reason.count / heaviest) * 100) : 0}%` }} />
              </div>
              <div className="ct">{reason.count.toLocaleString()}</div>
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
          <span className="split aside">marked rules can be adjusted in Filters</span>
        )}
        {report.reasons.length > 1 && <span className="split aside">a job can fail more than one rule</span>}
        {report.fx_age_hours != null && report.fx_age_hours > 24 && (
          <span className="split aside">salaries compared at rates {Math.round(report.fx_age_hours / 24)}d old</span>
        )}
      </div>
    </section>
  );
}

// Ten buckets across the 0.0 to 1.0 scale the gate is contracted to answer on.
const BUCKETS = 10;

export function GatePanel({ report }: { report: GateReport }) {
  const { plan, verdicts } = report;
  const [open, setOpen] = useState(false);
  if (!plan.batches.length) {
    return (
      <section className="panel">
        <div className="panel-head">
          <h2>Fit gate</h2>
          <span className="note">nothing new to gate</span>
        </div>
        <div className="empty">Every job that passed the rules already has a verdict from an earlier run.</div>
      </section>
    );
  }

  const scores = verdicts.map((verdict) => verdict.score).filter((score): score is number => score != null);
  const counts = new Array(BUCKETS).fill(0);
  for (const score of scores) counts[Math.min(BUCKETS - 1, Math.floor(score * BUCKETS))] += 1;
  const tallest = Math.max(...counts, 1);
  const overFrom = plan.bar != null ? Math.floor(plan.bar * BUCKETS) : BUCKETS;

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Fit gate</h2>
        <span className="note">
          {plan.jobs} jobs · {plan.batches.length} batches · {report.scored} scored
        </span>
        {verdicts.length > 0 && (
          <button type="button" className="btn ghost sm" aria-expanded={open} onClick={() => setOpen((was) => !was)}>
            {open ? "Hide verdicts" : `Show all ${verdicts.length} verdicts`}
          </button>
        )}
      </div>

      <div className="gatehead">
        <div>
          <div className="batches">
            {plan.batches.map((batch) => (
              <span className="batch" key={batch.label} data-status={batch.status} title={batch.status}>
                <b>{batch.label}</b>
                {batch.size}
              </span>
            ))}
          </div>
          {report.ungated > 0 && (
            <div className="hint" style={{ marginTop: 12 }}>
              {report.ungated} jobs came back unreadable and were left ungated. They keep their place and are
              scored on the next run.
            </div>
          )}
          {scores.length > 0 && (
            <div style={{ marginTop: 16 }}>
              <div className="dist" aria-label="Score distribution">
                {counts.map((count, index) => (
                  <div className="col" key={index} data-over={index >= overFrom}>
                    {count > 0 && <i style={{ height: `${Math.round((count / tallest) * 64) + 4}px` }} />}
                    <span>{count || ""}</span>
                  </div>
                ))}
              </div>
              <div className="distfoot">
                <span>0.0</span>
                {plan.bar != null && <span className="at">the shortlist bar is {plan.bar.toFixed(2)}</span>}
                <span>1.0</span>
              </div>
            </div>
          )}
        </div>

        <div className="kv">
          <div>
            <span>Sent to the gate</span>
            <b>{plan.jobs}</b>
          </div>
          {plan.held_by_company_cap > 0 && (
            <div>
              <span>Held by the company cap</span>
              <b>{plan.held_by_company_cap}</b>
            </div>
          )}
          <div>
            <span>Scored</span>
            <b>{report.scored}</b>
          </div>
          {plan.bar != null && (
            <div>
              <span>Above the bar</span>
              <b>{scores.filter((score) => score >= plan.bar!).length}</b>
            </div>
          )}
        </div>
      </div>

      {open && verdicts.length > 0 && (
        <div className="tablewrap panel-flush" style={{ marginTop: 16, borderTop: "1px solid var(--border)" }}>
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
              {[...verdicts].sort((a, b) => (b.score ?? -1) - (a.score ?? -1)).map((verdict) => (
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
    </section>
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
      {events.slice(-200).map((event) => (
        <div key={event.seq} className={event.level ?? "info"}>
          <span className="seq">{event.phase}</span>
          <span>{event.message}</span>
        </div>
      ))}
    </div>
  );
}

const OUTREACH_WORD: Record<string, string> = {
  drafted: "drafted",
  queued: "queued",
  sent: "sent",
  failed: "failed",
  cancelled: "cancelled",
};

export function Shortlist({
  rows,
  picked,
  bar,
  onPick,
  onPickAll,
  applied,
  onApplied,
  outreachStates,
  openJob,
  onOutreach,
}: {
  rows: ShortlistRow[];
  picked: Set<number>;
  bar: number | null;
  onPick: (id: number, on: boolean) => void;
  onPickAll: (ids: number[], on: boolean) => void;
  applied: Set<number>;
  onApplied: (id: number, on: boolean) => void;
  outreachStates?: OutreachStates;
  openJob?: number;
  onOutreach: (jobId: number) => void;
}) {
  const [query, setQuery] = useState({ role: "", company: "", where: "" });
  // Filtering happens here rather than upstream: a typed narrowing is a way of
  // reading this table, not a change to the run.
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
    return (
      <div className="empty-state">
        <h2>No jobs above the bar yet</h2>
        <p>Start a run on the Run page. Every job that clears the fit gate lands here, best fit first.</p>
      </div>
    );
  }
  const kept = shown.filter((row) => !row.below_bar).length;
  const missed = shown.length - kept;
  // "Select all" means what is in view and above the bar: near misses were
  // never exported, and a filtered-away row is not what the person is looking at.
  const selectable = shown.filter((row) => !row.below_bar).map((row) => row.job_id);
  const allPicked = selectable.length > 0 && selectable.every((id) => picked.has(id));
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
      <table className="shortlist-table">
        <thead>
          <tr>
            <th className="pick">
              <input
                type="checkbox"
                aria-label="Select every job in view above the bar"
                checked={allPicked}
                onChange={(e) => onPickAll(selectable, e.target.checked)}
              />
            </th>
            <th>Fit</th>
            <th>Role</th>
            <th>Company</th>
            <th>Where</th>
            <th>Why it ranked here</th>
            <th>Outreach</th>
            <th>Applied</th>
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
              {/* Drawn once, where the threshold fell. A run that returns three
                  jobs is usually a bar that moved, not a thin market. */}
              {row.below_bar && index > 0 && !shown[index - 1].below_bar && (
                <tr className="cutrow">
                  <td colSpan={8}>
                    <div className="cut">
                      <span>{bar != null ? `The bar · ${bar.toFixed(2)}` : "The bar"}</span>
                      <span className="aside">
                        {kept} kept above · {missed} near {missed === 1 ? "miss" : "misses"} below, not exported
                      </span>
                    </div>
                  </td>
                </tr>
              )}
              <tr
                className={row.below_bar ? "below" : undefined}
                data-picked={picked.has(row.job_id)}
                aria-selected={openJob === row.job_id}
              >
                <td className="pick">
                  <input
                    type="checkbox"
                    aria-label={`Select ${row.title} at ${row.company}`}
                    checked={picked.has(row.job_id)}
                    onChange={(e) => onPick(row.job_id, e.target.checked)}
                  />
                </td>
                <td className="fit">{row.fit != null ? row.fit.toFixed(2) : "—"}</td>
                <td className="c-role">
                  <span className="role">{headOf(row.title)}</span>
                  <span className="nowrap">
                    <span className="role">{tailOf(row.title)}</span>
                    {row.url && (
                    <a
                      className="posting"
                      href={row.url}
                      target="_blank"
                      rel="noreferrer"
                      title={`Open the posting on ${hostOf(row.url)}`}
                      aria-label={`Open the posting on ${hostOf(row.url)}`}
                    >
                      <ExternalLink aria-hidden="true" />
                    </a>
                    )}
                  </span>
                  {row.salary && <div className="co tabular">{row.salary}</div>}
                </td>
                <td className="c-company">
                  <span className="company">{row.company}</span>
                  <div className="co">via {row.source}</div>
                </td>
                <td className="c-where">
                  {row.remote_type && row.remote_type !== "unknown" ? (
                    <span className="tag">{row.remote_type}</span>
                  ) : null}
                  <div className="co" style={{ marginTop: row.remote_type && row.remote_type !== "unknown" ? 4 : 0 }}>
                    {row.location || "location not stated"}
                  </div>
                </td>
                <td className="why">
                  <Reason text={row.reasoning} />
                </td>
                <td className="c-out">
                  <button
                    type="button"
                    className="outbtn"
                    aria-expanded={openJob === row.job_id}
                    onClick={() => onOutreach(row.job_id)}
                  >
                    <span className="dot" data-state={outreachStates?.[row.job_id]} />
                    Outreach
                    {outreachStates?.[row.job_id] && OUTREACH_WORD[outreachStates[row.job_id]] && (
                      <span className="state">{OUTREACH_WORD[outreachStates[row.job_id]]}</span>
                    )}
                  </button>
                </td>
                <td className="c-sent">
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

/** The gate's reason, two lines at first: the table is read by scanning, and
 *  the full paragraph is one click away. */
function Reason({ text }: { text: string | null }) {
  const [open, setOpen] = useState(false);
  if (!text) return <span className="muted">No reason given</span>;
  return (
    <>
      <div className={open ? undefined : "clamp"}>{text}</div>
      {text.length > 110 && (
        <button type="button" className="more" onClick={() => setOpen((was) => !was)} aria-expanded={open}>
          {open ? "Show less" : "Show more"}
        </button>
      )}
    </>
  );
}

/** A title split before its last word, so the posting icon travels with that
 *  word instead of wrapping onto a line of its own. */
function headOf(title: string) {
  const cut = title.lastIndexOf(" ");
  return cut < 0 ? "" : title.slice(0, cut + 1);
}

function tailOf(title: string) {
  const cut = title.lastIndexOf(" ");
  return cut < 0 ? title : title.slice(cut + 1);
}

export function plural(count: number, word: string) {
  return `${count.toLocaleString()} ${word}${count === 1 ? "" : "s"}`;
}

function matches(value: string, needle: string) {
  const q = needle.trim().toLowerCase();
  return !q || (value ?? "").toLowerCase().includes(q);
}

export function hostOf(url: string) {
  try {
    return new URL(url).host.replace(/^www\./, "");
  } catch {
    return url.slice(0, 30);
  }
}
