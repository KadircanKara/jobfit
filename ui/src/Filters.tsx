import { useMemo, useState } from "react";
import { api, FieldError, type Filters, type Vocab } from "./api";
import { TagField } from "./TagField";
import { FeedsPanel } from "./Feeds";

const LEVELS = ["junior", "mid", "senior", "staff", "lead", "principal"];
const UNITS = ["hours", "days", "weeks", "months"] as const;

// Sample rates until the server reports the snapshot it fetched for the run.
// Shown with their timestamp so nobody reads a stale number as live.
const RATES: Record<string, number> = { USD: 1, EUR: 0.918, GBP: 0.784, TRY: 48.02 };

type Props = {
  filters: Filters;
  vocab: Vocab;
  onSaved: (filters: Filters) => void;
  onValidity: (ok: boolean) => void;
};

type Draft = {
  titles: string[];
  locations: string[];
  work_model: string[];
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
    experience_min: filters.experience_min ?? "junior",
    experience_max: filters.experience_max ?? "none",
    min_salary: filters.min_salary ? String(filters.min_salary) : "",
    currency: filters.currency || "USD",
    age_value: String(filters.max_age_days || 30),
    age_unit: "days",
    top_n: String(filters.top_n || 50),
  };
}

export function FiltersPanel({ filters, vocab, onSaved, onValidity }: Props) {
  const [draft, setDraft] = useState<Draft>(() => draftFrom(filters));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [saved, setSaved] = useState(false);
  const [impact, setImpact] = useState<{ matched: number; total: number } | null>(null);
  // Bumped after a save so the feeds section refetches: its proposals are
  // scored against the titles that were just written, not the old ones.
  const [feedsReload, setFeedsReload] = useState(0);

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((current) => ({ ...current, [key]: value }));

  const local = useMemo(() => validate(draft), [draft]);
  const messages = { ...local, ...errors };
  const valid = Object.keys(local).length === 0;
  onValidity(valid);

  const salary = parseSalary(draft.min_salary);

  async function save() {
    setErrors({});
    try {
      const body = await api.saveFilters({
        titles: draft.titles,
        locations: draft.locations,
        work_model: draft.work_model,
        experience_min: draft.experience_min,
        experience_max: draft.experience_max === "none" ? null : draft.experience_max,
        min_salary: draft.min_salary || null,
        currency: draft.currency,
        max_age: { value: Number(draft.age_value), unit: draft.age_unit },
        top_n: Number(draft.top_n),
      });
      onSaved(body.filters);
      setImpact(body.title_impact);
      setSaved(true);
      setFeedsReload((n) => n + 1);
      window.setTimeout(() => setSaved(false), 1400);
    } catch (error) {
      if (error instanceof FieldError) setErrors({ [error.field]: error.message });
      else setErrors({ filters: String(error) });
    }
  }

  const titleCount = impact
    ? impact.matched
    : draft.titles.reduce(
        (total, title) => total + (vocab.titles.find((row) => row.value === title)?.count ?? 0),
        0,
      );

  return (
    <>
      <div className="panel">
        <div className="panel-head">
          <h2>Search filters</h2>
          <div className="note">{vocab.active_jobs.toLocaleString()} active jobs in the corpus</div>
        </div>

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
          />

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
            {messages.filters && <div className="err">{messages.filters}</div>}
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

        <button className="btn ghost" onClick={save} disabled={!valid}>
          {saved ? "Saved" : "Save filters"}
        </button>
      </div>
      <FeedsPanel reloadToken={feedsReload} />
    </>
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
