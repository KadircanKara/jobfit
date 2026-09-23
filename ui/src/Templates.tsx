import { useCallback, useEffect, useState } from "react";
import { cvApi, type TemplateRow } from "./cv/api";
import { UploadPanel } from "./cv/UploadPanel";

/**
 * Every template, showing the person's own details. The default is what the
 * master CV is generated in; choosing another one empties the master CV until
 * it is generated again, so the page asks first.
 */
export function Templates() {
  const [rows, setRows] = useState<TemplateRow[]>([]);
  const [problem, setProblem] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [stamp, setStamp] = useState(0);

  const reload = useCallback(async () => {
    setRows((await cvApi.templates()).templates);
  }, []);

  useEffect(() => {
    reload().catch((error) => setProblem(String(error)));
  }, [reload]);

  async function act(id: string, call: () => Promise<unknown>, done: string) {
    setBusy(id);
    setProblem(null);
    setNotice(null);
    try {
      await call();
      await reload();
      setNotice(done);
    } catch (error) {
      setProblem(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(null);
    }
  }

  function makeDefault(row: TemplateRow) {
    if (!window.confirm(`Make ${row.name} the default? Your master CV will be empty until you generate it in ${row.name}.`)) {
      return;
    }
    act(row.id, () => cvApi.useTemplate(row.id), `${row.name} is the default. Generate your master CV on the Profile page.`);
  }

  function rename(row: TemplateRow) {
    const name = window.prompt("Rename the template", row.name);
    if (!name || name.trim() === row.name) return;
    act(row.id, () => cvApi.renameTemplate(row.id, name.trim()), `Renamed to ${name.trim()}.`);
  }

  function remove(row: TemplateRow) {
    if (!window.confirm(`Remove ${row.name}? It is moved aside, not deleted, and can be restored by hand.`)) return;
    act(row.id, () => cvApi.removeTemplate(row.id), `${row.name} was removed.`);
  }

  return (
    <div className="cv">
      <div className="hero">
        <div>
          <h1>Templates</h1>
          <div className="sub">Every preview shows your own details · the default is what Generate uses</div>
        </div>
      </div>

      {problem && <div className="err">{problem}</div>}
      {notice && <div className="cv-notice">{notice}</div>}

      <div className="cv-gallery">
        {rows.map((row) => (
          <TemplateCard
            key={`${row.id}-${stamp}`}
            row={row}
            stamp={stamp}
            busy={busy === row.id}
            onDefault={() => makeDefault(row)}
            onRename={() => rename(row)}
            onRemove={() => remove(row)}
          />
        ))}
      </div>

      <UploadPanel
        onAccepted={async (row) => {
          await reload();
          setStamp((current) => current + 1);
          setNotice(`${row.name} was added. Make it the default to generate your master CV in it.`);
        }}
      />
    </div>
  );
}

function TemplateCard({
  row,
  stamp,
  busy,
  onDefault,
  onRename,
  onRemove,
}: {
  row: TemplateRow;
  stamp: number;
  busy: boolean;
  onDefault: () => void;
  onRename: () => void;
  onRemove: () => void;
}) {
  const [broken, setBroken] = useState(false);
  const id = encodeURIComponent(row.id);
  return (
    <article className="cv-card" data-default={row.default}>
      <a className="cv-thumb" href={`/api/cv/templates/${id}/preview.pdf?v=${stamp}`} target="_blank" rel="noreferrer">
        {broken ? (
          <span className="empty">No thumbnail here. Open the full preview.</span>
        ) : (
          <img
            src={`/api/cv/templates/${id}/thumbnail.png?v=${stamp}`}
            alt={`${row.name}, filled with your details`}
            loading="lazy"
            onError={() => setBroken(true)}
          />
        )}
      </a>
      <div className="cv-card-body">
        <div className="cv-card-title">
          <b>{row.name}</b>
          {row.default && (
            <span className="cv-state" data-state="ready">
              Default
            </span>
          )}
        </div>
        <div className="note">
          {row.builtin ? "built in" : "uploaded"} · {row.engine}
        </div>
        {row.description && <p className="cv-card-desc">{row.description}</p>}
        <div className="cv-actions">
          {!row.default && (
            <button className="btn sm" onClick={onDefault} disabled={busy}>
              Make default
            </button>
          )}
          <a className="btn ghost sm" href={`/api/cv/templates/${id}/source.tex`}>
            Download .tex
          </a>
          {!row.builtin && (
            <button className="btn ghost sm" onClick={onRename} disabled={busy}>
              Rename
            </button>
          )}
          {!row.builtin && !row.default && (
            <button className="btn ghost sm" onClick={onRemove} disabled={busy}>
              Remove
            </button>
          )}
        </div>
      </div>
    </article>
  );
}
