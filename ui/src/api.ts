export type Filters = {
  titles: string[];
  locations: string[];
  work_model: string[];
  job_types: string[];
  experience_min: string | null;
  experience_max: string | null;
  min_salary: number | null;
  currency: string;
  include_unstated_salary: boolean;
  max_age_days: number;
  top_n: number;
};

export type VocabRow = { value: string; label: string; count: number };
export type Vocab = { titles: VocabRow[]; locations: VocabRow[]; active_jobs: number };

export type ShortlistRow = {
  job_id: number;
  title: string;
  company: string;
  location: string;
  remote_type: string;
  fit: number | null;
  reasoning: string | null;
  url: string | null;
  source: string;
  salary: string | null;
  // A job the surface threshold turned away. It rides along in the same list so
  // the table can draw the cut, and it was never exported to the CSV.
  below_bar: boolean;
};

export type DropReason = { code: string; label: string; count: number; tunable: boolean };

export type RankReport = {
  corpus: number;
  scored: number;
  passed: number;
  failed: number;
  skipped: number;
  by_market: Record<string, number>;
  // Heaviest first. A job failing three rules is counted under all three.
  reasons: DropReason[];
  fx_source: string | null;
  fx_age_hours: number | null;
};

export type GateBatch = { label: string; size: number; status: string };

export type GatePlan = {
  batches: GateBatch[];
  jobs: number;
  held_by_company_cap: number;
  bar: number | null;
};

export type GateVerdict = {
  job_id: number;
  title: string;
  company: string;
  source: string;
  market: string;
  score: number | null;
  reasoning: string;
  red_flags: string[];
};

export type GateReport = {
  plan: GatePlan;
  scored: number;
  ungated: number;
  verdicts: GateVerdict[];
};

export type RunState = {
  phase: string;
  running: boolean;
  outcome: string | null;
  error: string | null;
  degraded: string[];
  counters: Record<string, number>;
  rank: RankReport | null;
  gate: GateReport | null;
  results: ShortlistRow[];
  stopping?: boolean;
  /** Set while a pause has been asked for but the run has not reached a checkpoint. */
  paused?: boolean;
  resumable: boolean;
  run_id: string;
  started_at: string;
  finished_at: string | null;
  last_seq: number;
};

/** One row in the run picker. */
export type RunSummary = {
  run_id: string;
  started_at: string | null;
  finished_at: string | null;
  phase: string;
  outcome: string | null;
  shortlisted: number;
  jobs_total: number;
  resumable: boolean;
};

export type RunEvent = {
  seq: number;
  phase: string;
  message: string;
  source?: string;
  boards_done?: number;
  boards_total?: number;
  jobs_new?: number;
  jobs_total?: number;
  level?: string;
};

export type JobRun = {
  job_id: number;
  state: string;
  rounds: number;
  folder: string | null;
  fit: number | null;
  pages: number | null;
  findings: string[];
  error: string | null;
  title: string;
  company: string;
};

/** A job the studio can open, plus how far its draft has drifted.
 *  `ready` is false while the CV is still being cut: the folder exists from the
 *  moment tailoring starts, the cv.tex inside it does not. */
export type RevisableJob = JobRun & { ahead: number; opened: boolean; ready: boolean };

export type ReviseTurn = {
  role: "you" | "agent";
  text: string;
  /** What the agent decided the message was. A question gets an answer, not a rewrite. */
  kind: "answer" | "edit" | "refusal";
  changes: string[];
  build: "ok" | "failed" | null;
  pages: number | null;
  version: number | null;
  log: string | null;
  refused: boolean;
};

export type ReviseSession = {
  job_id: number;
  folder: string;
  title: string;
  company: string;
  fit: number | null;
  turns: ReviseTurn[];
  version: number;
  synced_version: number;
  ahead: number;
  pages: number | null;
  over_length: boolean;
  thinking: boolean;
  error: string | null;
};

export type RevisePreview = { ok: boolean; log: string; pdf: string | null; pages: number | null };

export type BackupRow = { name: string; taken_at: string; size: number };

