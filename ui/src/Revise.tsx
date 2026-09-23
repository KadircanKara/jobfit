import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  ChevronDown,
  ChevronRight,
  ExternalLink,
  SendHorizontal,
  TriangleAlert,
  X,
} from "lucide-react";
import { MarkApplied } from "./MarkApplied";
import { api, type RevisableJob, type ReviseSession, type ReviseTurn } from "./api";
import { followLink, href, type Route } from "./app/router";
import { Sheet } from "./app/Sheet";
import { useHunt } from "./app/store";
import { OutreachDrawer } from "./Outreach";
import { hostOf } from "./Run";

/** How often the thread is pulled while the agent is working. An edit plus a
 *  LaTeX build is slow enough that anything tighter is wasted. */
const POLL_MS = 1500;

export function StudioPage({ jobId, go }: { jobId: number; go: (to: Route | string) => void }) {
  const hunt = useHunt();
  const jobs = hunt.revisable;
  const at = jobs.findIndex((row) => row.job_id === jobId);
  const job = at >= 0 ? jobs[at] : undefined;
  const [session, setSession] = useState<ReviseSession | null>(null);
  const [pdf, setPdf] = useState<string | null>(null);
  const [buildLog, setBuildLog] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [outreach, setOutreach] = useState(false);

  // Opening a job whose CV has not been written yet fails, and correctly so.
  // The studio waits for it instead of showing that failure.
  const ready = job?.ready ?? false;

  const refreshPreview = useCallback(async (id: number) => {
    const built = await api.revisionPreview(id);
    setPdf(built.pdf);
    setBuildLog(built.ok ? null : built.log);
  }, []);

  // Opening is idempotent on the server: it resumes the thread if there is one.
  useEffect(() => {
    let live = true;
    setSession(null);
    setPdf(null);
    setBuildLog(null);
    setNotice(null);
    setOutreach(false);
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
    if (!session?.thinking) return;
    const timer = window.setInterval(async () => {
      try {
        const body = await api.revision(jobId);
        setSession(body);
        if (!body.thinking) await refreshPreview(jobId);
      } catch (error) {
        setNotice((error as Error).message);
      }
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [jobId, session?.thinking, refreshPreview]);

  const send = useCallback(
    async (text: string) => {
      if (!text.trim()) return;
      setNotice(null);
      setDraft("");
      try {
        setSession(await api.sendRevision(jobId, text));
      } catch (error) {
        setDraft(text); // give them their words back rather than losing them
        setNotice((error as Error).message);
      }
    },
    [jobId],
  );

  const act = useCallback(
    async (run: () => Promise<ReviseSession>) => {
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

  const moveTo = (index: number) => {
    const next = jobs[index];
    if (next) go({ page: "tailoring", jobId: next.job_id });
  };

  const back = (
    <nav className="crumbs" aria-label="Breadcrumb">
      <a href={href({ page: "tailoring" })} onClick={(e) => followLink(e, () => go({ page: "tailoring" }))}>
        Tailoring
      </a>
      <ChevronRight aria-hidden="true" />
      <span>Revise</span>
    </nav>
  );

  if (!job) {
    return (
      <div className="page">
        {back}
        <div className="empty-state" style={{ marginTop: 16 }}>
          <h2>No tailored CV for job {jobId} in this session</h2>
          <p>
            The studio opens CVs from the current tailoring batch. After a restart the batch is gone, but the
            folder and its files are still on disk.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="page">
      {back}
      <header className="page-header" style={{ marginTop: 6 }}>
        <h1>{job.title || `Job ${job.job_id}`}</h1>
        <span className="sub">{job.company}</span>
        {job.fit != null && <span className="badge num">fit {job.fit.toFixed(2)}</span>}
        <div className="page-actions">
          {job.url && (
            <a className="extlink" href={job.url} target="_blank" rel="noreferrer" title={job.url}>
              {hostOf(job.url)}
              <ExternalLink aria-hidden="true" />
            </a>
          )}
          <button type="button" className="outbtn" aria-expanded={outreach} onClick={() => setOutreach(true)}>
            <span className="dot" data-state={hunt.outreachStates[job.job_id]} />
            Outreach
          </button>
          <MarkApplied
            jobId={job.job_id}
            applied={hunt.applied.has(job.job_id)}
            onChange={(on) => hunt.setApplied(job.job_id, on)}
          />
        </div>
      </header>

      <div className="studio-bar">
        <Picker jobs={jobs} at={at} open={open} onToggle={() => setOpen((was) => !was)} onPick={(index) => {
          setOpen(false);
          moveTo(index);
        }} />
        <button type="button" className="btn ghost sm" disabled={at === 0} onClick={() => moveTo(at - 1)}>
          <ArrowLeft aria-hidden="true" />
          Previous
        </button>
        <button type="button" className="btn ghost sm" disabled={at >= jobs.length - 1} onClick={() => moveTo(at + 1)}>
          Next
          <ArrowRight aria-hidden="true" />
        </button>
        <span className="of">
          CV {at + 1} of {jobs.length}
          {jobs.some((row) => !row.ready) && ` · ${jobs.filter((row) => row.ready).length} ready`}
        </span>
      </div>

      {notice && (
        <div className="notice" data-tone="danger" role="alert" style={{ marginBottom: 12 }}>
          <TriangleAlert className="icon" aria-hidden="true" />
          <span className="grow">{notice}</span>
        </div>
      )}

      {!ready && (
        <div className="notice" style={{ marginBottom: 12 }}>
          <span className="spin" aria-hidden="true" />
          <span className="grow">
            The CV for this job is still being cut. This opens on its own the moment <b>cv.tex</b> lands; the
            list is checked every five seconds.
          </span>
        </div>
      )}

      <div className="studio">
        <div className="chat">
          <Thread turns={session?.turns ?? []} thinking={session?.thinking ?? false} waiting={!ready} />
          <div className="composer">
            <div className="row">
              <textarea
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void send(draft);
                }}
                placeholder="Ask about the posting, or say what to change"
                aria-label="Message the tailoring agent"
                disabled={!ready || session?.thinking}
              />
              <button
                className="btn"
                onClick={() => send(draft)}
                disabled={!ready || !session || session.thinking || !draft.trim()}
                title="Send (Cmd or Ctrl + Enter)"
              >
                <SendHorizontal aria-hidden="true" />
                Send
              </button>
            </div>
            <div className="hint">
              A question gets an answer; a change request makes the agent edit <b>cv.tex</b> and rebuild. There is
              no text editor on purpose: every change is asked for, so every change has a reason in the thread.
            </div>
          </div>
        </div>

        <div className="pdfpane">
          <div className="preview">
            {!ready ? (
              <div className="empty">Waiting for the CV to be cut.</div>
            ) : buildLog ? (
              <div className="failbox">{buildLog}</div>
            ) : pdf ? (
              <object data={`data:application/pdf;base64,${pdf}`} type="application/pdf" aria-label="Tailored CV preview" />
            ) : (
              <div className="empty">
                <span className="spin" aria-hidden="true" /> Building the preview
              </div>
            )}
          </div>

          {session && <Versions session={session} />}
          {session && (
            <SyncBar
              session={session}
              onSync={() => act(() => api.syncRevision(session.job_id))}
              onDiscard={() => {
                if (window.confirm("Discard every revision not yet synced to the folder?")) {
                  void act(() => api.discardRevision(session.job_id));
                }
              }}
              onTrim={() =>
                send("This runs past two pages. Cut it back to two without dropping anything the posting asks for.")
              }
            />
          )}
        </div>
      </div>

      {outreach && (
        <Sheet label="Outreach" title={job.title || `Job ${job.job_id}`} sub={`${job.company} · outreach`} onClose={() => setOutreach(false)}>
          <OutreachDrawer key={job.job_id} jobId={job.job_id} onBudget={hunt.refreshOutreach} />
        </Sheet>
      )}
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
        <ChevronDown className="icon" aria-hidden="true" />
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
              <span className="dot" data-tone={row.state === "running" ? "accent" : row.state === "failed" ? "danger" : "ok"} />
              <span>
                <span className="ttl">{row.title || `job ${row.job_id}`}</span>
                <span className="co">{subtitleOf(row)}</span>
              </span>
              <span className="meta">
                {row.fit != null && <span>{row.fit.toFixed(2)}</span>}
                {row.pages != null && <span>{row.pages} pp</span>}
                {!row.ready ? (
                  <span className="unsynced">cutting</span>
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
              <span>Cutting the CV. I can talk about it once it exists.</span>
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
              <span>Editing cv.tex and rebuilding</span>
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
      {turn.build === "ok" && (
        <span className="ok">
          <Check aria-hidden="true" /> builds
        </span>
      )}
      {turn.build === "failed" && (
        <span className="bad">
          <X aria-hidden="true" /> build failed
        </span>
      )}
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
            {session.pages} pages
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
            : "The folder matches the CV you are looking at"}
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
