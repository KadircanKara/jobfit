import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import "./theme.css";
import { api, type Filters, type JobRun, type RunEvent, type RunState, type Vocab } from "./api";
import { FiltersPanel } from "./Filters";
import { Log, PhaseStrip, Shortlist, SourceRail } from "./Run";
import { TailorBatch } from "./Tailor";
import { Profile } from "./Profile";

const EMPTY_RUN: RunState = {
  phase: "idle",
  running: false,
  outcome: null,
  error: null,
  degraded: [],
  counters: {},
  results: [],
  last_seq: 0,
};

export default function App() {
  const [mode, setMode] = useState(() => stored("jh-mode") ?? preferredMode());
  const [filters, setFilters] = useState<Filters | null>(null);
  const [vocab, setVocab] = useState<Vocab | null>(null);
  const [run, setRun] = useState<RunState>(EMPTY_RUN);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [valid, setValid] = useState(true);
  const [screen, setScreen] = useState<"hunt" | "profile">("hunt");
  const [tailor, setTailor] = useState<{ jobs: JobRun[]; running: boolean }>({ jobs: [], running: false });
  const [notice, setNotice] = useState<string | null>(null);
  const results = useRef<HTMLDivElement>(null);

  useEffect(() => {
    document.documentElement.dataset.mode = mode;
    try {
      localStorage.setItem("jh-mode", mode);
    } catch {
      /* private browsing; the choice just will not stick */
    }
  }, [mode]);

  useEffect(() => {
    api.filters().then((body) => {
      setFilters(body.filters);
      setVocab(body.vocab);
    });
    api.run().then(setRun);
  }, []);

  // The run outlives the tab, so the stream is opened whenever the page is,
  // and reconnects pick up from the last event this browser actually saw.
  useEffect(() => {
    const source = new EventSource("/api/runs/current/events");
    source.onmessage = (message) => {
      const event: RunEvent = JSON.parse(message.data);
      setEvents((current) => [...current.slice(-400), event]);
      if (["done", "failed", "shortlist"].includes(event.phase)) {
        api.run().then(setRun);
      }
    };
    source.onerror = () => source.close();
    return () => source.close();
  }, [run.running]);

  useEffect(() => {
    if (!run.running) return;
    const timer = window.setInterval(() => api.run().then(setRun), 2500);
    return () => window.clearInterval(timer);
  }, [run.running]);

  const sources = useMemo(() => progressFrom(events), [events]);

  useEffect(() => {
    api.tailorState().then(setTailor);
  }, []);

  // Tailoring outlives the tab too, so its state is polled while it runs.
  useEffect(() => {
    if (!tailor.running) return;
    const timer = window.setInterval(() => api.tailorState().then(setTailor), 2000);
    return () => window.clearInterval(timer);
  }, [tailor.running]);

  const startTailoring = useCallback(async () => {
    const ids = [...picked];
    if (!ids.length) return;
    await api.startTailoring(ids);
    setTailor(await api.tailorState());
  }, [picked]);

  const start = useCallback(async () => {
    setNotice(null);
    setEvents([]);
    const body = await api.start();
    if (!body.started) {
      setNotice(body.message ?? "a run is already going");
      return;
    }
    const state = await api.run();
    setRun(state);
    window.setTimeout(() => results.current?.scrollIntoView({ behavior: "smooth", block: "start" }), 80);
  }, []);

  const stop = useCallback(async () => {
    await api.stop();
    setRun(await api.run());
  }, []);

  const started = run.running || run.outcome !== null;

  return (
    <>
      <div className="topbar">
        <div className="brand">
          Job<em>hunt</em>
        </div>
        <div className="tabs" role="tablist">
          <button
            className="tab"
            role="tab"
            aria-selected={screen === "hunt"}
            onClick={() => setScreen("hunt")}
          >
            Job hunt
          </button>
          <button
            className="tab"
            role="tab"
            aria-selected={screen === "profile"}
            onClick={() => setScreen("profile")}
          >
            Profile
          </button>
        </div>
        <div className="modesw">
          <button aria-pressed={mode === "light"} onClick={() => setMode("light")}>
            Light
          </button>
          <button aria-pressed={mode === "dark"} onClick={() => setMode("dark")}>
            Dark
          </button>
        </div>
      </div>

      <main>
        {screen === "profile" && <Profile />}
        {screen === "hunt" && (
        <>
        <div className="hero">
          <div>
            <h1>Run the search</h1>
            <div className="sub">{subtitle(run)}</div>
          </div>
          <div className="runstate" data-state={stateOf(run)}>
            <span className="pulse" />
            <span>{statusText(run)}</span>
          </div>
          <div className="runctl">
            {run.running ? (
              <button className="btn stop" onClick={stop} disabled={run.stopping}>
                {run.stopping ? "Stopping" : "Stop run"}
              </button>
            ) : (
              <button
                className="btn"
                onClick={start}
                disabled={!valid}
                title={valid ? "" : "Fix the filters before starting a run"}
              >
                Start run
              </button>
            )}
          </div>
        </div>

        {notice && <div className="err">{notice}</div>}

        {filters && vocab && (
          <FiltersPanel filters={filters} vocab={vocab} onSaved={setFilters} onValidity={setValid} />
        )}

        {started && (
          <div ref={results} style={{ marginTop: "var(--gap)" }}>
            <PhaseStrip state={run} />

            <div className="panel">
              <div className="panel-head">
                <h2>Sources</h2>
                <div className="note">
                  {(run.counters.jobs_total ?? 0).toLocaleString()} jobs ·{" "}
                  {(run.counters.boards_done ?? 0).toLocaleString()} boards
                </div>
              </div>
              <SourceRail sources={sources} />
              <Log events={events} />
              {run.degraded.length > 0 && (
                <div className="hint" style={{ marginTop: 12 }}>
                  degraded this run: {run.degraded.join(", ")} — their jobs stay from the last successful
                  fetch
                </div>
              )}
            </div>

            {run.error && (
              <div className="panel">
                <div className="panel-head">
                  <h2>The run failed</h2>
                </div>
                <div className="failbox">{run.error}</div>
              </div>
            )}

            <div className="panel">
              <div className="panel-head">
                <h2>Shortlist</h2>
                <div className="note">
                  {run.outcome === "stopped_early"
                    ? `${run.results.length} jobs · stopped early, partial corpus`
                    : `${run.results.length} jobs above the bar`}
                </div>
              </div>

              {picked.size > 0 && (
                <div className="selbar">
                  <span className="count">
                    <b>{picked.size}</b> selected
                  </span>
                  {picked.size > 5 && (
                    <span className="warn">
                      Each one is a full tailoring run plus up to three reviewer rounds. {picked.size} will
                      take a while.
                    </span>
                  )}
                  <span className="acts">
                    <button className="btn ghost" onClick={() => setPicked(new Set())}>
                      Clear
                    </button>
                    <button className="btn" onClick={startTailoring} disabled={tailor.running}>
                      Tailor selected
                    </button>
                  </span>
                </div>
              )}

              <Shortlist
                rows={run.results}
                picked={picked}
                onPick={(id, on) =>
                  setPicked((current) => {
                    const next = new Set(current);
                    on ? next.add(id) : next.delete(id);
                    return next;
                  })
                }
                onPickAll={(on) => setPicked(on ? new Set(run.results.map((r) => r.job_id)) : new Set())}
              />
            </div>

            <TailorBatch
              jobs={tailor.jobs}
              running={tailor.running}
              onStop={async () => {
                await api.stopTailoring();
                setTailor(await api.tailorState());
              }}
            />
          </div>
        )}
        </>
        )}
      </main>
    </>
  );
}

