import { useEffect, useState } from "react";
import { api, type BackupRow } from "./api";

/**
 * master.tex, edited in place. Every save copies the current file aside first,
 * and the backup list below is the way back — including from a restore, which
 * takes a backup of its own before it overwrites anything.
 */
export function Profile() {
  const [text, setText] = useState("");
  const [saved, setSaved] = useState("");
  const [path, setPath] = useState("");
  const [backups, setBackups] = useState<BackupRow[]>([]);
  const [pdf, setPdf] = useState<string | null>(null);
  const [log, setLog] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const dirty = text !== saved;

  useEffect(() => {
    api.profile().then((body) => {
      setText(body.text);
      setSaved(body.text);
      setPath(body.path);
    });
    api.backups().then((body) => setBackups(body.backups));
  }, []);

  async function save() {
    setBusy("saving");
    setError(null);
    try {
      await api.saveProfile(text);
      setSaved(text);
      setBackups((await api.backups()).backups);
    } catch (problem) {
      setError(String(problem));
    } finally {
      setBusy(null);
    }
  }

  async function compile() {
    setBusy("compiling");
    setError(null);
    const result = await api.compileProfile(text);
    setLog(result.ok ? null : result.log);
    setPdf(result.pdf);
    setBusy(null);
  }

  async function restore(name: string) {
    setBusy("restoring");
    await api.restoreBackup(name);
    const body = await api.profile();
    setText(body.text);
    setSaved(body.text);
    setBackups((await api.backups()).backups);
    setBusy(null);
  }

  return (
    <>
      <div className="hero">
        <div>
          <h1>Your background</h1>
          <div className="sub">{path} · the only source every tailored CV is cut from</div>
        </div>
        <div className="runstate" data-state={dirty ? "running" : "idle"}>
          <span className="pulse" />
          <span>{dirty ? "Unsaved changes" : "Saved"}</span>
        </div>
        <div className="runctl">
          <button className="btn ghost" onClick={compile} disabled={busy !== null}>
            {busy === "compiling" ? "Compiling" : "Compile"}
          </button>
          <button className="btn" onClick={save} disabled={!dirty || busy !== null}>
            {busy === "saving" ? "Saving" : "Save and back up"}
          </button>
        </div>
      </div>

      {error && <div className="err">{error}</div>}

      <div className="panel">
        <div className="panel-head">
          <h2>master.tex</h2>
          <div className="note">every save writes a backup first</div>
        </div>
        <div className="editor">
          <textarea
            className="code"
            spellCheck={false}
            value={text}
            onChange={(event) => setText(event.target.value)}
            aria-label="master.tex source"
          />
          <div className="preview">
            {log ? (
              <div className="failbox">{log}</div>
            ) : pdf ? (
              <object data={`data:application/pdf;base64,${pdf}`} type="application/pdf" aria-label="CV preview" />
            ) : (
              <div className="empty">Compile to see the PDF here. The saved file is never touched by a compile.</div>
            )}
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-head">
          <h2>Backups</h2>
          <div className="note">read-only · never overwritten</div>
        </div>
        {backups.length === 0 ? (
          <div className="empty">No backups yet. The first save makes one.</div>
        ) : (
          backups.map((backup) => (
            <div className="backup" key={backup.name}>
              <span className="ts">{backup.taken_at.replace("T", " ").slice(0, 19)}</span>
              <span className="sz">{(backup.size / 1024).toFixed(1)} KB</span>
              <button onClick={() => restore(backup.name)} disabled={busy !== null}>
                Restore
              </button>
            </div>
          ))
        )}
      </div>
    </>
  );
}
