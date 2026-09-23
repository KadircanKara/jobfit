import type { BackupRow } from "../api";

export function BackupsPanel({
  backups,
  busy,
  onRestore,
}: {
  backups: BackupRow[];
  busy: boolean;
  onRestore: (name: string) => void;
}) {
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Backups</h2>
        <div className="note">one before every save · never overwritten</div>
      </div>
      {backups.length === 0 ? (
        <div className="empty">No backups yet. The second save makes the first one.</div>
      ) : (
        backups.map((backup) => (
          <div className="backup" key={backup.name}>
            <span className="ts">{backup.taken_at.replace("T", " ").slice(0, 19)}</span>
            <span className="sz">{(backup.size / 1024).toFixed(1)} KB</span>
            <button onClick={() => onRestore(backup.name)} disabled={busy}>
              Restore
            </button>
          </div>
        ))
      )}
    </section>
  );
}
