import { useEffect, useMemo, useRef, useState } from "react";
import { X } from "lucide-react";
import type { VocabRow } from "./api";

type Props = {
  label: string;
  wide?: boolean;
  tags: string[];
  vocab: VocabRow[];
  // False for a fixed list of suggestions with no corpus behind it, where a
  // "0 jobs" beside every choice would read as a warning rather than a fact.
  counts?: boolean;
  placeholder: string;
  freeNote: string;
  emptyNote: string;
  hint?: React.ReactNode;
  // Rendered between the input and the hint, so anything attached to the field
  // (the title presets) reads as part of it rather than as a stray control.
  children?: React.ReactNode;
  onChange: (tags: string[]) => void;
};

/**
 * Tags with autocomplete. The counts beside each suggestion are the point:
 * dropping a title silently removes every job it would have matched, so the
 * number belongs next to the choice rather than in a report afterwards.
 */
export function TagField({
  label,
  wide,
  tags,
  vocab,
  counts = true,
  placeholder,
  freeNote,
  emptyNote,
  hint,
  children,
  onChange,
}: Props) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [cursor, setCursor] = useState(0);
  const entry = useRef<HTMLInputElement>(null);

  const taken = useMemo(() => new Set(tags.map((t) => t.toLowerCase())), [tags]);

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    return vocab
      .filter((row) => !taken.has(row.value.toLowerCase()))
      .filter((row) => !q || row.value.toLowerCase().includes(q) || row.label.toLowerCase().includes(q))
      .sort((a, b) => {
        if (!q) return b.count - a.count;
        const ai = a.value.toLowerCase().startsWith(q) ? 0 : 1;
        const bi = b.value.toLowerCase().startsWith(q) ? 0 : 1;
        return ai - bi || b.count - a.count;
      })
      .slice(0, 8);
  }, [vocab, query, taken]);

  const exact = vocab.some((row) => row.value.toLowerCase() === query.trim().toLowerCase());
  const freeform = query.trim() && !exact && !taken.has(query.trim().toLowerCase());
  const options = freeform ? [query.trim(), ...matches.map((m) => m.value)] : matches.map((m) => m.value);

  useEffect(() => setCursor(0), [query]);

  function add(value: string) {
    const text = value.trim().replace(/,$/, "");
    if (!text) return;
    const canonical = vocab.find((row) => row.value.toLowerCase() === text.toLowerCase());
    const final = canonical ? canonical.value : text;
    if (!taken.has(final.toLowerCase())) onChange([...tags, final]);
    setQuery("");
  }

  function onKeyDown(event: React.KeyboardEvent<HTMLInputElement>) {
    if (event.key === "ArrowDown" && open) {
      event.preventDefault();
      setCursor((c) => (c + 1) % Math.max(options.length, 1));
    } else if (event.key === "ArrowUp" && open) {
      event.preventDefault();
      setCursor((c) => (c - 1 + options.length) % Math.max(options.length, 1));
    } else if (event.key === "Enter" || event.key === ",") {
      event.preventDefault();
      add(open && options[cursor] ? options[cursor] : query);
      setOpen(false);
    } else if (event.key === "Escape" || event.key === "Tab") {
      setOpen(false);
    } else if (event.key === "Backspace" && !query && tags.length) {
      onChange(tags.slice(0, -1));
    }
  }

  function highlight(value: string) {
    const q = query.trim();
    if (!q) return <b>{value}</b>;
    const at = value.toLowerCase().indexOf(q.toLowerCase());
    if (at < 0) return <b>{value}</b>;
    return (
      <b>
        {value.slice(0, at)}
        <mark>{value.slice(at, at + q.length)}</mark>
        {value.slice(at + q.length)}
      </b>
    );
  }

  return (
    <div className={wide ? "field wide" : "field"}>
      <label>
        <span>{label}</span>
      </label>
      <div className="tagwrap">
        <div className="taginput" onClick={(e) => e.target === e.currentTarget && entry.current?.focus()}>
          {tags.map((tag) => (
            <span className="tagitem" key={tag}>
              <span>{tag}</span>
              <button
                aria-label={`Remove ${tag}`}
                onClick={() => onChange(tags.filter((t) => t !== tag))}
              >
                <X aria-hidden="true" />
              </button>
            </span>
          ))}
          <input
            ref={entry}
            role="combobox"
            aria-expanded={open}
            aria-autocomplete="list"
            placeholder={placeholder}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setOpen(true);
            }}
            onFocus={() => setOpen(true)}
            onBlur={() => window.setTimeout(() => setOpen(false), 120)}
            onKeyDown={onKeyDown}
          />
        </div>
        {open && (
          <ul className="acbox" role="listbox">
            {freeform && (
              <li
                className="acitem"
                role="option"
                aria-selected={cursor === 0}
                onMouseDown={(e) => {
                  e.preventDefault();
                  add(query);
                }}
              >
                Add <b>“{query.trim()}”</b>
                <span className="n">{freeNote}</span>
              </li>
            )}
            {matches.map((row, index) => {
              const at = freeform ? index + 1 : index;
              return (
                <li
                  key={row.value}
                  className="acitem"
                  role="option"
                  aria-selected={cursor === at}
                  onMouseEnter={() => setCursor(at)}
                  onMouseDown={(e) => {
                    e.preventDefault();
                    add(row.value);
                  }}
                >
                  {highlight(row.value)}
                  {row.label && <span className="co">{row.label}</span>}
                  {counts && <span className="n">{row.count.toLocaleString()} jobs</span>}
                </li>
              );
            })}
            {!options.length && <li className="acempty">{emptyNote}</li>}
          </ul>
        )}
      </div>
      {children}
      {hint && <div className="hint">{hint}</div>}
    </div>
  );
}
