import { useEffect, useRef, useState } from "react";
import { MoreHorizontal } from "lucide-react";
import { api, type RunSummary } from "../api";
import { followLink, href, type Route } from "./router";
import { stamp, useHunt } from "./store";

/** The saved runs, listed under Shortlist in the sidebar. A date opens what that
 *  run found; the run's own numbers are one link away from there. */
export function RunHistory({ route, go }: { route: Route; go: (to: Route | string) => void }) {
  const hunt = useHunt();
  const liveKept = hunt.run.results.filter((row) => !row.below_bar).length;
  const shownId = route.page === "shortlist" || route.page === "run" ? route.runId : undefined;

  return (
    <div className="nav-sub" role="group" aria-label="Runs">
      <a
        className="nav-subitem"
        href={href({ page: "shortlist" })}
        aria-current={route.page === "shortlist" && !route.runId ? "page" : undefined}
        onClick={(event) => followLink(event, () => go({ page: "shortlist" }))}
      >
        <span className="label">Live</span>
        {liveKept > 0 && <span className="count">{liveKept}</span>}
      </a>
      {hunt.past.map((row) => (
        <RunRow
          key={row.run_id}
          row={row}
          current={shownId === row.run_id}
          go={go}
          onChanged={hunt.refreshPast}
        />
      ))}
    </div>
  );
}

function RunRow({
  row,
  current,
  go,
  onChanged,
}: {
  row: RunSummary;
  current: boolean;
  go: (to: Route | string) => void;
  onChanged: () => Promise<void>;
}) {
  const [menu, setMenu] = useState<"closed" | "open" | "confirm">("closed");
  const [renaming, setRenaming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const when = stamp(row.run_id);
  const label = row.name || when;

  // A click anywhere else, or Escape, puts the menu away.
  useEffect(() => {
    if (menu === "closed") return;
    const away = (event: MouseEvent) => {
      if (!box.current?.contains(event.target as Node)) setMenu("closed");
    };
    const escape = (event: KeyboardEvent) => event.key === "Escape" && setMenu("closed");
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", escape);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", escape);
    };
  }, [menu]);

  async function rename(name: string) {
    setRenaming(false);
    if (name.trim() === (row.name ?? "")) return;
    setBusy(true);
    setError(null);
    try {
      await api.renameRun(row.run_id, name);
      await onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove() {
    setBusy(true);
    setError(null);
    try {
      await api.deleteRun(row.run_id);
      setMenu("closed");
      if (current) go({ page: "shortlist" });
      await onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setMenu("closed");
    } finally {
      setBusy(false);
    }
  }

  if (renaming) {
    return (
      <div className="nav-sub-row" ref={box}>
        <input
          className="nav-rename"
          autoFocus
          defaultValue={row.name ?? ""}
          placeholder={when}
          maxLength={60}
          aria-label={`Name for the run of ${when}`}
          onFocus={(event) => event.currentTarget.select()}
          onBlur={(event) => void rename(event.currentTarget.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") event.currentTarget.blur();
            if (event.key === "Escape") setRenaming(false);
          }}
        />
      </div>
    );
  }

  return (
    <div className="nav-sub-row" ref={box} data-menu={menu !== "closed" || undefined}>
      <a
        className="nav-subitem"
        href={href({ page: "shortlist", runId: row.run_id })}
        aria-current={current ? "page" : undefined}
        aria-busy={busy || undefined}
        title={row.name ? `${row.name} · ${when}` : when}
        onClick={(event) => followLink(event, () => go({ page: "shortlist", runId: row.run_id }))}
      >
        <span className="label">{label}</span>
        <span className="count">{row.resumable ? "paused" : row.shortlisted}</span>
      </a>
      <button
        type="button"
        className="nav-more"
        aria-label={`Actions for ${label}`}
        aria-haspopup="menu"
        aria-expanded={menu !== "closed"}
        onClick={() => setMenu((was) => (was === "closed" ? "open" : "closed"))}
      >
        <MoreHorizontal aria-hidden="true" />
      </button>

      {menu === "open" && (
        <div className="nav-menu" role="menu">
          <button
            type="button"
            role="menuitem"
            onClick={() => {
              setMenu("closed");
              setRenaming(true);
            }}
          >
            Rename
          </button>
          <button type="button" role="menuitem" className="danger" onClick={() => setMenu("confirm")}>
            Delete
          </button>
        </div>
      )}
      {menu === "confirm" && (
        <div className="nav-menu confirm" role="alertdialog" aria-label={`Delete ${label}?`}>
          <p>
            Delete the run <b>{label}</b>? Its shortlist and run details go. Jobs, CVs and applications
            stay.
          </p>
          <div className="acts">
            <button type="button" className="btn ghost sm" onClick={() => setMenu("closed")} autoFocus>
              Cancel
            </button>
            <button type="button" className="btn danger sm" onClick={() => void remove()} disabled={busy}>
              Delete
            </button>
          </div>
        </div>
      )}
      {error && (
        <div className="nav-error" role="alert">
          {error}
        </div>
      )}
    </div>
  );
}
