import { cvApi } from "./api";
import { usePolledJob } from "./usePolledJob";

/**
 * Bringing today's master.tex in, once. An agent reads it; the review below is
 * the check, not the agent's word: every visible word of the master must come
 * back out of the Classic render of the result.
 */
export function ImportPanel({ onAccepted, onStartEmpty }: { onAccepted: () => void; onStartEmpty: () => void }) {
  const { job, setJob, problem, version, act } = usePolledJob(cvApi.importState);

  const start = () => act(async () => setJob(await cvApi.startImport()));
  const report = job?.report;

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Bring in your master.tex</h2>
        <div className="note">once · master.tex itself is never changed</div>
      </div>
      {problem && <div className="err">{problem}</div>}

      {(!job || job.state === "idle") && (
        <>
          <p className="cv-lede">
            You already have a master CV in LaTeX. An agent can read it into the form, keeping hidden bullets,
            notes and variants. Before anything is saved you see the result checked against your master, word by
            word.
          </p>
          <div className="cv-actions">
            <button className="btn" onClick={start}>
              Import master.tex
            </button>
            <button className="btn ghost" onClick={onStartEmpty}>
              Start from an empty form
            </button>
          </div>
        </>
      )}

      {job?.state === "running" && (
        <p className="cv-lede">
          <span className="spin" /> Reading master.tex. This takes a minute or two.
        </p>
      )}

      {job?.state === "failed" && (
        <>
          <div className="failbox">{job.error}</div>
          <div className="cv-actions" style={{ marginTop: 12 }}>
            <button className="btn" onClick={start}>
              Try again
            </button>
            <button className="btn ghost" onClick={onStartEmpty}>
              Start from an empty form
            </button>
          </div>
        </>
      )}

      {job?.state === "done" && report && (
        <div className="cv-review">
          <div>
            <Check ok={report.preamble_identical} good="Preamble identical to your master" bad="Preamble differs from your master" />
            <Check
              ok={report.missing.length === 0}
              good="Every word of your master came through"
              bad={`${report.missing.length} words of your master are missing`}
              words={report.missing}
            />
            <Check
              ok={report.added.length === 0}
              good="Nothing was added"
              bad={`${report.added.length} words were added that your master does not have`}
              words={report.added}
            />
            <details className="cv-comments">
              <summary>
                Comments: {report.comments_missing.length} words dropped, {report.comments_added.length} added
              </summary>
              Section rules such as <code>%-----EXPERIENCE-----</code> are rewritten, so some difference here is
              expected. Notes and hidden bullets should all be present.
              <div className="cv-words">
                {report.comments_missing.slice(0, 80).map((word, i) => (
                  <code key={i}>{word}</code>
                ))}
              </div>
            </details>
            <div className="cv-actions">
              <button
                className="btn"
                onClick={() =>
                  act(async () => {
                    await cvApi.acceptImport();
                    onAccepted();
                  })
                }
              >
                {report.faithful ? "Accept and fill the form" : "Accept anyway"}
              </button>
              <button className="btn ghost" onClick={() => act(async () => setJob(await cvApi.discardImport()))}>
                Discard
              </button>
            </div>
          </div>
          <div className="preview cv-preview">
            {job.has_preview ? (
              <object data={`/api/cv/import/preview.pdf?v=${version}`} type="application/pdf" aria-label="Imported CV preview" />
            ) : (
              <div className="failbox">{job.log || "The preview did not build."}</div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

function Check({ ok, good, bad, words }: { ok: boolean; good: string; bad: string; words?: string[] }) {
  return (
    <div className="cv-check" data-ok={ok}>
      <span className="cv-mark">{ok ? "✓" : "!"}</span>
      <div>
        <div>{ok ? good : bad}</div>
        {!ok && words && words.length > 0 && (
          <div className="cv-words">
            {words.slice(0, 60).map((word, i) => (
              <code key={i}>{word}</code>
            ))}
            {words.length > 60 && <span>and {words.length - 60} more</span>}
          </div>
        )}
      </div>
    </div>
  );
}
