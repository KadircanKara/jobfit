import { useState } from "react";
import { ArrowRight, History, Info, Pause, Play, RotateCcw, Square, TriangleAlert } from "lucide-react";
import { SourcePicker, sourceLabel } from "../Filters";
import { GatePanel, Log, Pipeline, RankPanel, SourceRail, plural } from "../Run";
import { followLink, href, type Route } from "../app/router";
import { runStatus, runTone, stamp, useHunt, useShownRun } from "../app/store";

const TONE: Record<string, string> = {
  running: "accent",
  paused: "warn",
  failed: "danger",
  stopped: "warn",
  done: "ok",
  idle: "neutral",
};

export function RunPage({ runId, go }: { runId?: string; go: (to: Route | string) => void }) {
  const hunt = useHunt();
  // Idle with a shortlist on file: show the last saved run, so the page says
  // what that run found rather than a row of phases that were "not run".
  const lastRun = !runId && !hunt.run.running && hunt.run.outcome === "restored" ? hunt.past[0] : undefined;
  const { run, saved, missing } = useShownRun(runId ?? lastRun?.run_id);
  const onFile = lastRun ? hunt.run.results : undefined;
  // The feed is the loudest thing on the page and the least often wanted, so it
  // starts shut; the phase strip above carries the run's state.
  const [feedOpen, setFeedOpen] = useState(false);
  const live = hunt.run;

  const restored = hunt.run.outcome === "restored" && !runId;
  const started = run.running || (run.outcome !== null && run.outcome !== "restored") || saved;
  const kept = (onFile ?? run.results).filter((row) => !row.below_bar);
  const tone = lastRun ? "idle" : runTone(run);

  async function start() {
    if (await hunt.start()) {
      if (runId) go({ page: "run" });
      window.scrollTo({ top: 0 });
    }
  }

  return (
    <div className="page">
      <header className="page-header">
        <h1>Run</h1>
        <span className="badge" data-tone={TONE[tone]}>
          <span className="dot" data-live={run.running ? "true" : undefined} />
          {lastRun ? "Idle" : saved ? `Saved run · ${runStatus(run)}` : runStatus(run)}
        </span>
        {run.running && !saved && <span className="sub">The tab can be closed; the run keeps going.</span>}
        <div className="page-actions">
          {live.running ? (
            <>
              <button
                className="btn ghost"
                onClick={hunt.pause}
                disabled={live.paused}
                title="Stop at the next board and keep what has been fetched"
              >
                <Pause aria-hidden="true" />
                {live.paused ? "Pausing" : "Pause"}
              </button>
              <button
                className="btn stop"
                onClick={() => {
                  if (window.confirm("Stop the run? Nothing further is ranked, gated or shortlisted.")) {
                    void hunt.stop();
                  }
                }}
                disabled={live.stopping}
              >
                <Square aria-hidden="true" />
                {live.stopping ? "Stopping" : "Stop run"}
              </button>
            </>
          ) : live.resumable ? (
            <button className="btn" onClick={hunt.resume} title="Pick up where it paused">
              <RotateCcw aria-hidden="true" />
              Resume run
            </button>
          ) : (
            <button
              className="btn"
              onClick={start}
              disabled={!hunt.valid}
              title={hunt.valid ? "Sync, rank, gate and shortlist" : "Fix the filters before starting a run"}
            >
              <Play aria-hidden="true" />
              Start run
            </button>
          )}
        </div>
      </header>

      {hunt.runError && (
        <div className="notice" data-tone="danger" role="alert" style={{ marginBottom: 12 }}>
          <TriangleAlert className="icon" aria-hidden="true" />
          <span className="grow">{hunt.runError}</span>
        </div>
      )}
      {!hunt.valid && !live.running && (
        <div className="notice" data-tone="warn" style={{ marginBottom: 12 }}>
          <TriangleAlert className="icon" aria-hidden="true" />
          <span className="grow">
            The filters have an unsaved problem, so a run cannot start.{" "}
            <a href={href({ page: "search" })} onClick={(e) => followLink(e, () => go({ page: "search" }))}>
              Open Filters
            </a>
          </span>
        </div>
      )}

      <div className="toolbar">
        {hunt.filters && (
          <SourcePicker value={hunt.sources} onChange={hunt.pickSources} saving={hunt.savingSources} />
        )}
        {hunt.past.length > 0 && (
          <>
            <span className="sep" aria-hidden="true" />
            <span className="lbl">
              <History className="icon" aria-hidden="true" style={{ verticalAlign: -3, width: 14 }} /> Runs
            </span>
            <div className="seg" role="group" aria-label="Which run">
              <a
                href="/"
                aria-current={!runId ? "page" : undefined}
                onClick={(e) => followLink(e, () => go({ page: "run" }))}
              >
                Live
              </a>
              {hunt.past.map((row) => (
                <a
                  key={row.run_id}
                  href={href({ page: "run", runId: row.run_id })}
                  aria-current={runId === row.run_id ? "page" : undefined}
                  title={`${row.jobs_total.toLocaleString()} jobs fetched`}
                  onClick={(e) => followLink(e, () => go({ page: "run", runId: row.run_id }))}
                >
                  {stamp(row.run_id)}
                  <span className="sub">{row.resumable ? "paused" : `${row.shortlisted} kept`}</span>
                </a>
              ))}
            </div>
          </>
        )}
      </div>
      {hunt.sourcesError && <div className="err">{hunt.sourcesError}</div>}

      {missing && (
        <div className="notice" data-tone="warn" style={{ marginBottom: 16 }}>
          <TriangleAlert className="icon" aria-hidden="true" />
          <span className="grow">There is no saved run {stamp(missing)} any more.</span>
        </div>
      )}
      {saved && !missing && !lastRun && (
        <div className="notice" style={{ marginBottom: 16 }}>
          <Info className="icon" aria-hidden="true" />
          <span className="grow">
            Showing the run from <b>{stamp(runId ?? "")}</b>.
          </span>
          <a href="/" onClick={(e) => followLink(e, () => go({ page: "run" }))}>
            Back to live
          </a>
        </div>
      )}
      {restored && !lastRun && (
        <div className="notice" style={{ marginBottom: 16 }}>
          <Info className="icon" aria-hidden="true" />
          <span className="grow">The shortlist is from an earlier run. Start a run to refresh it.</span>
        </div>
      )}
      {run.degraded.length > 0 && (
        <div className="notice" data-tone="warn" style={{ marginBottom: 16 }}>
          <TriangleAlert className="icon" aria-hidden="true" />
          <span className="grow">
            Could not reach {run.degraded.map(sourceLabel).join(", ")} this run, so{" "}
            {run.degraded.length > 1 ? "their" : "its"} jobs are from the last successful fetch.
          </span>
        </div>
      )}

      <div className="stack">
        <Pipeline state={lastRun ? { ...run, outcome: run.outcome === "restored" ? "completed" : run.outcome } : run} />

        {run.error && (
          <section className="panel">
            <div className="panel-head">
              <h2>The run failed</h2>
            </div>
            <div className="failbox">{run.error}</div>
          </section>
        )}

        {!started && !restored && !lastRun && (
          <div className="empty-state">
            <h2>No run yet</h2>
            <p>
              A run syncs every selected source, ranks the corpus against your filters, sends the survivors
              through the fit gate, and shortlists the best. It takes a few minutes and keeps going with this
              tab closed.
            </p>
          </div>
        )}

        {(started || restored) && (
          <div className={started && !saved ? "split" : undefined}>
            {started && !saved ? (
            <section className="panel">
              <div className="panel-head">
                <h2>Sync</h2>
                <span className="note">
                  {plural(run.counters.jobs_total ?? 0, "job")} · {plural(run.counters.boards_done ?? 0, "board")}
                </span>
              </div>
              <SourceRail sources={hunt.syncSources} />
            </section>
            ) : null}

            <section className="panel">
              <div className="panel-head">
                <h2>Shortlist</h2>
                <span className="note">
                  {run.outcome === "stopped_early"
                    ? `${kept.length} jobs · stopped early, partial corpus`
                    : `${kept.length} jobs above the bar`}
                </span>
                <a
                  className="btn ghost sm"
                  href={href({ page: "shortlist", runId })}
                  onClick={(e) => followLink(e, () => go({ page: "shortlist", runId }))}
                >
                  Open shortlist
                  <ArrowRight aria-hidden="true" />
                </a>
              </div>
              {kept.length ? (
                <div className="summary-rows">
                  {kept.slice(0, 6).map((row) => (
                    <div className="summary-row" key={row.job_id}>
                      <span className="num">{row.fit != null ? row.fit.toFixed(2) : "—"}</span>
                      <span>
                        {row.title} <span className="co">· {row.company}</span>
                      </span>
                      <span className="co">
                        {row.remote_type && row.remote_type !== "unknown" ? row.remote_type : row.location}
                      </span>
                    </div>
                  ))}
                  {kept.length > 6 && <div className="hint" style={{ paddingTop: 8 }}>and {kept.length - 6} more</div>}
                </div>
              ) : (
                <div className="empty">Nothing above the bar yet.</div>
              )}
            </section>
          </div>
        )}

        {run.rank && <RankPanel report={run.rank} running={run.phase === "rank"} />}

        {run.gate && <GatePanel report={run.gate} />}

        {!saved && hunt.events.length > 0 && (
          <section className="panel">
            <div className="panel-head">
              <h2>Run feed</h2>
              <span className="note">
                last {Math.min(hunt.events.length, 200)} of {hunt.events.length} lines, every phase in order
              </span>
              <button
                type="button"
                className="btn ghost sm"
                aria-expanded={feedOpen}
                onClick={() => setFeedOpen((open) => !open)}
              >
                {feedOpen ? "Hide" : "Show"}
              </button>
            </div>
            {feedOpen ? <Log events={hunt.events} /> : <div className="hint">Collapsed. Show it to read every line.</div>}
          </section>
        )}
      </div>
    </div>
  );
}
