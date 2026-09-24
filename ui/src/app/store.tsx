import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
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
} from "../api";
import { cvApi, type ImportState, type UploadState } from "../cv/api";

/* The work this app watches outlives any one page: a run, a tailoring batch, an
   agent editing a CV, a CV import or a template upload. Their state lives here,
   above the pages, so the activity panel can show all of it from anywhere and a
   page switch never loses track of what is running. */

export const EMPTY_RUN: RunState = {
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

export type SourceProgress = { done: number; total: number; jobs: number; status: string };
export type Tailor = { jobs: JobRun[]; running: boolean };
type Desk = { state: "idle" | "running" | "done" | "failed"; label: string };

type Hunt = {
  filters: Filters | null;
  vocab: Vocab | null;
  setFilters: (filters: Filters) => void;
  valid: boolean;
  setValid: (valid: boolean) => void;
  /** Which Filters pages hold edits that are not saved yet. */
  filtersDirty: { boards: boolean; upwork: boolean };
  setFiltersDirty: (dirty: { boards: boolean; upwork: boolean }) => void;
  sources: string[];
  pickSources: (next: string[]) => Promise<void>;
  savingSources: boolean;
  sourcesError: string | null;

  run: RunState;
  events: RunEvent[];
  syncSources: Record<string, SourceProgress>;
  past: RunSummary[];
  refreshPast: () => Promise<void>;
  runError: string | null;
  start: () => Promise<boolean>;
  stop: () => Promise<void>;
  pause: () => Promise<void>;
  resume: () => Promise<void>;

  tailor: Tailor;
  refreshTailor: () => Promise<void>;
  stopTailoring: () => Promise<void>;
  revisable: RevisableJob[];
  readyIds: Set<number>;

  applied: Set<number>;
  setApplied: (jobId: number, applied: boolean) => void;
  outreachBudget: OutreachBudget | null;
  outreachStates: OutreachStates;
  refreshOutreach: (budget: OutreachBudget) => void;

  picked: Set<number>;
  setPicked: (next: Set<number>) => void;

  desks: { import: Desk; upload: Desk };
  refreshDesks: () => void;
};

const HuntContext = createContext<Hunt | null>(null);

export function useHunt(): Hunt {
  const hunt = useContext(HuntContext);
  if (!hunt) throw new Error("useHunt outside HuntProvider");
  return hunt;
}

export function HuntProvider({ children }: { children: ReactNode }) {
  const [filters, setFilters] = useState<Filters | null>(null);
  const [vocab, setVocab] = useState<Vocab | null>(null);
  const [valid, setValid] = useState(true);
  const [filtersDirty, setFiltersDirtyState] = useState({ boards: false, upwork: false });
  const setFiltersDirty = useCallback(
    (next: { boards: boolean; upwork: boolean }) =>
      setFiltersDirtyState((current) =>
        current.boards === next.boards && current.upwork === next.upwork ? current : next,
      ),
    [],
  );
  const [sources, setSources] = useState<string[]>([]);
  const [savingSources, setSavingSources] = useState(false);
  const [sourcesError, setSourcesError] = useState<string | null>(null);

  const [run, setRun] = useState<RunState>(EMPTY_RUN);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [past, setPast] = useState<RunSummary[]>([]);
  const [runError, setRunError] = useState<string | null>(null);

  const [tailor, setTailor] = useState<Tailor>({ jobs: [], running: false });
  const [revisable, setRevisable] = useState<RevisableJob[]>([]);

  const [applied, setAppliedSet] = useState<Set<number>>(new Set());
  const [outreachBudget, setOutreachBudget] = useState<OutreachBudget | null>(null);
  const [outreachStates, setOutreachStates] = useState<OutreachStates>({});
  const [picked, setPicked] = useState<Set<number>>(new Set());

  const [importDesk, setImportDesk] = useState<ImportState | null>(null);
  const [uploadDesk, setUploadDesk] = useState<UploadState | null>(null);

  useEffect(() => {
    api
      .filters()
      .then((body) => {
        setFilters(body.filters);
        setVocab(body.vocab);
        setSources(body.filters.sources?.length ? body.filters.sources : ["ats", "linkedin"]);
      })
      .catch((error) => setSourcesError(`could not load the search filters: ${String(error)}`));
    api.run().then(setRun).catch(() => undefined);
    api.tailorState().then(setTailor).catch(() => undefined);
    api
      .appliedJobs()
      .then((body) => setAppliedSet(new Set(body.applied)))
      .catch(() => undefined);
    api.outreachBudget().then(setOutreachBudget).catch(() => setOutreachBudget(null));
    api
      .outreachStates()
      .then((body) => setOutreachStates(body.states))
      .catch(() => undefined);
  }, []);

  // The stream is opened whenever a run is going, and a phase ending pulls the
  // state that goes with it: rank and the gate report through the polled
  // state, not the feed.
  useEffect(() => {
    const source = new EventSource("/api/runs/current/events");
    source.onmessage = (message) => {
      const event: RunEvent = JSON.parse(message.data);
      setEvents((current) => [...current.slice(-400), event]);
      if (["rank", "gate", "shortlist", "done", "failed", "paused", "stopped"].includes(event.phase)) {
        api.run().then(setRun).catch(() => undefined);
      }
    };
    source.onerror = () => source.close();
    return () => source.close();
  }, [run.running]);

  useEffect(() => {
    if (!run.running) return;
    const timer = window.setInterval(() => api.run().then(setRun).catch(() => undefined), 2500);
    return () => window.clearInterval(timer);
  }, [run.running]);

  const refreshPast = useCallback(
    () =>
      api
        .runs()
        .then((body) => setPast(body.runs))
        .catch(() => undefined),
    [],
  );
  useEffect(() => {
    void refreshPast();
  }, [run.phase, run.outcome, refreshPast]);

  const syncSources = useMemo(() => progressFrom(events), [events]);

  const guarded = useCallback(async (work: () => Promise<void>) => {
    setRunError(null);
    try {
      await work();
    } catch (error) {
      setRunError(error instanceof Error ? error.message : String(error));
    }
  }, []);

  const start = useCallback(async () => {
    let started = false;
    await guarded(async () => {
      setEvents([]);
      const body = await api.start();
      if (!body.started) {
        setRunError(body.message ?? "A run is already going.");
        return;
      }
      started = true;
      setRun(await api.run());
    });
    return started;
  }, [guarded]);

  const stop = useCallback(
    () =>
      guarded(async () => {
        await api.stop();
        setRun(await api.run());
      }),
    [guarded],
  );

  const pause = useCallback(
    () =>
      guarded(async () => {
        await api.pause();
        setRun(await api.run());
      }),
    [guarded],
  );

  const resume = useCallback(
    () =>
      guarded(async () => {
        const body = await api.resumeRun();
        if (!body.resumed) {
          setRunError(body.message ?? "There is no paused run to resume.");
          return;
        }
        setRun(await api.run());
      }),
    [guarded],
  );

  // Clearing the last source is allowed on screen, where the picker says why it
  // is wrong, but never written: an empty list would leave the next run with
  // nothing to fetch.
  const pickSources = useCallback(
    async (next: string[]) => {
      const previous = sources;
      setSources(next);
      if (!next.length) return;
      setSavingSources(true);
      setSourcesError(null);
      try {
        const body = await api.saveFilters({ sources: next });
        setFilters(body.filters);
      } catch {
        setSources(previous);
        setSourcesError("Could not save the sources. They are back as they were.");
      } finally {
        setSavingSources(false);
      }
    },
    [sources],
  );

  const refreshTailor = useCallback(async () => {
    setTailor(await api.tailorState());
  }, []);

  const stopTailoring = useCallback(async () => {
    await api.stopTailoring();
    setTailor(await api.tailorState());
  }, []);

  useEffect(() => {
    if (!tailor.running) return;
    const timer = window.setInterval(() => api.tailorState().then(setTailor).catch(() => undefined), 2000);
    return () => window.clearInterval(timer);
  }, [tailor.running]);

  useEffect(() => {
    api
      .revisable()
      .then((body) => setRevisable(body.jobs))
      .catch(() => undefined);
  }, [tailor.jobs, tailor.running]);

  // A CV lands in its folder partway through tailoring and nothing announces
  // it, so while any job is still being cut the list is read again.
  const awaitingCv = revisable.some((row) => !row.ready);
  useEffect(() => {
    if (!awaitingCv) return;
    const timer = window.setInterval(
      () =>
        api
          .revisable()
          .then((body) => setRevisable(body.jobs))
          .catch(() => undefined),
      5000,
    );
    return () => window.clearInterval(timer);
  }, [awaitingCv]);

  const readyIds = useMemo(
    () => new Set(revisable.filter((row) => row.ready).map((row) => row.job_id)),
    [revisable],
  );

  const setApplied = useCallback((jobId: number, on: boolean) => {
    setAppliedSet((current) => {
      const next = new Set(current);
      if (on) next.add(jobId);
      else next.delete(jobId);
      return next;
    });
  }, []);

  // Every time an outreach panel loads or a send changes the budget is also the
  // moment to refresh which state each row shows, so no row keeps a stale one.
  const refreshOutreach = useCallback((budget: OutreachBudget) => {
    setOutreachBudget(budget);
    api
      .outreachStates()
      .then((body) => setOutreachStates(body.states))
      .catch(() => undefined);
  }, []);

  const refreshDesks = useCallback(() => {
    cvApi.importState().then(setImportDesk).catch(() => undefined);
    cvApi.uploadState().then(setUploadDesk).catch(() => undefined);
  }, []);

  useEffect(refreshDesks, [refreshDesks]);

  const deskRunning = importDesk?.state === "running" || uploadDesk?.state === "running";
  useEffect(() => {
    if (!deskRunning) return;
    const timer = window.setInterval(refreshDesks, 3000);
    return () => window.clearInterval(timer);
  }, [deskRunning, refreshDesks]);

  const desks = useMemo(
    () => ({
      import: { state: importDesk?.state ?? "idle", label: "master.tex" } as Desk,
      upload: { state: uploadDesk?.state ?? "idle", label: uploadDesk?.filename || "template" } as Desk,
    }),
    [importDesk, uploadDesk],
  );

  const value: Hunt = {
    filters,
    vocab,
    setFilters,
    valid,
    setValid,
    filtersDirty,
    setFiltersDirty,
    sources,
    pickSources,
    savingSources,
    sourcesError,
    run,
    events,
    syncSources,
    past,
    refreshPast,
    runError,
    start,
    stop,
    pause,
    resume,
    tailor,
    refreshTailor,
    stopTailoring,
    revisable,
    readyIds,
    applied,
    setApplied,
    outreachBudget,
    outreachStates,
    refreshOutreach,
    picked,
    setPicked,
    desks,
    refreshDesks,
  };

  return <HuntContext.Provider value={value}>{children}</HuntContext.Provider>;
}

/** A saved run read back from disk, or the live one when no id is given. A saved
 *  run is held apart from the live state, so a live event can never overwrite
 *  the run being looked at. */
export function useShownRun(runId?: string): { run: RunState; saved: boolean; missing: string | null } {
  const { run } = useHunt();
  const [saved, setSaved] = useState<RunState | null>(null);
  const [missing, setMissing] = useState<string | null>(null);

  useEffect(() => {
    setSaved(null);
    setMissing(null);
    if (!runId) return;
    let live = true;
    api
      .pastRun(runId)
      .then((body) => live && setSaved(body))
      .catch(() => live && setMissing(runId));
    return () => {
      live = false;
    };
  }, [runId]);

  if (runId && saved) return { run: saved, saved: true, missing: null };
  if (runId) return { run: EMPTY_RUN, saved: true, missing };
  return { run, saved: false, missing: null };
}

function progressFrom(events: RunEvent[]) {
  const sources: Record<string, SourceProgress> = {};
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

/** What a run is called on screen: its given name, or when it started. */
export function runLabel(past: RunSummary[], runId: string): string {
  return past.find((row) => row.run_id === runId)?.name || stamp(runId);
}

/** A run id is its start time: "20260822-100000". */
export function stamp(runId: string) {
  const match = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})/.exec(runId);
  if (!match) return runId;
  const [, , month, day, hour, minute] = match;
  return `${day}/${month} ${hour}:${minute}`;
}

export type RunTone = "idle" | "running" | "paused" | "stopped" | "failed" | "done";

export function runTone(run: RunState): RunTone {
  if (run.phase === "failed") return "failed";
  if (run.phase === "paused") return "paused";
  if (run.running) return "running";
  if (run.outcome === "killed" || run.outcome === "stopped_early") return "stopped";
  if (run.outcome === "completed") return "done";
  return "idle";
}

export function runStatus(run: RunState): string {
  if (run.phase === "paused") return "Paused";
  if (run.outcome === "killed") return "Stopped";
  if (run.outcome === "restored") return "Idle";
  if (run.phase === "failed") return "Failed";
  if (run.stopping) return "Stopping";
  if (run.paused) return "Pausing";
  if (run.running) return "Running";
  if (run.outcome === "stopped_early") return "Stopped early";
  if (run.outcome === "completed") return "Complete";
  return "Idle";
}

export const PHASE_LABEL: Record<string, string> = {
  sync: "Sync",
  rank: "Rank",
  gate: "Fit gate",
  shortlist: "Shortlist",
};