function progressFrom(events: RunEvent[]) {
  const sources: Record<string, { done: number; total: number; jobs: number; status: string }> = {};
  for (const event of events) {
    if (event.phase !== "sync" || !event.source) continue;
    const row = sources[event.source] ?? { done: 0, total: 0, jobs: 0, status: "active" };
    row.done = event.boards_done ?? row.done;
    row.total = event.boards_total ?? row.total;
    row.jobs = event.jobs_total ?? row.jobs;
    row.status = event.level === "warning" ? "degraded" : row.done >= row.total ? "done" : "active";
    sources[event.source] = row;
  }
  return sources;
}

function stateOf(run: RunState) {
  if (run.phase === "failed") return "failed";
  if (run.running) return "running";
  if (run.outcome) return "stopped";
  return "idle";
}

function statusText(run: RunState) {
  if (run.phase === "failed") return "Failed";
  if (run.stopping) return "Stopping after this board";
  if (run.running) return "Running · the tab can be closed";
  if (run.outcome === "stopped_early") return "Stopped early · partial results kept";
  if (run.outcome === "completed") return "Run complete";
  return "Idle";
}

function subtitle(run: RunState) {
  if (run.running) return "a run is going · closing this tab will not stop it";
  if (run.outcome) return "last run finished · start another when you want";
  return "filters ready · nothing running";
}

function preferredMode() {
  return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function stored(key: string) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
