import { useState } from "react";

/** Server refusals keyed by dotted field path, e.g. `experience.0.bullets.2.text`. */
export type Errors = Record<string, string>;

type FieldProps = {
  label: string;
  value: string;
  path: string;
  errors: Errors;
  onChange: (value: string) => void;
  placeholder?: string;
  wide?: boolean;
};

/** A labelled input that shows the server's refusal for its own field. */
export function Text({ label, value, path, errors, onChange, placeholder, wide }: FieldProps) {
  const error = errors[path];
  return (
    <div className={`field${wide ? " wide" : ""}${error ? " bad" : ""}`}>
      <label>
        <span>{label}</span>
      </label>
      <input
        aria-label={label}
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
      {error && <div className="hint cv-bad">{error}</div>}
    </div>
  );
}

export function Area({ label, value, path, errors, onChange, placeholder, rows = 3 }: FieldProps & { rows?: number }) {
  const error = errors[path];
  return (
    <div className={`field${error ? " bad" : ""}`}>
      <label>
        <span>{label}</span>
      </label>
      <textarea
        className={`cv-area${error ? " bad" : ""}`}
        aria-label={label}
        rows={rows}
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
      {error && <div className="hint cv-bad">{error}</div>}
    </div>
  );
}

/**
 * Private notes. Never printed; written into master.tex as comments, because
 * the tailoring agent reads the notes above an entry.
 */
export function Notes({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  // Derived, not remembered: a restore can bring a note into a card that was
  // already on screen with none.
  const [opened, setOpen] = useState(false);
  const open = opened || Boolean(value);
  if (!open) {
    return (
      <button type="button" className="cv-link" onClick={() => setOpen(true)}>
        Add a note
      </button>
    );
  }
  return (
    <textarea
      className="cv-area cv-notes"
      aria-label="Notes"
      rows={2}
      value={value}
      placeholder="Not printed. Kept in master.tex as a comment the tailoring agent reads."
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

export function RowTools({
  index,
  count,
  label,
  onMove,
  onRemove,
  hidden,
  onHide,
}: {
  index: number;
  count: number;
  label: string;
  onMove: (delta: number) => void;
  onRemove?: () => void;
  hidden?: boolean;
  onHide?: () => void;
}) {
  return (
    <div className="cv-tools">
      {onHide && (
        <button
          type="button"
          className="cv-tool"
          aria-pressed={Boolean(hidden)}
          onClick={onHide}
          title="Hidden items are never printed. They stay in master.tex as comments."
        >
          {hidden ? "Hidden" : "Hide"}
        </button>
      )}
      <button type="button" className="cv-tool" onClick={() => onMove(-1)} disabled={index === 0} aria-label={`Move ${label} up`}>
        ↑
      </button>
      <button
        type="button"
        className="cv-tool"
        onClick={() => onMove(1)}
        disabled={index >= count - 1}
        aria-label={`Move ${label} down`}
      >
        ↓
      </button>
      {onRemove && (
        <button type="button" className="cv-tool danger" onClick={onRemove} aria-label={`Remove ${label}`}>
          ✕
        </button>
      )}
    </div>
  );
}

/** Free-text chips. Enter or a comma commits one; backspace on empty removes the last. */
export function ChipInput({
  items,
  onChange,
  placeholder,
}: {
  items: string[];
  onChange: (items: string[]) => void;
  placeholder: string;
}) {
  const [draft, setDraft] = useState("");

  function commit() {
    const value = draft.trim();
    if (value && !items.includes(value)) onChange([...items, value]);
    setDraft("");
  }

  return (
    <div className="cv-chips">
      {items.map((item) => (
        <span key={item} className="cv-chip">
          {item}
          <button type="button" onClick={() => onChange(items.filter((kept) => kept !== item))} aria-label={`Remove ${item}`}>
            ✕
          </button>
        </span>
      ))}
      <input
        value={draft}
        placeholder={items.length ? "" : placeholder}
        aria-label={placeholder}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === ",") {
            event.preventDefault();
            commit();
          } else if (event.key === "Backspace" && !draft && items.length) {
            onChange(items.slice(0, -1));
          }
        }}
      />
    </div>
  );
}
