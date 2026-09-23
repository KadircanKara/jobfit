import type { BackupRow } from "../api";

// Mirrors jobhunt/cv/model.py. The server validates everything again; these
// types only keep the form honest about the shape it sends.
export type Variant = { id: string; label: string; text: string };
export type Bullet = { id: string; text: string; hidden: boolean; notes: string };
export type Entry = {
  id: string;
  title: string;
  subtitle: string;
  location: string;
  start: string;
  end: string;
  url: string;
  gpa: string;
  hidden: boolean;
  notes: string;
  bullets: Bullet[];
};
export type Link = { label: string; url: string };
export type Extra = { id: string; label: string; value: string };
export type SkillGroup = { id: string; category: string; items: string[] };
export type Language = { id: string; name: string; level: string; detail: string };
export type CustomSection = { id: string; title: string; entries: Entry[] };
export type SectionRef = { key: string; title: string };
export type Basics = {
  name: string;
  headline: string;
  headline_variants: Variant[];
  email: string;
  phone: string;
  location: string;
  links: Link[];
};
export type CvProfile = {
  schema_version: number;
  basics: Basics;
  extras: Extra[];
  summary: { text: string; variants: Variant[] };
  experience: Entry[];
  education: Entry[];
  projects: Entry[];
  skills: SkillGroup[];
  languages: Language[];
  custom_sections: CustomSection[];
  layout: SectionRef[];
};

export type MasterState = "empty" | "ready" | "stale";
export type MasterStatus = {
  state: MasterState;
  template_id: string;
  template_name: string;
  generated_at: string | null;
  reason: string;
};
export type Problem = { field: string; message: string };
export type ImportReport = {
  preamble_identical: boolean;
  missing: string[];
  added: string[];
  comments_missing: string[];
  comments_added: string[];
  faithful: boolean;
};
export type ImportState = {
  state: "idle" | "running" | "done" | "failed";
  error: string | null;
  report: ImportReport | null;
  profile: CvProfile | null;
  has_preview: boolean;
  log: string;
};
export type ProfileBody = {
  profile: CvProfile | null;
  path: string;
  saved_at: string | null;
  master_exists: boolean;
  import_available: boolean;
};

/** A save the server refused, with every problem it found, each keyed by field path. */
export class ProfileRefused extends Error {
  problems: Problem[];
  constructor(problems: Problem[]) {
    super(problems[0]?.message ?? "the profile was refused");
    this.problems = problems;
  }
}

async function read<T>(response: Response): Promise<T> {
  if (response.status === 422) {
    const body = await response.json();
    throw new ProfileRefused(
      body.problems ?? [{ field: body.field ?? "profile", message: body.message ?? "that was refused" }],
    );
  }
  if (!response.ok) {
    const text = await response.text();
    let message = text;
    try {
      const parsed = JSON.parse(text) as { message?: unknown };
      if (typeof parsed.message === "string") message = parsed.message;
    } catch {
      // A non-JSON body is more use raw than swallowed.
    }
    throw new Error(message);
  }
  return (await response.json()) as T;
}

const JSON_HEADERS = { "Content-Type": "application/json" };

function send(url: string, method: "POST" | "PUT", body?: unknown): Promise<Response> {
  return fetch(url, {
    method,
    headers: JSON_HEADERS,
    body: body === undefined ? undefined : JSON.stringify(body),
  });
}

export const cvApi = {
  async profile(): Promise<ProfileBody> {
    return read(await fetch("/api/cv/profile"));
  },
  async save(profile: CvProfile) {
    return read<{ saved: boolean; backup: string | null; master: MasterStatus }>(
      await send("/api/cv/profile", "PUT", { profile }),
    );
  },
  async backups(): Promise<{ backups: BackupRow[] }> {
    return read(await fetch("/api/cv/profile/backups"));
  },
  async restore(name: string) {
    return read<{ restored: boolean; master: MasterStatus }>(
      await send("/api/cv/profile/restore", "POST", { name }),
    );
  },
  async master(): Promise<MasterStatus> {
    return read(await fetch("/api/cv/master"));
  },
  async generate() {
    return read<{ ok: boolean; log: string; pages: number | null; master: MasterStatus }>(
      await send("/api/cv/master", "POST"),
    );
  },
  async importState(): Promise<ImportState> {
    return read(await fetch("/api/cv/import"));
  },
  async startImport(): Promise<ImportState> {
    return read(await send("/api/cv/import", "POST"));
  },
  async acceptImport() {
    return read<{ saved: boolean; backup: string | null }>(await send("/api/cv/import/accept", "POST"));
  },
  async discardImport(): Promise<ImportState> {
    return read(await send("/api/cv/import/discard", "POST"));
  },
};
