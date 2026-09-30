import { useEffect, useId, useMemo, useState } from "react";
import { Check, ChevronRight, TriangleAlert } from "lucide-react";
import { api, FieldError, type Filters, type Vocab } from "./api";
import { TagField } from "./TagField";
import { TitlePresets } from "./TitlePresets";

// Mirrors SENIORITY_ORDER in jobhunt/rank/deterministic.py, order included: the
// ceiling is disabled against the floor by index, so a list in a different order
// would let someone pick a ceiling the server then refuses.
const LEVELS = ["intern", "junior", "mid", "senior", "staff", "lead", "principal"];
const UNITS = ["hours", "days", "weeks", "months"] as const;
// Mirrors EMPLOYMENT_TYPES in jobhunt/pipeline/normalize.py. The label is what a
// person calls it; the value is what the filter stores.
const JOB_TYPES: [string, string][] = [
  ["full_time", "full time"],
  ["part_time", "part time"],
  ["contract", "contract"],
  ["internship", "internship"],
  ["temporary", "temporary"],
];

// Mirrors SOURCE_CHOICES in jobhunt/preferences.py.
const SOURCES: { id: string; label: string; hint: string; enabled: boolean }[] = [
  { id: "ats", label: "ATS", hint: "Greenhouse, Ashby, Lever and nine more", enabled: true },
  { id: "linkedin", label: "LinkedIn", hint: "Public job search", enabled: true },
];

export function sourceLabel(id: string): string {
  return SOURCES.find((source) => source.id === id)?.label ?? id;
}

// Sample rates until the server reports the snapshot it fetched for the run.
// Shown with their timestamp so nobody reads a stale number as live.
const RATES: Record<string, number> = { USD: 1, EUR: 0.918, GBP: 0.784, TRY: 48.02 };

type Props = {
  filters: Filters;
  vocab: Vocab;
  onSaved: (filters: Filters) => void;
  onValidity: (ok: boolean) => void;
  /* Which sources are on. Owned by the store, which draws the picker beside
     the run controls and saves a toggle straight away, so this panel only
     reads it - to send with a save and to gate it. */
  sources: string[];
  onDirty: (dirty: boolean) => void;
};

type Draft = {
  titles: string[];
  locations: string[];
  work_model: string[];
  job_types: string[];
  experience_min: string;
  experience_max: string;
  min_salary: string;
  currency: string;
  age_value: string;
  age_unit: string;
  top_n: string;
};

function draftFrom(filters: Filters): Draft {
  return {
    titles: filters.titles,
    locations: filters.locations,
    work_model: filters.work_model.length ? filters.work_model : ["remote", "hybrid", "onsite"],
    job_types: filters.job_types ?? [],
    experience_min: filters.experience_min ?? "junior",
    experience_max: filters.experience_max ?? "none",
    min_salary: filters.min_salary ? String(filters.min_salary) : "",
    currency: filters.currency || "USD",
    age_value: String(filters.max_age_days || 30),
    age_unit: "days",
    top_n: String(filters.top_n || 50),
  };
}

function payload(draft: Draft, sources: string[]) {
  return {
    titles: draft.titles,
    locations: draft.locations,
    work_model: draft.work_model,
    job_types: draft.job_types,
    sources,
    experience_min: draft.experience_min,
    experience_max: draft.experience_max === "none" ? null : draft.experience_max,
    min_salary: draft.min_salary || null,
    currency: draft.currency,
    max_age: { value: Number(draft.age_value), unit: draft.age_unit },
    top_n: Number(draft.top_n),
  };
}

/* Every value in a Draft is a string, a boolean, or a list of strings, and the
   keys are written in one place, so serialising is a sound way to compare. */
function same(a: unknown, b: unknown) {
  return JSON.stringify(a) === JSON.stringify(b);
}

/** Save and revert for the form. */
function Acts({
  dirty,
  ok,
  saved,
  saving,
  onSave,
  onRevert,
}: {
  dirty: boolean;
  ok: boolean;
  saved: boolean;
  saving: boolean;
  onSave: () => void;
  onRevert: () => void;
}) {
  return (
    <div className="formacts whole">
      {!ok && dirty && (
        <span className="hint">Fix the fields marked above, and keep at least one source on, to save.</span>
      )}
      <button className="btn ghost" onClick={onRevert} disabled={!dirty || saving}>
        Revert
      </button>
      <button className="btn" onClick={onSave} disabled={!ok || !dirty || saving} aria-busy={saving}>
        {saving ? (
          <>
            <span className="spin" aria-hidden="true" />
            Saving
          </>
        ) : saved ? (
          "Saved"
        ) : (
          "Save"
        )}
      </button>
    </div>
  );
}

