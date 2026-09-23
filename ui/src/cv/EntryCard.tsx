import type { Bullet, Entry } from "./api";
import { blankBullet, blankEntry, move, removeAt, replaceAt } from "./blank";
import { Notes, RowTools, Text, type Errors } from "./Controls";

export type EntryKind = "experience" | "education" | "projects" | "custom";

const LABELS: Record<EntryKind, { title: string; subtitle: string; start: string; end: string }> = {
  experience: { title: "Organisation", subtitle: "Role", start: "April 2026", end: "Nov 2023" },
  education: { title: "School", subtitle: "Degree", start: "Spring 2022", end: "Fall 2025" },
  projects: { title: "Project", subtitle: "Subtitle", start: "Jun 2026", end: "Jul 2026" },
  custom: { title: "Title", subtitle: "Subtitle", start: "2025", end: "" },
};

export function EntryList({
  entries,
  kind,
  path,
  errors,
  addLabel,
  onChange,
}: {
  entries: Entry[];
  kind: EntryKind;
  path: string;
  errors: Errors;
  addLabel: string;
  onChange: (next: Entry[]) => void;
}) {
  return (
    <>
      {entries.map((entry, index) => (
        <EntryCard
          key={entry.id}
          entry={entry}
          kind={kind}
          path={`${path}.${index}`}
          errors={errors}
          index={index}
          count={entries.length}
          onChange={(next) => onChange(replaceAt(entries, index, next))}
          onMove={(delta) => onChange(move(entries, index, delta))}
          onRemove={() => {
            if (window.confirm(`Delete ${entry.title || "this entry"}? Hide keeps it out of the CV without losing it.`)) {
              onChange(removeAt(entries, index));
            }
          }}
        />
      ))}
      <button type="button" className="btn ghost sm" onClick={() => onChange([...entries, blankEntry()])}>
        {addLabel}
      </button>
    </>
  );
}

export function EntryCard({
  entry,
  kind,
  path,
  errors,
  index,
  count,
  onChange,
  onMove,
  onRemove,
}: {
  entry: Entry;
  kind: EntryKind;
  path: string;
  errors: Errors;
  index: number;
  count: number;
  onChange: (next: Entry) => void;
  onMove: (delta: number) => void;
  onRemove: () => void;
}) {
  const set = <K extends keyof Entry>(key: K, value: Entry[K]) => onChange({ ...entry, [key]: value });
  const labels = LABELS[kind];
  const present = entry.end === "Present";

  return (
    <div className="cv-entry" data-hidden={entry.hidden}>
      <div className="cv-entry-head">
        <span className="cv-entry-name">{entry.title || "Untitled"}</span>
        {entry.hidden && <span className="cv-flag">hidden · kept in master.tex as a comment</span>}
        <RowTools
          index={index}
          count={count}
          label={entry.title || "entry"}
          onMove={onMove}
          onRemove={onRemove}
          hidden={entry.hidden}
          onHide={() => set("hidden", !entry.hidden)}
        />
      </div>
      {errors[path] && <div className="err">{errors[path]}</div>}

      <div className="fields">
        <Text label={labels.title} value={entry.title} path={`${path}.title`} errors={errors} onChange={(v) => set("title", v)} wide />
        <Text
          label={labels.subtitle}
          value={entry.subtitle}
          path={`${path}.subtitle`}
          errors={errors}
          onChange={(v) => set("subtitle", v)}
          wide
        />
        <Text
          label="Start"
          value={entry.start}
          path={`${path}.start`}
          errors={errors}
          onChange={(v) => set("start", v)}
          placeholder={labels.start}
        />
        <div className="field">
          <label>
            <span>End</span>
          </label>
          <div className="cv-end">
            <input
              aria-label="End"
              value={present ? "" : entry.end}
              disabled={present}
              placeholder={present ? "Present" : labels.end}
              onChange={(event) => set("end", event.target.value)}
            />
            <label className="checkline">
              <input type="checkbox" checked={present} onChange={(event) => set("end", event.target.checked ? "Present" : "")} />
              <span>Present</span>
            </label>
          </div>
        </div>
        <Text label="Location" value={entry.location} path={`${path}.location`} errors={errors} onChange={(v) => set("location", v)} wide />
        {kind === "education" ? (
          <Text label="GPA" value={entry.gpa} path={`${path}.gpa`} errors={errors} onChange={(v) => set("gpa", v)} placeholder="3.67/4.00" />
        ) : (
          <Text label="Link" value={entry.url} path={`${path}.url`} errors={errors} onChange={(v) => set("url", v)} placeholder="https://" />
        )}
      </div>
      <Notes value={entry.notes} onChange={(v) => set("notes", v)} />

      <div className="cv-bullets">
        {entry.bullets.map((bullet, i) => (
          <BulletRow
            key={bullet.id}
            bullet={bullet}
            path={`${path}.bullets.${i}`}
            errors={errors}
            index={i}
            count={entry.bullets.length}
            onChange={(next) => set("bullets", replaceAt(entry.bullets, i, next))}
            onMove={(delta) => set("bullets", move(entry.bullets, i, delta))}
            onRemove={() => set("bullets", removeAt(entry.bullets, i))}
          />
        ))}
        <div>
          <button type="button" className="btn ghost sm" onClick={() => set("bullets", [...entry.bullets, blankBullet()])}>
            Add bullet
          </button>
        </div>
      </div>
    </div>
  );
}

function BulletRow({
  bullet,
  path,
  errors,
  index,
  count,
  onChange,
  onMove,
  onRemove,
}: {
  bullet: Bullet;
  path: string;
  errors: Errors;
  index: number;
  count: number;
  onChange: (next: Bullet) => void;
  onMove: (delta: number) => void;
  onRemove: () => void;
}) {
  const error = errors[`${path}.text`];
  return (
    <div className="cv-bullet" data-hidden={bullet.hidden}>
      <span className="cv-dot">•</span>
      <div>
        <textarea
          className={`cv-area${error ? " bad" : ""}`}
          aria-label="Bullet"
          rows={2}
          value={bullet.text}
          placeholder="What you did and what it changed. **Bold** the keyword a recruiter searches for."
          onChange={(event) => onChange({ ...bullet, text: event.target.value })}
        />
        {error && <div className="hint cv-bad">{error}</div>}
        <Notes value={bullet.notes} onChange={(notes) => onChange({ ...bullet, notes })} />
      </div>
      <RowTools
        index={index}
        count={count}
        label="bullet"
        onMove={onMove}
        onRemove={onRemove}
        hidden={bullet.hidden}
        onHide={() => onChange({ ...bullet, hidden: !bullet.hidden })}
      />
    </div>
  );
}
