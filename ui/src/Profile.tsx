import { useCallback, useEffect, useMemo, useState } from "react";
import { Save, TriangleAlert } from "lucide-react";
import type { BackupRow } from "./api";
import { setLeaveGuard } from "./app/router";
import {
  cvApi,
  ProfileRefused,
  type CvProfile,
  type MasterStatus,
  type ProfileBody,
  type TemplateRow,
} from "./cv/api";
import { BackupsPanel } from "./cv/BackupsPanel";
import { blankProfile } from "./cv/blank";
import type { Errors } from "./cv/Controls";
import { stillApplies } from "./cv/errors";
import { ImportPanel } from "./cv/ImportPanel";
import { MasterPanel } from "./cv/MasterPanel";
import { ProfileForm } from "./cv/ProfileForm";

/**
 * Your background, as fields. master.tex is generated from them and never edited
 * here. Every save backs the profile up first, and the master files are only
 * replaced by a generate that compiled.
 */
export function Profile({ onOpenTemplates }: { onOpenTemplates?: () => void } = {}) {
  const [body, setBody] = useState<ProfileBody | null>(null);
  const [profile, setProfile] = useState<CvProfile | null>(null);
  const [saved, setSaved] = useState("");
  const [errors, setErrors] = useState<Errors>({});
  // The profile as it was when the server refused it: errors are positional,
  // and only shown while their position still holds the same item.
  const [refused, setRefused] = useState<CvProfile | null>(null);
  const [status, setStatus] = useState<MasterStatus | null>(null);
  const [backups, setBackups] = useState<BackupRow[]>([]);
  const [busy, setBusy] = useState<null | "saving" | "generating" | "restoring" | "switching">(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [log, setLog] = useState<string | null>(null);
  const [missing, setMissing] = useState<string[]>([]);
  const [templates, setTemplates] = useState<TemplateRow[]>([]);
  const [version, setVersion] = useState(0);

  const dirty = profile !== null && JSON.stringify(profile) !== saved;
  const shown = useMemo(() => (refused ? stillApplies(errors, refused, profile) : errors), [errors, refused, profile]);

  function clearErrors() {
    setErrors({});
    setRefused(null);
  }

  const reload = useCallback(async () => {
    const next = await cvApi.profile();
    setBody(next);
    setProfile(next.profile);
    setSaved(next.profile ? JSON.stringify(next.profile) : "");
    clearErrors();
    setStatus(next.profile ? await cvApi.master() : null);
    setTemplates((await cvApi.templates()).templates);
    setBackups((await cvApi.backups()).backups);
  }, []);

  useEffect(() => {
    reload().catch((error) => setProblem(String(error)));
  }, [reload]);

  // Edits live only in this tab until saved, so leaving asks first: another
  // page in the app as much as a reload or a closed tab.
  useEffect(() => {
    setLeaveGuard(dirty ? () => window.confirm("Leave this page? Your unsaved profile changes will be lost.") : null);
    return () => setLeaveGuard(null);
  }, [dirty]);

  useEffect(() => {
    if (!dirty) return;
    const guard = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty]);

  async function save(): Promise<boolean> {
    if (!profile) return false;
    setBusy("saving");
    setProblem(null);
    try {
      const result = await cvApi.save(profile);
      setSaved(JSON.stringify(profile));
      clearErrors();
      setStatus(result.master);
      setBackups((await cvApi.backups()).backups);
      return true;
    } catch (error) {
      if (error instanceof ProfileRefused) {
        setErrors(Object.fromEntries(error.problems.map((p) => [p.field, p.message])));
        setRefused(profile);
        const first = error.problems[0];
        setProblem(
          `Not saved: ${error.problems.length} ${error.problems.length === 1 ? "field needs" : "fields need"} attention, starting with ${first.field} (${first.message}).`,
        );
      } else {
        setProblem(error instanceof Error ? error.message : String(error));
      }
      return false;
    } finally {
      setBusy(null);
    }
  }

  async function generate() {
    if (dirty && !(await save())) return;
    setBusy("generating");
    setProblem(null);
    try {
      const result = await cvApi.generate();
      setLog(result.ok ? null : result.log);
      setMissing(result.ok ? result.missing : []);
      setStatus(result.master);
      setVersion((current) => current + 1);
    } catch (error) {
      setProblem(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(null);
    }
  }

  async function pick(id: string) {
    const row = templates.find((candidate) => candidate.id === id);
    if (!row || row.default) return;
    if (!window.confirm(`Switch to ${row.name}? The master CV will be empty until you generate it again.`)) return;
    setBusy("switching");
    try {
      setStatus(await cvApi.useTemplate(id));
      setLog(null);
      setMissing([]);
      setTemplates((await cvApi.templates()).templates);
    } catch (error) {
      setProblem(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(null);
    }
  }

  async function restore(name: string) {
    if (dirty && !window.confirm("Restoring replaces your unsaved edits. Continue?")) return;
    setBusy("restoring");
    try {
      await cvApi.restore(name);
      await reload();
    } catch (error) {
      setProblem(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="page">
      <header className="page-header">
        <h1>Profile</h1>
        {profile && (
          <span className="badge" data-tone={dirty ? "warn" : "ok"}>
            <span className="dot" />
            {dirty ? "Unsaved changes" : "Saved"}
          </span>
        )}
        <span className="sub">
          The only source every CV is cut from
        </span>
        {profile && (
          <div className="page-actions">
            <button className="btn" onClick={save} disabled={!dirty || busy !== null}>
              {busy === "saving" ? <span className="spin" aria-hidden="true" /> : <Save aria-hidden="true" />}
              {busy === "saving" ? "Saving" : "Save and back up"}
            </button>
          </div>
        )}
      </header>

      {problem && (
        <div className="notice" data-tone="danger" role="alert" style={{ marginBottom: 16 }}>
          <TriangleAlert className="icon" aria-hidden="true" />
          <span className="grow">{problem}</span>
        </div>
      )}

      {body && !profile && body.import_available && (
        <ImportPanel onAccepted={() => reload()} onStartEmpty={() => setProfile(blankProfile())} />
      )}
      {body && !profile && !body.import_available && (
        <section className="panel">
          <div className="panel-head">
            <h2>Start your profile</h2>
          </div>
          <p className="cv-lede">Fill in your details once. The master CV and every tailored CV are cut from them.</p>
          <button className="btn" onClick={() => setProfile(blankProfile())}>
            Start
          </button>
        </section>
      )}

      {profile && (
        <div className="cv-layout">
          <div>
            <ProfileForm profile={profile} errors={shown} onChange={setProfile} />
          </div>
          <aside className="cv-side">
            <MasterPanel
              status={status}
              dirty={dirty}
              busy={busy !== null}
              switching={busy === "switching"}
              generating={busy === "generating"}
              onOpenTemplates={onOpenTemplates}
              log={log}
              missing={missing}
              templates={templates}
              onPick={pick}
              version={version}
              onGenerate={generate}
            />
          </aside>
        </div>
      )}

      {profile && (
        <div style={{ marginTop: 16 }}>
          <BackupsPanel backups={backups} busy={busy !== null} onRestore={restore} />
        </div>
      )}
    </div>
  );
}