/* The form unmounts with the Run page. What it was holding is kept here, so an
   unsaved edit and an open panel are both still there on the way back. Module
   state rather than the store: nothing else reads a half-typed draft. */
let kept: { draft: Draft | null; open: boolean } = { draft: null, open: false };

/** One line saying what the collapsed panel holds. */
function summary(draft: Draft, sources: string[]): string {
  const parts = [
    draft.titles.length ? `${draft.titles.length} title${draft.titles.length > 1 ? "s" : ""}` : "any title",
    draft.locations.length ? draft.locations.slice(0, 3).join(", ") + (draft.locations.length > 3 ? " +" : "") : "anywhere",
    draft.work_model.join(", "),
    draft.experience_max === "none" ? `${draft.experience_min}+` : `${draft.experience_min} to ${draft.experience_max}`,
    sources.map(sourceLabel).join(" + "),
  ];
  return parts.filter(Boolean).join(" · ");
}

export function FiltersPanel({ filters, vocab, onSaved, onValidity, sources, onDirty }: Props) {
  const [draft, setDraft] = useState<Draft>(() => kept.draft ?? draftFrom(filters));
  const [open, setOpen] = useState(kept.open);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);
  const [impact, setImpact] = useState<{ matched: number; total: number } | null>(null);
  // Presets live beside the titles in filters.yaml, so they arrive with the
  // filters and are re-read from whatever the group endpoints return.
  const [groups, setGroups] = useState<Record<string, string[]>>(filters.title_groups ?? {});
  const bodyId = useId();

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((current) => ({ ...current, [key]: value }));

  const local = useMemo(() => validate(draft), [draft]);
  const messages = { ...local, ...errors };
  const valid = Object.keys(local).length === 0;
  useEffect(() => onValidity(valid), [valid, onValidity]);
  // A problem the run would refuse to start on is not left folded away.
  useEffect(() => {
    if (!valid) setOpen(true);
  }, [valid]);

  const onFile = useMemo(() => draftFrom(filters), [filters]);
  const dirty = !same(draft, onFile);
  useEffect(() => onDirty(dirty), [dirty, onDirty]);
  useEffect(() => {
    kept = { draft: dirty ? draft : null, open };
  }, [draft, dirty, open]);

  const salary = parseSalary(draft.min_salary);

  async function save() {
    setErrors({});
    setSaving(true);
    try {
      const body = await api.saveFilters(payload(draft, sources));
      onSaved(body.filters);
      // Take back what the server stored, not what was typed: "$1,000" is
      // saved as 1000, and a draft still reading "$1,000" would mark the
      // form unsaved the moment the save succeeded.
      setDraft(draftFrom(body.filters));
      setImpact(body.title_impact);
      setSaved(true);
      window.setTimeout(() => setSaved(false), 1400);
    } catch (error) {
      if (error instanceof FieldError) {
        setErrors({ [error.field]: error.message });
      } else setErrors({ filters: String(error) });
    } finally {
      setSaving(false);
    }
  }

  /** Throw away unsaved edits, back to what is on file. */
  function revert() {
    setDraft(draftFrom(filters));
    setErrors({});
  }

  const titleCount = impact
    ? impact.matched
    : draft.titles.reduce(
        (total, title) => total + (vocab.titles.find((row) => row.value === title)?.count ?? 0),
        0,
      );

  return (
    <section className="panel acc" data-open={open || undefined}>
      <button
        type="button"
        className="acc-head"
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={() => setOpen((was) => !was)}
      >
        <ChevronRight className="acc-chevron" aria-hidden="true" />
        <h2>Filters</h2>
        {dirty && (
          <span className="acc-dirty">
            <span className="dot" data-tone="warn" aria-hidden="true" />
            unsaved
          </span>
        )}
        {!open && <span className="acc-summary">{summary(draft, sources)}</span>}
      </button>

      <div id={bodyId} className="acc-body" hidden={!open}>
        <div className="fields">
          <TagField
            label="Titles"
            wide
            tags={draft.titles}
            vocab={vocab.titles}
            placeholder="Add a title, press Enter"
            freeNote="not in the corpus"
            emptyNote="Every matching title is already on the list."
            onChange={(tags) => set("titles", tags)}
            hint={
              <>
                matches <b>{titleCount.toLocaleString()}</b> of {vocab.active_jobs.toLocaleString()} active
                jobs
              </>
            }
          >
            <TitlePresets
              groups={groups}
              titles={draft.titles}
              savedTitles={filters.titles}
              onPick={(tags) => set("titles", tags)}
              onGroups={setGroups}
            />
          </TagField>

          <TagField
            label="Locations"
            tags={draft.locations}
            vocab={vocab.locations}
            placeholder="Country or region"
            freeNote="could not be placed"
            emptyNote="No other place matches that."
            onChange={(tags) => set("locations", tags)}
            hint={
              draft.locations.length ? (
                <>{draft.locations.length} place{draft.locations.length > 1 ? "s" : ""} in scope</>
              ) : (
                <>no location rule · every market</>
              )
            }
          />

          <div className="field">
            <label>
              <span>Work model</span>
            </label>
            <div className="toggles">
              {["remote", "hybrid", "onsite"].map((model) => (
                <button
                  key={model}
                  className="chip"
                  aria-pressed={draft.work_model.includes(model)}
                  onClick={() =>
                    set(
                      "work_model",
                      draft.work_model.includes(model)
                        ? draft.work_model.filter((m) => m !== model)
                        : [...draft.work_model, model],
                    )
                  }
                >
                  {model}
                </button>
              ))}
            </div>
            <div className="hint">any combination</div>
          </div>

          <div className="field">
            <label>
              <span>Job type</span>
            </label>
            <div className="toggles">
              {JOB_TYPES.map(([value, label]) => (
                <button
                  key={value}
                  className="chip"
                  aria-pressed={draft.job_types.includes(value)}
                  onClick={() =>
                    set(
                      "job_types",
                      draft.job_types.includes(value)
                        ? draft.job_types.filter((t) => t !== value)
                        : [...draft.job_types, value],
                    )
                  }
                >
                  {label}
                </button>
              ))}
            </div>
            <div className="hint">
              {draft.job_types.length
                ? "a posting that does not state its type is still kept"
                : "no restriction"}
            </div>
          </div>

          <div className={messages.experience_max ? "field bad" : "field"}>
            <label>
              <span>Experience floor</span>
            </label>
            <select value={draft.experience_min} onChange={(e) => set("experience_min", e.target.value)}>
              {LEVELS.map((level) => (
                <option key={level}>{level}</option>
              ))}
            </select>
          </div>

          <div className={messages.experience_max ? "field bad" : "field"}>
            <label>
              <span>Ceiling</span>
            </label>
            <select value={draft.experience_max} onChange={(e) => set("experience_max", e.target.value)}>
              {LEVELS.map((level) => (
                <option
                  key={level}
                  value={level}
                  disabled={LEVELS.indexOf(level) < LEVELS.indexOf(draft.experience_min)}
                >
                  {level}
                </option>
              ))}
              <option value="none">no ceiling</option>
            </select>
          </div>

          <div className={messages.min_salary ? "field bad" : "field"}>
            <label>
              <span>Min salary</span>
            </label>
            <input
              inputMode="numeric"
              placeholder="any"
              value={draft.min_salary}
              onChange={(e) => set("min_salary", e.target.value)}
            />
          </div>

          <div className="field">
            <label>
              <span>Currency</span>
            </label>
            <select value={draft.currency} onChange={(e) => set("currency", e.target.value)}>
              {Object.keys(RATES).map((code) => (
                <option key={code}>{code}</option>
              ))}
            </select>
          </div>

          <div className="notes">
            {messages.experience_max && <div className="err">{messages.experience_max}</div>}
            {messages.min_salary && <div className="err">{messages.min_salary}</div>}
            {messages.locations && <div className="err">{messages.locations}</div>}
            {salary !== null && !messages.min_salary && (
              <div className="fx">
                At or above <b>{salary.toLocaleString("en-US")}</b> {draft.currency} — matched as{" "}
                {Object.keys(RATES)
                  .filter((code) => code !== draft.currency)
                  .map((code) => `${Math.round((salary / RATES[draft.currency]) * RATES[code]).toLocaleString("en-US")} ${code}`)
                  .join(" · ")}
                <br />
                <span className="stale">
                  the run fetches its own rates before it filters, and compares every job against that one
                  snapshot
                </span>
              </div>
            )}
          </div>

          <div className={messages.max_age ? "field bad" : "field"}>
            <label>
              <span>Max age</span>
            </label>
            <div className="suffixed">
              <input
                inputMode="numeric"
                value={draft.age_value}
                onChange={(e) => set("age_value", e.target.value)}
                aria-label="Maximum age, amount"
              />
              <select
                value={draft.age_unit}
                onChange={(e) => set("age_unit", e.target.value)}
                aria-label="Maximum age, unit"
              >
                {UNITS.map((unit) => (
                  <option key={unit} value={unit}>
                    {unit}
                  </option>
                ))}
              </select>
            </div>
          </div>

          <div className={messages.top_n ? "field bad" : "field"}>
            <label>
              <span>Show me</span>
            </label>
            <input
              inputMode="numeric"
              value={draft.top_n}
              onChange={(e) => set("top_n", e.target.value)}
              aria-label="Jobs per run"
            />
            <div className="hint">jobs per run</div>
          </div>

          <div className="notes">
            {messages.max_age && <div className="err">{messages.max_age}</div>}
            {messages.top_n && <div className="err">{messages.top_n}</div>}
          </div>
        </div>

        <Acts dirty={dirty} ok={valid && sources.length > 0} saved={saved} saving={saving} onSave={save} onRevert={revert} />
        {messages.filters && <div className="err">{messages.filters}</div>}
      </div>
    </section>
  );
}

