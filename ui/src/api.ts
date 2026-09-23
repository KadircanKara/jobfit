// Mirrors preferences.UpworkPreferences: a different market's vocabulary,
// kept as its own object rather than folded into the shared fields above.
export type UpworkFilters = {
  queries: string[];
  job_types: string[];
  min_hourly: number | null;
  min_fixed: number | null;
  experience_level: string[];
  sort: string;
  verified_payment_only: boolean;
  require_verified_client: boolean;
  require_client_spend: boolean;
  workload: string[];
  proposals_max: number | null;
  client_min_hires: number | null;
  max_pages: number;
};

export type Filters = {
  titles: string[];
  locations: string[];
  work_model: string[];
  job_types: string[];
  sources: string[];
  experience_min: string | null;
  experience_max: string | null;
  min_salary: number | null;
  currency: string;
  include_unstated_salary: boolean;
  max_age_days: number;
  top_n: number;
  // Named selections of `titles`, so a set worth returning to can be picked
  // again after the field is cleared.
  title_groups: Record<string, string[]>;
  upwork: UpworkFilters;
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

export type OutreachRoute =
  | "dm"
  | "free_inmail"
  | "invite_note"
  | "invite_then_dm"
  | "paid_inmail";

export type OutreachState = "none" | "drafted" | "queued" | "sent" | "failed" | "cancelled";

export type OutreachContact = {
  contact_id: number;
  full_name: string;
  headline: string | null;
  profile_url: string | null;
  origin: string;
  is_connection: boolean | null;
  can_send_inmail: boolean | null;
  // The route this contact resolves to right now. Null only when no free route
  // exists and no fallback has been picked yet.
  route: OutreachRoute | null;
  allowed_routes: OutreachRoute[];
  needs_choice: boolean;
  state: OutreachState;
  body: string | null;
  limit: number;
  provider_ref: string | null;
  failure: string | null;
  invited_at: string | null;
  accepted_at: string | null;
  sent_at: string | null;
};

export type OutreachBudget = {
  invites_used: number;
  invites_max: number;
  invites_week_used: number;
  invites_week_max: number;
  dms_used: number;
  dms_max: number;
  credits: number;
  delay_min: number;
  delay_max: number;
};

export type OutreachBody = { contacts: OutreachContact[]; budget: OutreachBudget };

// A candidate is not yet a contact - "existing" is the only bridge between the
// two, and it is what lets the drawer grey out someone already added.
export type ContactCandidate = {
  full_name: string;
  headline: string | null;
  profile_url: string | null;
  origin: string;
  existing: boolean;
};

export type OutreachStates = Record<number, OutreachState>;

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
  url: string | null;
  /** Null when the batch started with no profile, and the CV was cut from master.tex. */
  template_id: string | null;
  template_name: string;
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

export type FeedProposal = {
  provider: string;
  category: string;
  token: string | null;
  matched: number;
  sample: number;
  registered: boolean;
};

export type ApprovedFeed = {
  provider: string;
  token: string;
  status: string;
  last_job_count: number | null;
  last_fetched_at: string | null;
};

export type FeedsBody = {
  // False when no job titles are configured: proposals are scored against the
  // titles, so an empty list then means "nothing to score by", not "no data".
  has_titles: boolean;
  proposals: FeedProposal[];
  approved: ApprovedFeed[];
};

export class FieldError extends Error {
  field: string;
  constructor(field: string, message: string) {
    super(message);
    this.field = field;
  }
}

/**
 * The outreach reader. `json` turns every 422 into a FieldError attributed to
 * `filters`, which is right for filter validation and wrong here: a refusal from
 * outreach - over the route's limit, at a cap, an illegal transition - arrives as
 * FastAPI's `detail`, and that sentence is what the drawer has to show.
 */
async function detailJson<T>(response: Response): Promise<T> {
  if (!response.ok) {
    const text = await response.text();
    let detail = text;
    try {
      const parsed = JSON.parse(text) as { detail?: unknown };
      if (typeof parsed.detail === "string") detail = parsed.detail;
    } catch {
      // A non-JSON body (a proxy's own error page) is more use raw than swallowed.
    }
    throw new Error(detail);
  }
  return (await response.json()) as T;
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
  async saveTitleGroup(
    name: string,
    titles: string[],
  ): Promise<{ title_groups: Record<string, string[]> }> {
    return json(
      await fetch("/api/title-groups", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ name, titles }),
      }),
    );
  },
  async deleteTitleGroup(name: string): Promise<{ title_groups: Record<string, string[]> }> {
    return json(
      await fetch(`/api/title-groups/${encodeURIComponent(name)}`, { method: "DELETE" }),
    );
  },
  async readFeeds(): Promise<FeedsBody> {
    return json(await fetch("/api/feeds"));
  },
  async saveFeeds(body: {
    approve?: { provider: string; token: string }[];
    retire?: { provider: string; token: string }[];
  }): Promise<FeedsBody> {
    return json(
      await fetch("/api/feeds", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
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
  /** `templates` maps a job id to the template its CV is cut in. Leave it out
   *  when there is no profile yet: every CV is then cut from master.tex. */
  async startTailoring(jobIds: number[], templates?: Record<number, string>) {
    const response = await fetch("/api/tailor", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(templates ? { job_ids: jobIds, templates } : { job_ids: jobIds }),
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
  async appliedJobs(): Promise<{ applied: number[] }> {
    return json(await fetch("/api/applied"));
  },
  async setApplied(jobId: number, applied: boolean): Promise<{ job_id: number; applied: boolean }> {
    return json(
      await fetch(`/api/applied/${jobId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ applied }),
      }),
    );
  },
  async outreach(jobId: number): Promise<OutreachBody> {
    return detailJson(await fetch(`/api/outreach/${jobId}`));
  },
  async outreachBudget(): Promise<OutreachBudget> {
    return detailJson(await fetch("/api/outreach/budget"));
  },
  async addContact(
    jobId: number,
    body: {
      full_name: string;
      profile_url?: string | null;
      headline?: string | null;
      origin?: string;
    },
  ): Promise<OutreachContact> {
    return detailJson(
      await fetch(`/api/outreach/${jobId}/contacts`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      }),
    );
  },
  async removeContact(contactId: number): Promise<{ removed: boolean }> {
    return detailJson(await fetch(`/api/outreach/contacts/${contactId}`, { method: "DELETE" }));
  },
  async setContactStatus(
    contactId: number,
    body: { is_connection?: boolean; can_send_inmail?: boolean },
  ): Promise<{ contact_id: number; is_connection: boolean | null; can_send_inmail: boolean | null }> {
    return detailJson(
      await fetch(`/api/outreach/contacts/${contactId}/status`, {
        method: "PATCH",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(body),
      }),
    );
  },
  async draftOutreach(
    jobId: number,
    contactId: number,
    route?: OutreachRoute | null,
  ): Promise<OutreachContact> {
    return detailJson(
      await fetch(`/api/outreach/${jobId}/${contactId}/draft`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ route: route ?? null }),
      }),
    );
  },
  async saveOutreachBody(
    jobId: number,
    contactId: number,
    body: string,
    route?: OutreachRoute | null,
  ): Promise<OutreachContact> {
    return detailJson(
      await fetch(`/api/outreach/${jobId}/${contactId}/body`, {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ body, route: route ?? null }),
      }),
    );
  },
  async approveOutreach(
    jobId: number,
    contactId: number,
    route?: OutreachRoute | null,
  ): Promise<OutreachContact> {
    return detailJson(
      await fetch(`/api/outreach/${jobId}/${contactId}/approve`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ route: route ?? null }),
      }),
    );
  },
  async cancelOutreach(jobId: number, contactId: number): Promise<OutreachContact> {
    return detailJson(await fetch(`/api/outreach/${jobId}/${contactId}/cancel`, { method: "POST" }));
  },
  async outreachStates(): Promise<{ states: OutreachStates }> {
    return detailJson(await fetch("/api/outreach/states"));
  },
  // A GET, deliberately: reads the poster the job posting already names, no
  // network call to LinkedIn involved. Safe to fire on every drawer open.
  async statedContacts(jobId: number): Promise<{ candidates: ContactCandidate[] }> {
    return detailJson(await fetch(`/api/outreach/${jobId}/contacts/stated`));
  },
  // Fires a company-scoped LinkedIn people search. Only ever call this from a
  // user action on one job's drawer - never from anything that loops over jobs.
  // `note` is set when the search was skipped rather than run - an Upwork gig
  // whose client is anonymous has no company name to search for.
  async findContacts(
    jobId: number,
  ): Promise<{ candidates: ContactCandidate[]; note?: string | null }> {
    return detailJson(await fetch(`/api/outreach/${jobId}/find`, { method: "POST" }));
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
};
