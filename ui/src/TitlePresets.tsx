import { useState } from "react";
import { api } from "./api";

/**
 * Named selections of the Titles field.
 *
 * Trying a different set of titles means losing the one you had, and typing it
 * back from memory is where a title quietly goes missing. A preset is the set
 * you already trusted, kept under a name so picking it up again is one click.
 *
 * Picking one only changes the draft. Nothing about the search changes until
 * Save filters is pressed, which keeps "let me look at something else for a
 * minute" from rewriting what the next run actually looks for.
 */
export function TitlePresets({
  groups,
  titles,
  savedTitles,
  onPick,
  onGroups,
}: {
  groups: Record<string, string[]>;
  titles: string[];
  savedTitles: string[];
  onPick: (titles: string[]) => void;
  onGroups: (groups: Record<string, string[]>) => void;
}) {
  const [naming, setNaming] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const names = Object.keys(groups);
  const active = names.find((name) => same(groups[name], titles)) ?? null;
  const clash = naming
    ? names.find((name) => name.toLowerCase() === naming.trim().toLowerCase())
    : undefined;

  async function run(work: () => Promise<{ title_groups: Record<string, string[]> }>) {
    setBusy(true);
    try {
      onGroups((await work()).title_groups);
      setError(null);
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <div className="presets">
        <span className="lbl">Presets</span>

        {names.length === 0 && <span className="none">none saved yet</span>}
        {names.map((name) => (
          <span key={name} className="preset" aria-pressed={active === name}>
            <button
              type="button"
              className="pick"
              disabled={busy}
              onClick={() => onPick(groups[name])}
              title={groups[name].join(", ")}
            >
              {name}
              <span className="n">{groups[name].length}</span>
            </button>
            <button
              type="button"
              className="kill"
              aria-label={`Delete preset ${name}`}
              disabled={busy}
              onClick={() => run(() => api.deleteTitleGroup(name))}
            >
              ×
            </button>
          </span>
        ))}

        <span className="presetacts">
          <button
            type="button"
            className={naming === null ? "linkbtn" : "linkbtn armed"}
            disabled={busy || titles.length === 0}
            title={titles.length ? "Name the titles in the field" : "Add a title first"}
            onClick={() => setNaming(naming === null ? "" : null)}
          >
            Save as…
          </button>
          <button
            type="button"
            className="linkbtn"
            disabled={busy || same(titles, savedTitles)}
            title="Put the field back to what is saved"
            onClick={() => onPick(savedTitles)}
          >
            Reset
          </button>
          <button
            type="button"
            className="linkbtn"
            disabled={busy || titles.length === 0}
            title="Empty the Titles field"
            onClick={() => onPick([])}
          >
            Clear
          </button>
        </span>
      </div>

      {naming !== null && (
        <div className="saverow">
          <input
            autoFocus
            value={naming}
            placeholder="Preset name, e.g. test"
            aria-label="Preset name"
            onChange={(e) => setNaming(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && naming.trim()) commit();
              if (e.key === "Escape") setNaming(null);
            }}
          />
          <button
            type="button"
            className="btn sm"
            disabled={busy || !naming.trim()}
            onClick={commit}
          >
            {clash ? "Overwrite" : "Save preset"}
          </button>
          <button type="button" className="btn ghost sm" onClick={() => setNaming(null)}>
            Cancel
          </button>
          <span className={clash ? "why warn" : "why"}>
            {clash
              ? `“${clash}” already exists — saving replaces its ${groups[clash].length} titles.`
              : `Saves the ${titles.length} title${titles.length === 1 ? "" : "s"} in the field. Your filters are not saved by this.`}
          </span>
        </div>
      )}

      {error && <div className="err">{error}</div>}
    </>
  );

  function commit() {
    const name = (naming ?? "").trim();
    if (!name) return;
    run(async () => {
      const body = await api.saveTitleGroup(name, titles);
      setNaming(null);
      return body;
    });
  }
}

/** Set comparison, case-insensitively: order is not part of a selection. */
function same(a: string[], b: string[]) {
  if (a.length !== b.length) return false;
  const left = [...a].map((s) => s.toLowerCase()).sort();
  const right = [...b].map((s) => s.toLowerCase()).sort();
  return left.every((value, index) => value === right[index]);
}
