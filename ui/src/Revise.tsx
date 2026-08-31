import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { MarkApplied } from "./MarkApplied";
import {
  api,
  type OutreachBudget,
  type OutreachStates,
  type RevisableJob,
  type ReviseSession,
  type ReviseTurn,
} from "./api";
import { OutreachDrawer } from "./Outreach";
import { hostOf } from "./Run";

/** How often the thread is pulled while the agent is working. An edit plus a
 *  LaTeX build is slow enough that anything tighter is wasted. */
const POLL_MS = 1500;

export function ReviseStudio({
  jobs,
  focus,
  applied,
  onApplied,
  outreachStates,
  onBudget,
}: {
  jobs: RevisableJob[];
  focus?: number | null;
  applied: Set<number>;
  onApplied: (jobId: number, applied: boolean) => void;
  outreachStates?: OutreachStates;
  onBudget?: (budget: OutreachBudget) => void;
}) {
  const [at, setAt] = useState(0);
  const [session, setSession] = useState<ReviseSession | null>(null);
  const [pdf, setPdf] = useState<string | null>(null);
  const [buildLog, setBuildLog] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  // Collapsed by default - the studio is a focused writing view, and a
  // permanently expanded drawer would crowd it. `key={jobId}` on the mounted
  // drawer below (not a reset here) is what keeps it from ever showing the
  // previous job's contacts after a prev/next switch: it forces a fresh
  // mount, so it fetches for the new job instead of reusing stale state.
  const [openOutreach, setOpenOutreach] = useState(false);

  const job = jobs[at];
  const jobId = job?.job_id;
  // Opening a job whose CV has not been written yet fails, and correctly so.
  // The studio waits for it instead of showing that failure.
  const ready = job?.ready ?? false;

  // "Review & revise" on a batch row picks that CV rather than whichever one
  // the studio happened to be showing.
  useEffect(() => {
    if (focus == null) return;
    const index = jobs.findIndex((row) => row.job_id === focus);
    if (index >= 0) setAt(index);
  }, [focus, jobs]);

  const refreshPreview = useCallback(async (id: number) => {
    const built = await api.revisionPreview(id);
    setPdf(built.pdf);
    setBuildLog(built.ok ? null : built.log);
  }, []);

  // Opening is idempotent on the server: it resumes the thread if there is one.
  useEffect(() => {
    if (jobId == null) return;
    let live = true;
    setSession(null);
    setPdf(null);
    setBuildLog(null);
    setNotice(null);
    if (!ready) return;
    api
      .openRevision(jobId)
      .then((body) => {
        if (!live) return;
        setSession(body);
        return refreshPreview(jobId);
      })
      .catch((error: Error) => live && setNotice(error.message));
    return () => {
      live = false;
    };
  }, [jobId, ready, refreshPreview]);

  // While the agent works the thread is polled, and the preview is rebuilt once
  // it stops, because that is the only moment the PDF can have changed.
  useEffect(() => {
    if (jobId == null || !session?.thinking) return;
    const timer = window.setInterval(async () => {
      const body = await api.revision(jobId);
      setSession(body);
      if (!body.thinking) await refreshPreview(jobId);
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [jobId, session?.thinking, refreshPreview]);

  const send = useCallback(async () => {
    if (jobId == null || !draft.trim()) return;
    setNotice(null);
    const text = draft;
    setDraft("");
    try {
      setSession(await api.sendRevision(jobId, text));
    } catch (error) {
      setDraft(text); // give them their words back rather than losing them
      setNotice((error as Error).message);
    }
  }, [jobId, draft]);

  const act = useCallback(
    async (run: () => Promise<ReviseSession>) => {
      if (jobId == null) return;
      setNotice(null);
      try {
        setSession(await run());
        await refreshPreview(jobId);
      } catch (error) {
        setNotice((error as Error).message);
      }
    },
    [jobId, refreshPreview],
  );

  if (!jobs.length) return null;

  return (
    <div className="panel">
      <div className="panel-head">
        <h2>Revision studio</h2>
        <div className="note">
          {jobs.some((row) => !row.ready)
            ? `${jobs.filter((row) => row.ready).length} of ${jobs.length} ready`
            : "optional · the folder already has a finished CV"}
        </div>
        {job?.url && (
          <a className="posting-link" href={job.url} target="_blank" rel="noreferrer" title={job.url}>
            {hostOf(job.url)} ↗
          </a>
        )}
        {jobId != null && (
          <button
            type="button"
            className="outbtn"
            aria-expanded={openOutreach}
            onClick={() => setOpenOutreach((was) => !was)}
          >
            <span className="dot" data-state={outreachStates?.[jobId]} />
            <span>Outreach</span>
            <span className="caret">{openOutreach ? "▾" : "▸"}</span>
          </button>
        )}
        {jobId != null && (
          <MarkApplied
            jobId={jobId}
            applied={applied.has(jobId)}
            onChange={(on) => onApplied(jobId, on)}
          />
        )}
      </div>

      {openOutreach && jobId != null && (
        <div className="studio-outreach">
          {/* Keyed by jobId so switching CVs with prev/next remounts the
              drawer instead of reusing one wired to the job just left. */}
          <OutreachDrawer key={jobId} jobId={jobId} onBudget={onBudget} />
        </div>
      )}

      <div className="stepper">
        <Picker
          jobs={jobs}
          at={at}
          open={open}
          onToggle={() => setOpen((was) => !was)}
          onPick={(index) => {
            setAt(index);
            setOpen(false);
          }}
        />
        <div className="dots" aria-hidden="true">
          {jobs.map((row, index) => (
            <span
              key={row.job_id}
              className="dot"
              data-s={index === at ? "now" : index < at ? "done" : undefined}
            />
          ))}
        </div>
        <span className="of">
          CV {at + 1} of {jobs.length}
        </span>
        <div className="nav">
          <button type="button" disabled={at === 0} onClick={() => setAt(at - 1)}>
            ← Previous
          </button>
          <button type="button" disabled={at >= jobs.length - 1} onClick={() => setAt(at + 1)}>
            Next →
          </button>
        </div>
      </div>

      {notice && <div className="err">{notice}</div>}

      {!ready && (
        <div className="fx">
          The CV for this job is still being cut. This opens on its own the moment{" "}
          <b>cv.tex</b> lands — checking every five seconds.
        </div>
      )}

      <div className="studio">
        <div className="chat">
          <Thread
            turns={session?.turns ?? []}
            thinking={session?.thinking ?? false}
            waiting={!ready}
          />
          <div className="composer">
            <div className="row">
              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void send();
                }}
                placeholder="Ask about the posting, or say what to change…"
                aria-label="Message the tailoring agent"
                disabled={!ready || session?.thinking}
              />
              <button
                className="btn"
                onClick={send}
                disabled={!ready || !session || session.thinking || !draft.trim()}
              >
                Send
              </button>
            </div>
            <div className="hint">
              Ask a question and you get an answer; ask for a change and the agent edits{" "}
              <b>cv.tex</b> and rebuilds. There is no text editor here on purpose — every change is
              asked for, so every change has a reason in the thread above it.
            </div>
          </div>
        </div>

        <div className="pdfpane">
          <div className="preview">
            {!ready ? (
              <div className="empty">Waiting for the CV to be cut…</div>
            ) : buildLog ? (
              <div className="failbox">{buildLog}</div>
            ) : pdf ? (
              <object
                data={`data:application/pdf;base64,${pdf}`}
                type="application/pdf"
                aria-label="Tailored CV preview"
              />
            ) : (
              <div className="empty">Building the preview…</div>
            )}
          </div>

          {session && <Versions session={session} />}
          {session && (
            <SyncBar
              session={session}
              onSync={() => act(() => api.syncRevision(session.job_id))}
              onDiscard={() => act(() => api.discardRevision(session.job_id))}
              onTrim={async () => {
                setDraft("");
                setSession(
                  await api.sendRevision(
                    session.job_id,
                    "This runs past two pages. Cut it back to two without dropping anything the posting asks for.",
                  ),
                );
              }}
            />
          )}
        </div>
      </div>
    </div>
  );
}

function Picker({
  jobs,
  at,
  open,
  onToggle,
  onPick,
}: {
  jobs: RevisableJob[];
  at: number;
  open: boolean;
  onToggle: () => void;
  onPick: (index: number) => void;
}) {
  const box = useRef<HTMLDivElement>(null);
  const here = jobs[at];

  // A menu that stays open after you click away is a menu you have to fight.
  useEffect(() => {
    if (!open) return;
    const away = (event: MouseEvent) => {
      if (!box.current?.contains(event.target as Node)) onToggle();
    };
    const escape = (event: KeyboardEvent) => event.key === "Escape" && onToggle();
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", escape);
    };
  }, [open, onToggle]);

  return (
    <div className="picker" ref={box}>
      <button className="trigger" type="button" aria-expanded={open} aria-haspopup="listbox" onClick={onToggle}>
        <span>
          <span className="ttl">{here.title || `job ${here.job_id}`}</span>
          <span className="co">{subtitleOf(here)}</span>
        </span>
        <span className="caret" aria-hidden="true">
          {open ? "▴" : "▾"}
        </span>
      </button>

      {open && (
        <div className="menu" role="listbox" aria-label="Pick a tailored CV">
          {jobs.map((row, index) => (
            <button
              className="mrow"
              type="button"
              role="option"
              key={row.job_id}
              data-state={row.state}
              aria-current={index === at}
              onClick={() => onPick(index)}
            >
              <span className="mk" />
              <span>
                <span className="ttl">{row.title || `job ${row.job_id}`}</span>
                <span className="co">{subtitleOf(row)}</span>
              </span>
              <span className="meta">
                {row.fit != null && <span>{row.fit.toFixed(2)}</span>}
                {row.pages != null && <span>{row.pages} pp</span>}
                {!row.ready ? (
                  <span className="unsynced">cutting…</span>
                ) : row.ahead > 0 ? (
                  <span className="unsynced">{row.ahead} ahead</span>
                ) : row.opened ? (
                  <span>synced</span>
                ) : null}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

function subtitleOf(job: RevisableJob) {
  const bits = [job.company];
  if (job.state === "running") bits.push("still tailoring");
  else if (job.state === "failed") bits.push(`not approved in ${job.rounds} rounds`);
  else if (job.fit != null) bits.push(`fit ${job.fit.toFixed(2)}`);
  return bits.filter(Boolean).join(" · ");
}

function Thread({
  turns,
  thinking,
  waiting,
}: {
  turns: ReviseTurn[];
  thinking: boolean;
  waiting?: boolean;
}) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [turns.length, thinking]);

  if (waiting) {
    return (
      <div className="turns">
        <div className="turn agent">
          <div className="nm">Tailoring agent</div>
          <div className="bubble">
            <div className="thinking">
              <span className="pulse" />
              <span>cutting the CV — I can talk about it once it exists…</span>
            </div>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="turns" ref={box}>
      {turns.map((turn, index) => (
        <div className={`turn ${turn.role}`} key={index}>
          <div className="nm">{turn.role === "you" ? "You" : "Tailoring agent"}</div>
          <div className={`bubble${turn.kind === "refusal" ? " refused" : ""}`}>
            {turn.text}
            {turn.changes.length > 0 && (
              <ul className="changes">
                {turn.changes.map((change) => (
                  <li key={change}>{change}</li>
                ))}
              </ul>
            )}
            {turn.log && <div className="failbox" style={{ marginTop: 10 }}>{turn.log}</div>}
            {turn.role === "agent" && turn.kind !== "answer" && <Built turn={turn} />}
          </div>
        </div>
      ))}

      {thinking && (
        <div className="turn agent">
          <div className="nm">Tailoring agent</div>
          <div className="bubble">
            <div className="thinking">
              <span className="pulse" />
              <span>editing cv.tex and rebuilding…</span>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function Built({ turn }: { turn: ReviseTurn }) {
  if (!turn.build && turn.version == null) return null;
  return (
    <div className="built">
      {turn.build === "ok" && <span className="ok">✓ builds</span>}
      {turn.build === "failed" && <span className="bad">✗ build failed</span>}
      {!turn.build && <span>no change</span>}
      {turn.pages != null && <span>{turn.pages} pages</span>}
      {turn.version != null && (
        <span>
          {turn.build === "ok" ? "" : "still "}v{turn.version}
        </span>
      )}
    </div>
  );
}

function Versions({ session }: { session: ReviseSession }) {
  const all = useMemo(
    () => Array.from({ length: session.version }, (_, index) => index + 1),
    [session.version],
  );
  return (
    <div className="vstrip">
      {all.map((version) => (
        <span
          className="ver"
          key={version}
          data-s={
            version === session.version
              ? "now"
              : version === session.synced_version
                ? "synced"
                : undefined
          }
        >
          v{version}
          {version === session.version
            ? " · showing"
            : version === session.synced_version
              ? " · synced"
              : ""}
        </span>
      ))}
    </div>
  );
}

function SyncBar({
  session,
  onSync,
  onDiscard,
  onTrim,
}: {
  session: ReviseSession;
  onSync: () => void;
  onDiscard: () => void;
  onTrim: () => void;
}) {
  const ahead = session.ahead > 0;
  return (
    <>
      {session.over_length && (
        <div className="synced" data-state="ahead">
          <span className="pages" data-over="true">
            {session.pages} pp · over
          </span>
          <span>Two pages is what the batch cuts to. Past that it is your call.</span>
          <span className="acts">
            <button className="btn ghost sm" onClick={onTrim} disabled={session.thinking}>
              Ask the agent to trim
            </button>
          </span>
        </div>
      )}

      <div className="synced" data-state={ahead ? "ahead" : "clean"}>
        <span>
          {ahead
            ? `${session.ahead} revision${session.ahead > 1 ? "s" : ""} ahead of the folder`
            : "✓ Folder matches the CV you are looking at"}
        </span>
        <span className="where" title={session.folder}>
          {folderName(session.folder)}
        </span>
        <span className="acts">
          <button className="btn ghost sm" onClick={onDiscard} disabled={!ahead || session.thinking}>
            Discard
          </button>
          <button className="btn sm" onClick={onSync} disabled={!ahead || session.thinking}>
            Sync to folder
          </button>
        </span>
      </div>
    </>
  );
}

function folderName(path: string) {
  const parts = path.split("/").filter(Boolean);
  return parts.slice(-2).join(" / ");
}
