import type { MasterStatus } from "./api";

const LABEL = { empty: "Empty", ready: "Ready", stale: "Out of date" } as const;

/**
 * The generated master CV. Downloads exist only while there is a master in the
 * current template: after a template change the panel is empty until the next
 * generate compiles.
 */
export function MasterPanel({
  status,
  dirty,
  busy,
  log,
  missing,
  version,
  onGenerate,
}: {
  status: MasterStatus | null;
  dirty: boolean;
  busy: boolean;
  log: string | null;
  missing: string[];
  version: number;
  onGenerate: () => void;
}) {
  const available = status?.state === "ready" || status?.state === "stale";
  return (
    <section className="panel">
      <div className="panel-head">
        <h2>Master CV</h2>
        {status && (
          <span className="cv-state" data-state={status.state}>
            {LABEL[status.state]}
          </span>
        )}
        {status && <div className="note">{status.template_name}</div>}
      </div>
      <div className="cv-actions">
        <button className="btn" onClick={onGenerate} disabled={busy}>
          {busy ? "Generating" : dirty ? "Save and generate" : "Generate master CV"}
        </button>
        <Download href="/api/cv/master.pdf?download=1" enabled={available} label="PDF" />
        <Download href="/api/cv/master.tex" enabled={available} label=".tex" />
      </div>
      {available && status?.reason && <div className="cv-reason">{status.reason}</div>}
      {available && missing.length > 0 && (
        <div className="cv-reason">
          This template's font cannot print {missing.join(" ")}, so {missing.length === 1 ? "it was" : "they were"} left
          out of the PDF.
        </div>
      )}
      <div className="preview cv-preview">
        {log ? (
          <div className="failbox">{log}</div>
        ) : available ? (
          // Keyed as well as cache-busted: a browser may keep an embedded PDF when
          // only its data attribute changes.
          <object
            key={version}
            data={`/api/cv/master.pdf?v=${version}`}
            type="application/pdf"
            aria-label="Master CV preview"
          />
        ) : (
          <div className="empty">{status?.reason ?? "Generate to see your CV here."}</div>
        )}
      </div>
      {available && status?.generated_at && (
        <div className="note cv-when">generated {status.generated_at.replace("T", " ").slice(0, 16)} UTC</div>
      )}
    </section>
  );
}

function Download({ href, enabled, label }: { href: string; enabled: boolean; label: string }) {
  // Not a disabled link but no link at all: without an href it cannot be
  // followed, middle-clicked or opened in a tab while the master is empty.
  return enabled ? (
    <a className="btn ghost" href={href}>
      Download {label}
    </a>
  ) : (
    <span className="btn ghost cv-off" aria-disabled="true">
      Download {label}
    </span>
  );
}
