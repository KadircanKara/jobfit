import { useEffect, useState } from "react";
import { cvApi, type TemplateRow } from "./api";

export type PickedJob = { job_id: number; title: string; company: string };

/**
 * The step between "Tailor selected" and the batch: which template each CV is
 * cut in. Every job starts on the default template; a thumbnail sets them all
 * at once, and each row can differ. With no profile saved there is nothing to
 * render a template from, so the batch runs as it always has, from master.tex.
 */
export function TemplateStep({
  jobs,
  onStart,
  onCancel,
}: {
  jobs: PickedJob[];
  onStart: (templates?: Record<number, string>) => Promise<void>;
  onCancel: () => void;
}) {
  const [rows, setRows] = useState<TemplateRow[] | null>(null);
  const [defaultId, setDefaultId] = useState("");
  const [hasProfile, setHasProfile] = useState<boolean | null>(null);
  const [choice, setChoice] = useState<Record<number, string>>({});
  const [problem, setProblem] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    Promise.all([cvApi.profile(), cvApi.templates()])
      .then(([profile, library]) => {
        setHasProfile(profile.profile !== null);
        setRows(library.templates);
        setDefaultId(library.default_id);
      })
      .catch((error) => setProblem(error instanceof Error ? error.message : String(error)));
  }, []);

  const templateOf = (jobId: number) => choice[jobId] ?? defaultId;
  const everyJobOn = (id: string) => jobs.every((job) => templateOf(job.job_id) === id);

  function applyToAll(id: string) {
    setChoice(Object.fromEntries(jobs.map((job) => [job.job_id, id])));
  }

  async function start() {
    setStarting(true);
    setProblem(null);
    try {
      await onStart(
        hasProfile ? Object.fromEntries(jobs.map((job) => [job.job_id, templateOf(job.job_id)])) : undefined,
      );
    } catch (error) {
      setProblem(error instanceof Error ? error.message : String(error));
    } finally {
      setStarting(false);
    }
  }

  const count = jobs.length === 1 ? "1 CV" : `${jobs.length} CVs`;
  const loading = hasProfile === null && !problem;

  return (
    <section className="tpl-step" aria-label="Choose a template for each CV">
      <div className="tpl-step-head">
        <b>Which template should each CV use?</b>
        {hasProfile && (
          <span className="note">
            Each CV is cut from your profile in the template you pick. Click a template to use it for every job.
          </span>
        )}
      </div>

      {loading && <div className="empty">Loading templates…</div>}

      {hasProfile === false && (
        <p className="note">
          No profile is saved yet, so each CV is cut from your master.tex as it is. Save a profile on the Profile
          page to choose a template per job.
        </p>
      )}

      {hasProfile && rows && (
        <>
          <div className="tpl-strip">
            {rows.map((row) => (
              <button
                key={row.id}
                type="button"
                className="tpl-pick"
                aria-pressed={everyJobOn(row.id)}
                onClick={() => applyToAll(row.id)}
                title={`Use ${row.name} for every job`}
              >
                <img
                  src={`/api/cv/templates/${encodeURIComponent(row.id)}/thumbnail.png`}
                  alt=""
                  loading="lazy"
                  onError={(event) => (event.currentTarget.style.visibility = "hidden")}
                />
                <span>
                  {row.name}
                  {row.default && <em> · default</em>}
                </span>
              </button>
            ))}
          </div>

          <ul className="tpl-jobs">
            {jobs.map((job) => (
              <li key={job.job_id}>
                <span className="who">{job.title || `job ${job.job_id}`}</span>
                {job.company && <span className="at">{job.company}</span>}
                <select
                  aria-label={`Template for ${job.title || `job ${job.job_id}`}`}
                  value={templateOf(job.job_id)}
                  onChange={(event) => setChoice((current) => ({ ...current, [job.job_id]: event.target.value }))}
                >
                  {rows.map((row) => (
                    <option key={row.id} value={row.id}>
                      {row.name}
                    </option>
                  ))}
                </select>
              </li>
            ))}
          </ul>
        </>
      )}

      {problem && <div className="failbox">{problem}</div>}

      <div className="acts">
        <button className="btn ghost" onClick={onCancel} disabled={starting}>
          Cancel
        </button>
        <button className="btn" onClick={start} disabled={starting || loading}>
          {starting ? "Starting…" : `Tailor ${count}`}
        </button>
      </div>
    </section>
  );
}
