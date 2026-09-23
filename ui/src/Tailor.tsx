import type { JobRun } from "./api";
import { MarkApplied } from "./MarkApplied";
import { hostOf } from "./Run";

const STAGES = ["Reading the posting", "Cutting the CV", "Reviewer gate", "Fit to two pages"];

export function TailorBatch({
  jobs,
  running,
  onStop,
  onReview,
  readyIds,
  applied,
  onApplied,
}: {
  jobs: JobRun[];
  running: boolean;
  onStop: () => void;
  onReview: (jobId: number) => void;
  /** Jobs whose cv.tex is finished. Only these can be opened in the studio. */
  readyIds: Set<number>;
  /** Jobs already recorded as sent, so the button reads the right way round. */
  applied: Set<number>;
  onApplied: (jobId: number, applied: boolean) => void;
}) {
  if (!jobs.length) return null;
  const approved = jobs.filter((job) => job.state === "approved").length;
  const failed = jobs.filter((job) => job.state === "failed").length;

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>Tailoring batch</h2>
        <div className="note">
          {running
            ? `${jobs.length} jobs · 2 at a time · reviewer gates every CV`
            : `${approved} approved${failed ? ` · ${failed} not approved` : ""}`}
        </div>
        {running && (
          <button className="btn stop" onClick={onStop} style={{ marginLeft: 12 }}>
            Stop batch
          </button>
        )}
      </div>

      {jobs.map((job) => (
        <div className="job" key={job.job_id} data-state={job.state}>
          <div className="top">
            <span className="who">{job.title ?? `job ${job.job_id}`}</span>
            {job.company && <span className="at">{job.company}</span>}
            {job.template_name && <span className="tpl">{job.template_name}</span>}
            {/* The posting, not the folder: checking what the CV is aimed at is
                the one thing you cannot do from the files on disk. */}
            {job.url && (
              <a className="posting-link" href={job.url} target="_blank" rel="noreferrer" title={job.url}>
                {hostOf(job.url)} ↗
              </a>
            )}
            <span className="status">{statusOf(job)}</span>
            <MarkApplied
              jobId={job.job_id}
              applied={applied.has(job.job_id)}
              onChange={(on) => onApplied(job.job_id, on)}
            />
          </div>

          <div className="stages">
            {STAGES.map((_, index) => (
              <span key={index} className="stage" data-s={stageState(job, index)} />
            ))}
          </div>

          {job.findings.length > 0 && (
            <div className="found">
              {job.state === "approved" ? "Caught before it shipped:" : "The reviewer objected to:"}
              <ul>
                {job.findings.map((finding) => (
                  <li key={finding}>{finding}</li>
                ))}
              </ul>
            </div>
          )}

          {job.error && <div className="found">{job.error}</div>}

          {job.folder && (
            <div className="acts">
              {job.pages != null && (
                <span className="pages" data-over={job.pages > 2}>
                  {job.pages} pp{job.pages > 2 ? " · over" : ""}
                </span>
              )}
              <span className="paths">
                {job.state === "failed"
                  ? `folder kept — nothing was shipped as ready`
                  : job.folder}
              </span>
              {readyIds.has(job.job_id) ? (
                <button
                  className="btn sm"
                  style={{ marginLeft: "auto" }}
                  onClick={() => onReview(job.job_id)}
                >
                  Review &amp; revise
                </button>
              ) : (
                // Offering this before cv.tex is finished gives you a studio
                // that cannot compile, preview or answer anything.
                <span className="paths" style={{ marginLeft: "auto" }}>
                  cutting the CV…
                </span>
              )}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

function statusOf(job: JobRun) {
  if (job.state === "queued") return "queued";
  if (job.state === "cancelled") return "cancelled";
  if (job.state === "approved")
    return `Approved · ${job.rounds} round${job.rounds > 1 ? "s" : ""}${job.fit ? ` · fit ${job.fit.toFixed(2)}` : ""}`;
  if (job.pages != null && job.pages > 2) return `Round ${job.rounds} · trimming to 2 pages`;
  if (job.state === "failed") {
    if (!job.error) return `Not approved after ${job.rounds} rounds`;
    return job.rounds ? "Stopped" : "Skipped";
  }
  return `Round ${job.rounds || 1}`;
}

function stageState(job: JobRun, index: number) {
  if (job.state === "queued" || job.state === "cancelled") return "todo";
  if (job.state === "approved") return "done";
  if (job.state === "failed") return index === 0 && !job.folder ? "failed" : "done";
  // The length stage is only live once a cut has been measured and ran long;
  // otherwise the reviewer gate is still where the work is.
  const trimming = job.pages != null && job.pages > 2;
  const at = job.folder ? (trimming ? 3 : job.rounds > 0 ? 2 : 1) : 0;
  return index < at ? "done" : index === at ? "now" : "todo";
}
