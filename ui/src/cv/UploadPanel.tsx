import { useEffect, useRef, useState } from "react";
import { cvApi, type TemplateRow, type UploadState } from "./api";

/**
 * Adding a template. A finished CV is converted by an agent, a file that already
 * uses \VAR and \BLOCK is only checked. Either way it is filled with the person's
 * own details, compiled in the sandbox and put through the ATS check, and nothing
 * is added until they have seen the result and named it.
 */
export function UploadPanel({ onAccepted }: { onAccepted: (row: TemplateRow) => void }) {
  const [job, setJob] = useState<UploadState | null>(null);
  const [name, setName] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const [version, setVersion] = useState(0);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    cvApi
      .uploadState()
      .then((state) => {
        setJob(state);
        setName(state.suggested_name);
      })
      .catch((error) => setProblem(String(error)));
  }, []);

  // Converting takes a minute or two, so the page polls.
  useEffect(() => {
    if (job?.state !== "running") return;
    const timer = window.setInterval(async () => {
      const next = await cvApi.uploadState();
      setJob(next);
      if (next.state !== "running") setVersion((current) => current + 1);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [job?.state]);

  async function act(call: () => Promise<void>) {
    setProblem(null);
    try {
      await call();
    } catch (error) {
      setProblem(error instanceof Error ? error.message : String(error));
    }
  }

  const choose = (file: File) =>
    act(async () => {
      const next = await cvApi.uploadTemplate(file);
      setJob(next);
      setName(next.suggested_name);
      setVersion((current) => current + 1);
      if (input.current) input.current.value = "";
    });

  const discard = () => act(async () => setJob(await cvApi.discardUpload()));
  const accept = () =>
    act(async () => {
      const added = await cvApi.acceptUpload(name.trim());
      setJob(await cvApi.uploadState());
      onAccepted(added);
    });

  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Add a template</h2>
        <div className="note">.tex only · built in a sandbox</div>
      </div>
      {problem && <div className="err">{problem}</div>}

      {(!job || job.state === "idle") && (
        <>
          <p className="cv-lede">
            Upload a .tex file. A finished CV is turned into a template by an agent, keeping its look; a file that
            already uses <code>\VAR</code> and <code>\BLOCK</code> is only checked. Either way it is filled with your
            details, compiled with no access to your files, and put through the ATS check before you decide.{" "}
            <a href="/api/cv/contract.md" target="_blank" rel="noreferrer">
              How templates are written
            </a>
          </p>
          <label className="btn">
            Choose a .tex file
            <input
              ref={input}
              type="file"
              accept=".tex"
              hidden
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) choose(file);
              }}
            />
          </label>
        </>
      )}

      {job?.state === "running" && (
        <p className="cv-lede">
          <span className="spin" />{" "}
          {job.mode === "convert"
            ? `Turning ${job.filename} into a template. This takes a minute or two.`
            : `Checking ${job.filename}.`}
        </p>
      )}

      {job?.state === "failed" && (
        <>
          <div className="failbox">{job.error}</div>
          <div className="cv-actions" style={{ marginTop: 12 }}>
            <button className="btn ghost" onClick={discard}>
              Start over
            </button>
          </div>
        </>
      )}

      {job?.state === "done" && (
        <div className="cv-review">
          <div>
            <div className="note">
              {job.filename} ·{" "}
              {job.mode === "convert"
                ? `converted in ${job.rounds} round${job.rounds === 1 ? "" : "s"}`
                : "checked as a template"}{" "}
              · {job.engine}
            </div>
            {job.acceptable ? (
              <Line ok text="Keeps hidden items hidden, prints everything, compiles" />
            ) : (
              job.problems.map((text) => <Line key={text} ok={false} text={text} />)
            )}
            {job.warnings.map((text) => (
              <div className="cv-reason" key={text}>
                {text}
              </div>
            ))}
            {!job.ats_ran && job.ats_note && <div className="note">{job.ats_note}</div>}
            {job.acceptable && (
              <div className="field">
                <label>
                  <span>Name</span>
                </label>
                <input aria-label="Template name" value={name} maxLength={60} onChange={(e) => setName(e.target.value)} />
              </div>
            )}
            <div className="cv-actions">
              {job.acceptable && (
                <button className="btn" onClick={accept} disabled={!name.trim()}>
                  Add to my templates
                </button>
              )}
              <button className="btn ghost" onClick={discard}>
                Discard
              </button>
            </div>
          </div>
          <div className="preview cv-preview">
            {job.has_preview ? (
              <object
                key={version}
                data={`/api/cv/templates/upload/preview.pdf?v=${version}`}
                type="application/pdf"
                aria-label="Template preview"
              />
            ) : (
              <div className="failbox">{job.problems.join("\n\n") || "There is no preview."}</div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

function Line({ ok, text }: { ok: boolean; text: string }) {
  return (
    <div className="cv-check" data-ok={ok}>
      <span className="cv-mark">{ok ? "✓" : "!"}</span>
      <div style={{ whiteSpace: "pre-wrap" }}>{text}</div>
    </div>
  );
}
