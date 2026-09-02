import { useMemo, useState } from "react";
import { api, FieldError, type Filters, type Vocab } from "./api";
import { TagField } from "./TagField";
import { TitlePresets } from "./TitlePresets";
import { FeedsPanel } from "./Feeds";

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
  { id: "upwork", label: "Upwork", hint: "Freelance postings, its own settings below", enabled: true },
];

// Mirrors UPWORK_JOB_TYPES / UPWORK_EXPERIENCE in jobhunt/preferences.py.
const UPWORK_JOB_TYPES: [string, string][] = [
  ["hourly", "hourly"],
  ["fixed", "fixed price"],
];
const UPWORK_EXPERIENCE = ["entry_level", "intermediate", "expert"];

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
  job_types: string[];
  sources: string[];
  experience_min: string;
  experience_max: string;
  min_salary: string;
  currency: string;
  age_value: string;
  age_unit: string;
  top_n: string;
  upwork: {
    queries: string[];
    job_types: string[];
    min_hourly: string;
    min_fixed: string;
    experience_level: string[];
    sort: string;
    verified_payment_only: boolean;
    require_verified_client: boolean;
    require_client_spend: boolean;
  };
};

function draftFrom(filters: Filters): Draft {
  return {
    titles: filters.titles,
    locations: filters.locations,
    work_model: filters.work_model.length ? filters.work_model : ["remote", "hybrid", "onsite"],
    job_types: filters.job_types ?? [],
    sources: filters.sources?.length ? filters.sources : ["ats", "linkedin"],
    experience_min: filters.experience_min ?? "junior",
    experience_max: filters.experience_max ?? "none",
    min_salary: filters.min_salary ? String(filters.min_salary) : "",
    currency: filters.currency || "USD",
    age_value: String(filters.max_age_days || 30),
    age_unit: "days",
    top_n: String(filters.top_n || 50),
    upwork: {
      queries: filters.upwork?.queries ?? [],
      job_types: filters.upwork?.job_types?.length ? filters.upwork.job_types : ["hourly", "fixed"],
      min_hourly: filters.upwork?.min_hourly ? String(filters.upwork.min_hourly) : "",
      min_fixed: filters.upwork?.min_fixed ? String(filters.upwork.min_fixed) : "",
      experience_level: filters.upwork?.experience_level ?? [],
      sort: filters.upwork?.sort ?? "relevance",
      verified_payment_only: filters.upwork?.verified_payment_only ?? true,
      require_verified_client: filters.upwork?.require_verified_client ?? false,
      require_client_spend: filters.upwork?.require_client_spend ?? false,
    },
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
  // Presets live beside the titles in filters.yaml, so they arrive with the
  // filters and are re-read from whatever the group endpoints return.
  const [groups, setGroups] = useState<Record<string, string[]>>(filters.title_groups ?? {});

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) =>
    setDraft((current) => ({ ...current, [key]: value }));

  const setUpwork = <K extends keyof Draft["upwork"]>(key: K, value: Draft["upwork"][K]) =>
    setDraft((current) => ({ ...current, upwork: { ...current.upwork, [key]: value } }));

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
        job_types: draft.job_types,
        sources: draft.sources,
        experience_min: draft.experience_min,
        experience_max: draft.experience_max === "none" ? null : draft.experience_max,
        min_salary: draft.min_salary || null,
        currency: draft.currency,
        max_age: { value: Number(draft.age_value), unit: draft.age_unit },
        top_n: Number(draft.top_n),
        upwork: {
          queries: draft.upwork.queries,
          job_types: draft.upwork.job_types,
          min_hourly: draft.upwork.min_hourly || null,
          min_fixed: draft.upwork.min_fixed || null,
          experience_level: draft.upwork.experience_level,
          sort: draft.upwork.sort,
          verified_payment_only: draft.upwork.verified_payment_only,
          require_verified_client: draft.upwork.require_verified_client,
          require_client_spend: draft.upwork.require_client_spend,
        },
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

        <SourcePicker value={draft.sources} onChange={(next) => set("sources", next)} />

        {draft.sources.includes("upwork") && (
          <UpworkPanel value={draft.upwork} onChange={setUpwork} messages={messages} />
        )}

        <button className="btn ghost" onClick={save} disabled={!valid || draft.sources.length === 0}>
          {saved ? "Saved" : "Save filters"}
        </button>
      </div>
      <FeedsPanel reloadToken={feedsReload} />
    </>
  );
}

