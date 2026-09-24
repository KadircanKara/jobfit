import type { MasterStatus, TemplateRow } from "./api";
import { Download as DownloadIcon, FileOutput } from "lucide-react";

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
  templates,
  switching,
  generating,
  onGenerate,
  onPick,
  onOpenTemplates,
}: {
  status: MasterStatus | null;
  dirty: boolean;
  busy: boolean;
  log: string | null;
  missing: string[];
  version: number;
  templates: TemplateRow[];
  switching: boolean;
  generating?: boolean;
  onGenerate: () => void;
  onPick: (id: string) => void;
  onOpenTemplates?: () => void;
}) {
  // Before the first save there is no status yet; the default is still known.
  const chosen = status?.template_id ?? templates.find((row) => row.default)?.id ?? "";
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
      {templates.length > 0 && (
        <div className="cv-picker">
          <label htmlFor="cv-template">Template</label>
          <select
            id="cv-template"
            value={chosen}
            onChange={(event) => onPick(event.target.value)}
            disabled={busy}
          >
            {templates.map((row) => (
              <option key={row.id} value={row.id}>
                {row.name}
              </option>
            ))}
          </select>
          <a
            className="note"
            href="/cv/templates"
            onClick={(event) => {
              if (!onOpenTemplates || event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
              event.preventDefault();
              onOpenTemplates();
            }}
          >
            all templates
          </a>
        </div>
      )}
      <div className="cv-actions">
        <button className="btn" onClick={onGenerate} disabled={busy}>
          {busy ? <span className="spin" aria-hidden="true" /> : <FileOutput aria-hidden="true" />}
          {switching
            ? "Switching template"
            : generating
              ? "Generating"
              : busy
                ? "Working"
                : dirty
                  ? "Save and generate"
                  : "Generate master CV"}
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
      <DownloadIcon aria-hidden="true" />
      {label}
    </a>
  ) : (
    <span className="btn ghost cv-off" aria-disabled="true" title="Generate the master CV first">
      <DownloadIcon aria-hidden="true" />
      {label}
    </span>
  );
}
