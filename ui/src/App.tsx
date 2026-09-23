import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import "./theme.css";
import {
  api,
  type Filters,
  type JobRun,
  type OutreachBudget,
  type OutreachStates,
  type RevisableJob,
  type RunEvent,
  type RunState,
  type RunSummary,
  type Vocab,
} from "./api";
import { FiltersPanel, SourcePicker } from "./Filters";
import { GatePanel, Log, PhaseStrip, RankPanel, Shortlist, SourceRail } from "./Run";
import { TailorBatch } from "./Tailor";
import { ReviseStudio } from "./Revise";
import { Profile } from "./Profile";
import { Templates } from "./Templates";

/* Each section of the app is a page with its own URL, so a tab can be linked,
   bookmarked, reopened, and walked back through with the browser's own back
   button. A handful of routes did not justify a router dependency. */
const PATHS = {
  hunt: "/",
  filters: "/filters",
  profile: "/profile",
  templates: "/templates",
} as const;

type Screen = keyof typeof PATHS;

function screenFor(path: string): Screen {
  const found = (Object.keys(PATHS) as Screen[]).find(
    (screen) => screen !== "hunt" && path.startsWith(PATHS[screen]),
  );
  // Anything else is the hunt page. The server only serves the shell for the
  // known paths, so an unknown one never reaches this in the first place.
  return found ?? "hunt";
}

