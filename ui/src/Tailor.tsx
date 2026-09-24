import { ArrowRight, ExternalLink, Square } from "lucide-react";
import type { JobRun } from "./api";
import { followLink, href, type Route } from "./app/router";
import { useHunt } from "./app/store";
import { MarkApplied } from "./MarkApplied";
import { hostOf } from "./Run";

const STAGES = ["Reading the posting", "Cutting the CV", "Reviewer gate", "Fit to two pages"];

export function TailoringPage({ go }: { go: (to: Route | string) => void }) {
  const { tailor, stopTailoring, readyIds, applied, setApplied } = useHunt();
  const { jobs, running } = tailor;
  const approved = jobs.filter((job) => job.state === "approved").length;
  const failed = jobs.filter((job) => job.state === "failed").length;
  const done = approved + failed + jobs.filter((job) => job.state === "cancelled").length;
  // The badge counts outcomes in the words the rows use, and takes the tone of
  // the worst one: a batch with any job that did not ship is not green.
  const tally = (["Approved", "Not approved", "Stopped", "Skipped", "Cancelled"] as const)
    .map((word) => [word, jobs.filter((job) => outcomeOf(job) === word).length] as const)
    .filter(([, count]) => count > 0)
    .map(([word, count]) => `${count} ${word.toLowerCase()}`)
    .join(" · ");
  const worst = failed ? "warn" : "ok";

  return (
    <div className="page">
      <header className="page-header">
        <h1>Tailoring</h1>
        {jobs.length > 0 && (
          <span className="badge" data-tone={running ? "accent" : failed && !approved ? "danger" : worst}>
            <span className="dot" data-live={running ? "true" : undefined} />
            {running ? `Running · ${done} of ${jobs.length} done` : tally}
          </span>
        )}
        {running && <span className="sub">The reviewer gates every CV. The tab can be closed.</span>}
        <div className="page-actions">
          {running && (
            <button
              className="btn stop"
              onClick={() => {
                if (window.confirm("Stop the batch? CVs already approved stay; the rest are left as they are.")) {
                  void stopTailoring();
                }
              }}
            >
              <Square aria-hidden="true" />
              Stop batch
            </button>
          )}
        </div>
      </header>

      {!jobs.length ? (
        <div className="empty-state">
          <h2>No tailoring batch in this session</h2>
          <p>
            Pick jobs on the shortlist and choose Tailor selected. Each CV is cut from your profile in the
            template you pick, checked claim by claim by a reviewer, and trimmed to two pages.
          </p>
          <a
            className="btn ghost sm"
            href={href({ page: "shortlist" })}
            onClick={(e) => followLink(e, () => go({ page: "shortlist" }))}
          >
            Open the shortlist
            <ArrowRight aria-hidden="true" />
          </a>
        </div>
      ) : (
        <div className="jobs">
          {jobs.map((job) => (
            <JobRow
              key={job.job_id}
              job={job}
              ready={readyIds.has(job.job_id)}
              applied={applied.has(job.job_id)}
              onApplied={(on) => setApplied(job.job_id, on)}
              go={go}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function JobRow({
  job,
  ready,
  applied,
  onApplied,
  go,
}: {
  job: JobRun;
  ready: boolean;
  applied: boolean;
  onApplied: (on: boolean) => void;
  go: (to: Route | string) => void;
}) {
  const studio: Route = { page: "tailoring", jobId: job.job_id };
  return (
    <div className="job" data-state={job.state}>
      <div className="top">
        <span className="who">{job.title || `Job ${job.job_id}`}</span>
        {job.company && <span className="at">{job.company}</span>}
        {job.template_name && <span className="tag">{job.template_name}</span>}
        {/* The posting, not the folder: checking what the CV is aimed at is
            the one thing the files on disk cannot tell you. */}
        {job.url && (
          <a className="extlink" href={job.url} target="_blank" rel="noreferrer" title={job.url}>
            {hostOf(job.url)}
            <ExternalLink aria-hidden="true" />
          </a>
        )}
        <span className="badge" data-tone={toneOf(job)}>
          <span className="dot" data-live={job.state === "running" ? "true" : undefined} />
          {statusOf(job)}
        </span>
      </div>

      <div className="steps">
        {STAGES.map((label, index) => (
          <div className="step" key={label} data-s={stageState(job, index)}>
            <div className="progress">
              <i />
            </div>
            {label}
          </div>
        ))}
      </div>

      {job.findings.length > 0 && (
        <div className="found">
          <b>{job.state === "approved" ? "Caught before it shipped" : "The reviewer objected to"}</b>
          <ul>
            {job.findings.map((finding) => (
              <li key={finding}>{finding}</li>
            ))}
          </ul>
        </div>
      )}

      {job.error && (
        <div className="found" data-tone="danger">
          {job.error}
        </div>
      )}

      <div className="acts">
        {job.pages != null && (
          <span className="pages" data-over={job.pages > 2}>
            {job.pages} {job.pages === 1 ? "page" : "pages"}
            {job.pages > 2 ? " · over two" : ""}
          </span>
        )}
        {job.folder && (
          <span className="paths" title={job.folder}>
            {job.state === "failed" ? "Folder kept; nothing was shipped as ready" : job.folder}
          </span>
        )}
        <span className="grow" />
        <MarkApplied jobId={job.job_id} applied={applied} onChange={onApplied} />
        {ready ? (
          <a className="btn ghost sm" href={href(studio)} onClick={(e) => followLink(e, () => go(studio))}>
            Review and revise
            <ArrowRight aria-hidden="true" />
          </a>
        ) : job.folder && job.state === "running" ? (
          // Offering the studio before cv.tex is finished gives a studio that
          // cannot compile, preview or answer anything.
          <span className="hint">
            <span className="spin" aria-hidden="true" /> Cutting the CV
          </span>
        ) : null}
      </div>
    </div>
  );
}

function outcomeOf(job: JobRun) {
  if (job.state === "approved") return "Approved";
  if (job.state === "cancelled") return "Cancelled";
  if (job.state === "failed") return !job.error ? "Not approved" : job.rounds ? "Stopped" : "Skipped";
  return null;
}

function toneOf(job: JobRun) {
  if (job.state === "running") return "accent";
  if (job.state === "approved") return "ok";
  if (job.state === "failed") return "danger";
  return undefined;
}

function statusOf(job: JobRun) {
  if (job.state === "queued") return "Queued";
  if (job.state === "cancelled") return "Cancelled";
  if (job.state === "approved")
    return `Approved · ${job.rounds} round${job.rounds > 1 ? "s" : ""}${job.fit ? ` · fit ${job.fit.toFixed(2)}` : ""}`;
  if (job.state === "failed") {
    if (!job.error) return `Not approved after ${job.rounds} rounds`;
    return job.rounds ? "Stopped" : "Skipped";
  }
  if (job.pages != null && job.pages > 2) return `Round ${job.rounds} · trimming to two pages`;
  return `Round ${job.rounds || 1}`;
}

function stageState(job: JobRun, index: number) {
  if (job.state === "queued" || job.state === "cancelled") return "todo";
  if (job.state === "approved") return "done";
  if (job.state === "failed") {
    // No folder means it never got past reading the posting. With a folder,
    // the reviewer gate is where a failed CV stopped.
    if (!job.folder) return index === 0 ? "failed" : "todo";
    return index < 2 ? "done" : index === 2 ? "failed" : "todo";
  }
  // The length stage is only live once a cut has been measured and ran long;
  // otherwise the reviewer gate is still where the work is.
  const trimming = job.pages != null && job.pages > 2;
  const at = job.folder ? (trimming ? 3 : job.rounds > 0 ? 2 : 1) : 0;
  return index < at ? "done" : index === at ? "now" : "todo";
}