function UpworkPanel({
  value,
  onChange,
  messages,
}: {
  value: Draft["upwork"];
  onChange: <K extends keyof Draft["upwork"]>(key: K, next: Draft["upwork"][K]) => void;
  messages: Record<string, string>;
}) {
  const toggle = <K extends "job_types" | "experience_level">(key: K, item: string) =>
    onChange(key, (value[key].includes(item) ? value[key].filter((v) => v !== item) : [...value[key], item]) as Draft["upwork"][K]);

  return (
    <div className="fields upwork">
      <TagField
        label="Upwork queries"
        wide
        tags={value.queries}
        vocab={[]}
        placeholder="Add a search term, press Enter"
        freeNote="free text"
        emptyNote="No suggestions — Upwork has no fixed title vocabulary, type your own."
        onChange={(tags) => onChange("queries", tags)}
        hint={<>searched against the posting itself, not matched against the titles above</>}
      />

      <div className="field">
        <label>
          <span>Job type</span>
        </label>
        <div className="toggles">
          {UPWORK_JOB_TYPES.map(([id, label]) => (
            <button
              key={id}
              className="chip"
              aria-pressed={value.job_types.includes(id)}
              onClick={() => toggle("job_types", id)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className={messages["upwork.min_hourly"] ? "field bad" : "field"}>
        <label>
          <span>Min hourly rate</span>
        </label>
        <input
          inputMode="numeric"
          placeholder="any"
          value={value.min_hourly}
          onChange={(e) => onChange("min_hourly", e.target.value)}
        />
        <div className="hint">$ per hour</div>
      </div>

      <div className={messages["upwork.min_fixed"] ? "field bad" : "field"}>
        <label>
          <span>Min fixed budget</span>
        </label>
        <input
          inputMode="numeric"
          placeholder="any"
          value={value.min_fixed}
          onChange={(e) => onChange("min_fixed", e.target.value)}
        />
        <div className="hint">$ for the whole project</div>
      </div>

      <div className="field">
        <label>
          <span>Experience level</span>
        </label>
        <div className="toggles">
          {UPWORK_EXPERIENCE.map((level) => (
            <button
              key={level}
              className="chip"
              aria-pressed={value.experience_level.includes(level)}
              onClick={() => toggle("experience_level", level)}
            >
              {level.replace("_", " ")}
            </button>
          ))}
        </div>
        <div className="hint">{value.experience_level.length ? "" : "no restriction"}</div>
      </div>

      <div className="field">
        <label>Sort</label>
        <select value={value.sort} onChange={(e) => onChange("sort", e.target.value)}>
          <option value="relevance">Best match</option>
          <option value="recency">Newest first</option>
          <option value="client_total_charge">Client spend</option>
          <option value="client_rating">Client rating</option>
        </select>
        <div className="hint">
          Best match is what the Upwork website itself shows. Newest first returns a
          different set entirely — on the same query it shared no results at all with
          Best match, and most of them were assistant work that merely mentions AI.
        </div>
      </div>

      <div className="field">
        <label className="checkline">
          <input
            type="checkbox"
            checked={value.verified_payment_only}
            onChange={(e) => onChange("verified_payment_only", e.target.checked)}
          />
          <span>Verified payment only</span>
        </label>
        <div className="hint">Asked of Upwork's own search, so unverified clients never come back.</div>
      </div>

      <div className="field">
        <label className="checkline">
          <input
            type="checkbox"
            checked={value.require_verified_client}
            onChange={(e) => onChange("require_verified_client", e.target.checked)}
          />
          <span>Drop unverified clients after the fetch</span>
        </label>
        <div className="hint">
          Belt and braces with the box above: this one is a rule over what was stored, so it
          still holds for jobs fetched before that box was ticked.
        </div>
      </div>

      <div className="field">
        <label className="checkline">
          <input
            type="checkbox"
            checked={value.require_client_spend}
            onChange={(e) => onChange("require_client_spend", e.target.checked)}
          />
          <span>Drop clients who have never spent</span>
        </label>
        <div className="hint">
          A different signal from payment verification: in the first real run every client was
          verified, yet 14 of 40 had spent $0.00. A card on file is not a hiring history.
        </div>
      </div>

      <div className="notes">
        {messages["upwork.queries"] && <div className="err">{messages["upwork.queries"]}</div>}
        {messages["upwork.job_types"] && <div className="err">{messages["upwork.job_types"]}</div>}
        {messages["upwork.min_hourly"] && <div className="err">{messages["upwork.min_hourly"]}</div>}
        {messages["upwork.min_fixed"] && <div className="err">{messages["upwork.min_fixed"]}</div>}
        {messages["upwork.experience_level"] && (
          <div className="err">{messages["upwork.experience_level"]}</div>
        )}
        {messages["upwork.verified_payment_only"] && (
          <div className="err">{messages["upwork.verified_payment_only"]}</div>
        )}
      </div>
    </div>
  );
}

function SourcePicker({
  value,
  onChange,
}: {
  value: string[];
  onChange: (next: string[]) => void;
}) {
  return (
    <div className="sources">
      <span className="lbl">Sources</span>
      {SOURCES.map((source) => (
        <label key={source.id} className={source.enabled ? "src" : "src off"} title={source.hint}>
          <input
            type="checkbox"
            disabled={!source.enabled}
            checked={value.includes(source.id)}
            onChange={(e) =>
              onChange(
                e.target.checked ? [...value, source.id] : value.filter((id) => id !== source.id),
              )
            }
          />
          {source.label}
        </label>
      ))}
      {value.length === 0 && (
        <span className="why warn">Pick at least one source — nothing can be found otherwise.</span>
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

  if (draft.upwork.min_hourly.trim() && !(Number(draft.upwork.min_hourly) > 0)) {
    out["upwork.min_hourly"] = "Numbers only, above zero.";
  }
  if (draft.upwork.min_fixed.trim() && !(Number(draft.upwork.min_fixed) > 0)) {
    out["upwork.min_fixed"] = "Numbers only, above zero.";
  }

  return out;
}