function useScreen(): [Screen, (next: Screen) => void] {
  const [screen, setScreen] = useState<Screen>(() => screenFor(window.location.pathname));

  useEffect(() => {
    const onPop = () => setScreen(screenFor(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const go = useCallback((next: Screen) => {
    // Pushed only on a real move, so clicking the current tab twice does not
    // leave two entries that both go nowhere.
    if (window.location.pathname !== PATHS[next]) {
      window.history.pushState(null, "", PATHS[next]);
    }
    setScreen(next);
  }, []);

  return [screen, go];
}

const EMPTY_RUN: RunState = {
  phase: "idle",
  running: false,
  outcome: null,
  error: null,
  degraded: [],
  counters: {},
  rank: null,
  gate: null,
  results: [],
  resumable: false,
  run_id: "",
  started_at: "",
  finished_at: null,
  last_seq: 0,
};

export default function App() {
  const [mode, setMode] = useState(() => stored("jh-mode") ?? preferredMode());
  const [filters, setFilters] = useState<Filters | null>(null);
  const [vocab, setVocab] = useState<Vocab | null>(null);
  const [run, setRun] = useState<RunState>(EMPTY_RUN);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [applied, setApplied] = useState<Set<number>>(new Set());
  const [outreachBudget, setOutreachBudget] = useState<OutreachBudget | null>(null);
  const [outreachStates, setOutreachStates] = useState<OutreachStates>({});
  // The feed is the loudest thing on the page and the least often wanted, so
  // it starts shut and the phase strip above it carries the run's state.
  const [feedOpen, setFeedOpen] = useState(false);
  const [valid, setValid] = useState(true);
  const [screen, go] = useScreen();
  const [tailor, setTailor] = useState<{ jobs: JobRun[]; running: boolean }>({ jobs: [], running: false });
  const [revisable, setRevisable] = useState<RevisableJob[]>([]);
  const [reviewing, setReviewing] = useState<number | null>(null);
  const studio = useRef<HTMLDivElement>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [past, setPast] = useState<RunSummary[]>([]);
  // Which saved run is on screen. Empty means the live one.
  const [viewing, setViewing] = useState("");
  const results = useRef<HTMLDivElement>(null);
  // Which sources a run fetches from. It sits beside the run controls rather
  // than in the filters form, so it is owned here and written on the spot -
  // there is no Save button on this screen to defer it to.
  const [sources, setSources] = useState<string[]>([]);
  const [savingSources, setSavingSources] = useState(false);

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
      setSources(body.filters.sources?.length ? body.filters.sources : ["ats", "linkedin"]);
    });
    api.run().then(setRun);
    // Which jobs are already recorded as sent. Studio and the tailor chat show
    // jobs whose application state they cannot infer from their own payloads.
    api
      .appliedJobs()
      .then((body) => setApplied(new Set(body.applied)))
      .catch(() => {
        /* the buttons simply start unmarked; pressing one still works */
      });
    api.outreachBudget().then(setOutreachBudget).catch(() => setOutreachBudget(null));
    api
      .outreachStates()
      .then((body) => setOutreachStates(body.states))
      .catch(() => {
        /* the dots simply start neutral; a send still updates them */
      });
  }, []);

  // The drawer reports a budget every time it loads or a send changes it -
  // the natural moment to also refresh which dot each row should wear, so a
  // send never leaves a stale dot behind.
  const refreshOutreach = useCallback((budget: OutreachBudget) => {
    setOutreachBudget(budget);
    api
      .outreachStates()
      .then((body) => setOutreachStates(body.states))
      .catch(() => {
        /* leave the previous states in place rather than clearing them */
      });
  }, []);

  // The run outlives the tab, so the stream is opened whenever the page is,
  // and reconnects pick up from the last event this browser actually saw.
  useEffect(() => {
    const source = new EventSource("/api/runs/current/events");
    source.onmessage = (message) => {
      const event: RunEvent = JSON.parse(message.data);
      setEvents((current) => [...current.slice(-400), event]);
      // Rank and the gate report through the polled state, not the feed, so a
      // phase ending has to pull the state that goes with it.
      if (["rank", "gate", "shortlist", "done", "failed"].includes(event.phase)) {
        api.run().then(setRun);
      }
    };
    source.onerror = () => source.close();
    return () => source.close();
  }, [run.running]);

  useEffect(() => {
    if (!run.running || viewing) return;
    const timer = window.setInterval(() => api.run().then(setRun), 2500);
    return () => window.clearInterval(timer);
  }, [run.running, viewing]);

  // Per-board sync progress, distinct from the sources a run is allowed to
  // fetch from: this one is read off the feed while the run is going.
  const syncSources = useMemo(() => progressFrom(events), [events]);

  useEffect(() => {
    api.runs().then((body) => setPast(body.runs));
  }, [run.phase, run.outcome]);

  const showPast = useCallback(async (runId: string) => {
    if (!runId) {
      setViewing("");
      setRun(await api.run());
      return;
    }
    setViewing(runId);
    setRun(await api.pastRun(runId));
  }, []);

  useEffect(() => {
    api.tailorState().then(setTailor);
  }, []);

  // Tailoring outlives the tab too, so its state is polled while it runs.
  useEffect(() => {
    if (!tailor.running) return;
    const timer = window.setInterval(() => api.tailorState().then(setTailor), 2000);
    return () => window.clearInterval(timer);
  }, [tailor.running]);

  useEffect(() => {
    api.revisable().then((body) => setRevisable(body.jobs));
  }, [tailor.jobs, tailor.running]);

  // A CV appears in its folder partway through tailoring, and nothing announces
  // it. While any job is still being cut, the list is re-read so the studio can
  // open it the moment it lands.
  const readyIds = useMemo(
    () => new Set(revisable.filter((row) => row.ready).map((row) => row.job_id)),
    [revisable],
  );
  const awaitingCv = revisable.some((row) => !row.ready);
  useEffect(() => {
    if (!awaitingCv) return;
    const timer = window.setInterval(
      () => api.revisable().then((body) => setRevisable(body.jobs)),
      5000,
    );
    return () => window.clearInterval(timer);
  }, [awaitingCv]);

  const startTailoring = useCallback(async () => {
    const ids = [...picked];
    if (!ids.length) return;
    await api.startTailoring(ids);
    setTailor(await api.tailorState());
  }, [picked]);

  // Clearing the last source is allowed on screen - the picker says why it is
  // wrong - but never written, since an empty list would leave the next run
  // with nothing to fetch and no obvious way to tell it had been emptied.
  const pickSources = useCallback(
    async (next: string[]) => {
      const previous = sources;
      setSources(next);
      if (!next.length) return;
      setSavingSources(true);
      setNotice(null);
      try {
        const body = await api.saveFilters({ sources: next });
        setFilters(body.filters);
      } catch {
        // Nothing on this screen owns an unsaved sources list, so a rejected
        // write has to put the boxes back rather than leave them lying about
        // what the next run will fetch.
        setSources(previous);
        setNotice("could not save the sources — they are back as they were");
      } finally {
        setSavingSources(false);
      }
    },
    [sources],
  );

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

  const pause = useCallback(async () => {
    await api.pause();
    setRun(await api.run());
  }, []);

  const resume = useCallback(async () => {
    setNotice(null);
    setViewing("");
    const body = await api.resumeRun();
    if (!body.resumed) {
      setNotice(body.message ?? "there is no paused run to resume");
      return;
    }
    setRun(await api.run());
  }, []);

  const restored = run.outcome === "restored";
  const started = run.running || run.outcome !== null;
  const watched = started && !restored;
  // Near misses ride along in the same list so the table can draw the cut.
  const kept = run.results.filter((row) => !row.below_bar).length;

  return (
    <>
      <div className="topbar">
        <div className="brand">
          Job<em>hunt</em>
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

      <div className="shell">
        <nav className="sidenav" aria-label="Sections">
          <NavLink screen="hunt" current={screen} go={go}>
            Job hunt
          </NavLink>
          <NavLink screen="filters" current={screen} go={go}>
            Search filters
          </NavLink>
          <NavLink screen="profile" current={screen} go={go}>
            Profile
          </NavLink>
          <NavLink screen="templates" current={screen} go={go}>
            Templates
          </NavLink>
        </nav>

        <main>
        {screen === "profile" && <Profile onOpenTemplates={() => go("templates")} />}
        {screen === "templates" && <Templates />}

        {/* Kept mounted whichever screen is up, so an unsaved draft survives a
            trip to the hunt page and back. */}
        {filters && vocab && (
          <div style={{ display: screen === "filters" ? undefined : "none" }}>
            <FiltersPanel
              filters={filters}
              vocab={vocab}
              onSaved={setFilters}
              onValidity={setValid}
              sources={sources}
            />
          </div>
        )}

        {screen === "hunt" && (
        <>
        <div className="runbar">
        <div className="hero">
          <div className="runctl">
            {run.running ? (
              <>
                <button
                  className="btn ghost"
                  onClick={pause}
                  disabled={run.paused}
                  title="Stop at the next board and keep what has been fetched"
                >
                  {run.paused ? "Pausing" : "Pause"}
                </button>
                <button
                  className="btn stop"
                  onClick={stop}
                  disabled={run.stopping}
                  title="Kill the run. Nothing further is ranked, gated or shortlisted."
                >
                  {run.stopping ? "Stopping" : "Stop run"}
                </button>
              </>
            ) : run.resumable ? (
              <button className="btn" onClick={resume} title="Pick up where it paused">
                Resume run
              </button>
            ) : (
              <button
                className="btn"
                onClick={start}
                disabled={!valid || Boolean(viewing)}
                title={
                  viewing
                    ? "Showing a saved run — go back to the live one to start"
                    : valid
                      ? ""
                      : "Fix the filters before starting a run"
                }
              >
                Start run
              </button>
            )}
          </div>
          {filters && (
            <SourcePicker value={sources} onChange={pickSources} saving={savingSources} />
          )}
          <div className="runstate" data-state={stateOf(run)}>
            <span className="pulse" />
            <span>{statusText(run)}</span>
          </div>
        </div>

        {notice && <div className="err">{notice}</div>}

        {past.length > 0 && (
          <div className="runpicker">
            <span className="lbl">Runs</span>
            <button
              className="rchip"
              type="button"
              aria-pressed={!viewing}
              onClick={() => showPast("")}
            >
              Live
            </button>
            {past.map((row) => (
              <button
                className="rchip"
                type="button"
                key={row.run_id}
                aria-pressed={viewing === row.run_id}
                data-outcome={row.outcome ?? row.phase}
                onClick={() => showPast(row.run_id)}
                title={`${row.jobs_total.toLocaleString()} jobs fetched`}
              >
                {stamp(row.run_id)}
                <span className="sub">
                  {row.resumable ? "paused" : `${row.shortlisted} shortlisted`}
                </span>
              </button>
            ))}
          </div>
        )}
        </div>

        {viewing && (
          <div className="fx">
            Showing the run from <b>{stamp(viewing)}</b>, read back from disk. Nothing here is
            live.
          </div>
        )}

        {started && (
          <div ref={results} style={{ marginTop: "var(--gap)" }}>
            {restored && (
              <div className="fx">
                Showing the shortlist on file from the last run. Nothing is running now —
                <b> start a run</b> to refresh it.
              </div>
            )}

            {watched && <PhaseStrip state={run} />}

            {watched && (
            <div className="panel">
              <div className="panel-head">
                <h2>Sync</h2>
                <div className="note">
                  {(run.counters.jobs_total ?? 0).toLocaleString()} jobs ·{" "}
                  {(run.counters.boards_done ?? 0).toLocaleString()} boards
                </div>
              </div>
              <SourceRail sources={syncSources} />
              {run.degraded.length > 0 && (
                <div className="hint" style={{ marginTop: 12 }}>
                  degraded this run: {run.degraded.join(", ")} — their jobs stay from the last successful
                  fetch
                </div>
              )}
            </div>
            )}

            {run.rank && <RankPanel report={run.rank} running={run.phase === "rank"} />}

            {run.gate && <GatePanel report={run.gate} />}

            {events.length > 0 && (
              <div className="panel">
                <div className="panel-head">
                  <h2>Run feed</h2>
                  <div className="note">
                    {events.length} {events.length === 1 ? "line" : "lines"} · every phase, in order
                  </div>
                  <button
                    type="button"
                    className="btn sm ghost"
                    aria-expanded={feedOpen}
                    onClick={() => setFeedOpen((open) => !open)}
                  >
                    {feedOpen ? "Hide" : "Show"}
                  </button>
                </div>
                {feedOpen && <Log events={events} />}
              </div>
            )}

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
                    ? `${kept} jobs · stopped early, partial corpus`
                    : `${kept} jobs above the bar`}
                </div>
                {outreachBudget && (
                  <div className="note">
                    {outreachBudget.invites_used}/{outreachBudget.invites_max} invites ·{" "}
                    {outreachBudget.dms_used}/{outreachBudget.dms_max} messages ·{" "}
                    {outreachBudget.credits} InMail credits
                  </div>
                )}
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
                applied={applied}
                onApplied={(id, on) =>
                  setApplied((current) => {
                    const next = new Set(current);
                    on ? next.add(id) : next.delete(id);
                    return next;
                  })
                }
                picked={picked}
                bar={run.gate?.plan.bar ?? null}
                onPick={(id, on) =>
                  setPicked((current) => {
                    const next = new Set(current);
                    on ? next.add(id) : next.delete(id);
                    return next;
                  })
                }
                onPickAll={(on) => setPicked(on ? new Set(run.results.map((r) => r.job_id)) : new Set())}
                outreachStates={outreachStates}
                onBudget={refreshOutreach}
              />
            </div>

            <TailorBatch
              applied={applied}
                onApplied={(id, on) =>
                  setApplied((current) => {
                    const next = new Set(current);
                    on ? next.add(id) : next.delete(id);
                    return next;
                  })
                }
              jobs={tailor.jobs}
              running={tailor.running}
              onStop={async () => {
                await api.stopTailoring();
                setTailor(await api.tailorState());
              }}
              readyIds={readyIds}
              onReview={(jobId) => {
                setReviewing(jobId);
                window.setTimeout(
                  () => studio.current?.scrollIntoView({ behavior: "smooth", block: "start" }),
                  60,
                );
              }}
            />

            <div ref={studio}>
              <ReviseStudio
                jobs={revisable}
                focus={reviewing}
                applied={applied}
                onApplied={(id, on) =>
                  setApplied((current) => {
                    const next = new Set(current);
                    on ? next.add(id) : next.delete(id);
                    return next;
                  })
                }
                outreachStates={outreachStates}
                onBudget={refreshOutreach}
              />
            </div>
          </div>
        )}
        </>
        )}
        </main>
      </div>
    </>
  );
}