export function SourcePicker({
  value,
  onChange,
  saving,
}: {
  value: string[];
  onChange: (next: string[]) => void;
  /* A toggle here writes on the spot - there is no Save button beside the run
     controls to defer it to - so the write has to say it is happening. */
  saving?: boolean;
}) {
  return (
    <div className="sources" role="group" aria-label="Sources" aria-busy={saving}>
      <span className="lbl">Sources</span>
      {SOURCES.map((source) => {
        const on = value.includes(source.id);
        return (
          <button
            key={source.id}
            type="button"
            className="chip"
            aria-pressed={on}
            disabled={!source.enabled}
            title={source.hint}
            onClick={() => onChange(on ? value.filter((id) => id !== source.id) : [...value, source.id])}
          >
            {on && <Check aria-hidden="true" />}
            {source.label}
          </button>
        );
      })}
      {saving && <span className="spin" aria-hidden="true" />}
      {value.length === 0 && (
        <span className="warn" role="alert">
          <TriangleAlert className="icon" aria-hidden="true" /> Pick at least one source. Nothing can be found
          otherwise.
        </span>
      )}
    </div>
  );
}

function parseSalary(raw: string): number | null {
  const text = raw.trim().toLowerCase().replace(/[,$€£₺\s]/g, "");
  if (!text) return null;
  const match = text.match(/^([0-9]*\.?[0-9]+)([km]?)$/);
  if (!match) return null;
  const base = Number(match[1]);
  const scale = match[2] === "k" ? 1000 : match[2] === "m" ? 1_000_000 : 1;
  return Math.round(base * scale);
}

function validate(draft: Draft): Record<string, string> {
  const out: Record<string, string> = {};

  if (
    draft.experience_max !== "none" &&
    LEVELS.indexOf(draft.experience_max) < LEVELS.indexOf(draft.experience_min)
  ) {
    out.experience_max =
      "The ceiling sits below the floor, so nothing can match. Raise the ceiling or lower the floor.";
  }

  if (draft.min_salary.trim()) {
    const parsed = parseSalary(draft.min_salary);
    if (parsed === null || parsed <= 0) {
      out.min_salary = "Numbers only, above zero. 80000 or 80k both work.";
    }
  }

  if (!/^\d+$/.test(draft.age_value.trim()) || Number(draft.age_value) < 1) {
    out.max_age = "Whole numbers only, above zero.";
  }

  const top = Number(draft.top_n);
  if (!/^\d+$/.test(draft.top_n.trim()) || top < 1 || top > 200) {
    out.top_n = "Pick a whole number between 1 and 200.";
  }

  return out;
}
