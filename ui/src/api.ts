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
};

export type RunState = {
  phase: string;
  running: boolean;
  outcome: string | null;
  error: string | null;
  degraded: string[];
  counters: Record<string, number>;
  results: ShortlistRow[];
  stopping?: boolean;
  last_seq: number;
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
  findings: string[];
  error: string | null;
  title?: string;
  company?: string;
};

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