export class FieldError extends Error {
  field: string;
  constructor(field: string, message: string) {
    super(message);
    this.field = field;
  }
}

async function json<T>(response: Response): Promise<T> {
  if (response.status === 422) {
    const body = await response.json();
    throw new FieldError(body.field ?? "filters", body.message ?? "that value was refused");
  }
  if (!response.ok) throw new Error(await response.text());
  return (await response.json()) as T;
}

export const api = {
  async filters(): Promise<{ filters: Filters; vocab: Vocab }> {
    return json(await fetch("/api/filters"));
  },
  async saveFilters(payload: Record<string, unknown>) {
    return json<{ filters: Filters; title_impact: { matched: number; total: number } }>(
      await fetch("/api/filters", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }),
    );
  },
  async run(): Promise<RunState> {
    return json(await fetch("/api/runs/current"));
  },
  async start(): Promise<{ started: boolean; message?: string }> {
    const response = await fetch("/api/runs", { method: "POST" });
    if (response.status === 409) return response.json();
    return json(response);
  },
  async stop(): Promise<{ stop_requested: boolean }> {
    return json(await fetch("/api/runs/current/stop", { method: "POST" }));
  },
  async pause(): Promise<{ paused: boolean; reason?: string }> {
    return json(await fetch("/api/runs/current/pause", { method: "POST" }));
  },
  async resumeRun(): Promise<{ resumed: boolean; message?: string }> {
    const response = await fetch("/api/runs/current/resume", { method: "POST" });
    if (response.status === 409) return response.json();
    return json(response);
  },
  async runs(): Promise<{ runs: RunSummary[] }> {
    return json(await fetch("/api/runs"));
  },
  async pastRun(runId: string): Promise<RunState> {
    return json(await fetch(`/api/runs/${runId}`));
  },

  async tailorState(): Promise<{ jobs: JobRun[]; running: boolean }> {
    return json(await fetch("/api/tailor"));
  },
  async startTailoring(jobIds: number[]) {
    const response = await fetch("/api/tailor", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_ids: jobIds }),
    });
    if (response.status === 409) return response.json();
    return json<{ started: boolean; jobs: JobRun[] }>(response);
  },
  async stopTailoring() {
    return json<{ stop_requested: boolean }>(await fetch("/api/tailor/stop", { method: "POST" }));
  },

  async revisable(): Promise<{ jobs: RevisableJob[] }> {
    return json(await fetch("/api/revise"));
  },
  async openRevision(jobId: number): Promise<ReviseSession> {
    return json(await fetch(`/api/revise/${jobId}`, { method: "POST" }));
  },
  async revision(jobId: number): Promise<ReviseSession> {
    return json(await fetch(`/api/revise/${jobId}`));
  },
  async sendRevision(jobId: number, message: string): Promise<ReviseSession> {
    return json(
      await fetch(`/api/revise/${jobId}/message`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message }),
      }),
    );
  },
  async revisionPreview(jobId: number): Promise<RevisePreview> {
    return json(await fetch(`/api/revise/${jobId}/preview`));
  },
  async syncRevision(jobId: number): Promise<ReviseSession> {
    return json(await fetch(`/api/revise/${jobId}/sync`, { method: "POST" }));
  },
  async discardRevision(jobId: number): Promise<ReviseSession> {
    return json(await fetch(`/api/revise/${jobId}/discard`, { method: "POST" }));
  },

  async profile(): Promise<{ path: string; text: string; modified_at: string }> {
    return json(await fetch("/api/profile"));
  },
  async saveProfile(text: string) {
    return json<{ saved: boolean; backup: string | null }>(
      await fetch("/api/profile", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      }),
    );
  },
  async backups(): Promise<{ backups: BackupRow[] }> {
    return json(await fetch("/api/profile/backups"));
  },
  async restoreBackup(name: string) {
    return json<{ restored: boolean }>(
      await fetch("/api/profile/restore", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      }),
    );
  },
  async compileProfile(text: string): Promise<{ ok: boolean; log: string; pdf: string | null }> {
    return json(
      await fetch("/api/profile/compile", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      }),
    );
  },
};