/* A real anchor, not a button: the tabs are pages now, so opening one in a new
   tab or copying its address has to work the way it does anywhere else. Only a
   plain left click is intercepted. */
function NavLink({
  screen,
  current,
  go,
  children,
}: {
  screen: Screen;
  current: Screen;
  go: (next: Screen) => void;
  children: ReactNode;
}) {
  return (
    <a
      className="tab"
      href={PATHS[screen]}
      aria-current={current === screen ? "page" : undefined}
      onClick={(event) => {
        if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) {
          return;
        }
        event.preventDefault();
        go(screen);
      }}
    >
      {children}
    </a>
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
  if (run.phase === "paused" || run.outcome === "killed") return "stopped";
  if (run.running) return "running";
  if (run.outcome === "restored") return "idle";
  if (run.outcome) return "stopped";
  return "idle";
}

function statusText(run: RunState) {
  if (run.phase === "paused") return "Paused · resume picks it up here";
  if (run.outcome === "killed") return "Stopped · nothing further was run";
  if (run.outcome === "restored") return "Idle · showing the last shortlist";
  if (run.phase === "failed") return "Failed";
  if (run.stopping) return "Stopping after this board";
  if (run.running) return "Running · the tab can be closed";
  if (run.outcome === "stopped_early") return "Stopped early · partial results kept";
  if (run.outcome === "completed") return "Run complete";
  return "Idle";
}

/** A run id is its start time: "20260822-100000". */
function stamp(runId: string) {
  const match = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})/.exec(runId);
  if (!match) return runId;
  const [, , month, day, hour, minute] = match;
  return `${day}/${month} ${hour}:${minute}`;
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
