import { useCallback, useEffect, useState } from "react";
import { RefreshCw, Square } from "lucide-react";
import { api, type BoardCheck, type BoardSummary } from "./api";

const POLL_MS = 2000;

function count(n: number): string {
  return n.toLocaleString("en-US");
}

function when(iso: string | null): string {
  if (!iso) return "never checked";
  const days = Math.floor((Date.now() - new Date(iso + "Z").getTime()) / 86_400_000);
  return days <= 0 ? "last checked today" : `last checked ${days} day${days > 1 ? "s" : ""} ago`;
}

/**
 * Runs fetch only the boards that have ever posted a matching title. Every
 * other board is fetched here, when asked: a board that turns out to carry a
 * matching job joins the runs from then on.
 */
export function BoardCheckPanel({ runRunning }: { runRunning: boolean }) {
  const [summary, setSummary] = useState<BoardSummary | null>(null);
  const [check, setCheck] = useState<BoardCheck | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(() => {
    api
      .boards()
      .then((body) => {
        setSummary(body);
        setCheck(body.check);
      })
      .catch(() => setError("Could not load the board counts."));
  }, []);

  useEffect(load, [load]);

  const running = check?.running ?? false;
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => {
      api
        .boardCheck()
        .then((next) => {
          setCheck(next);
          if (!next.running) load();
        })
        .catch(() => undefined);
    }, POLL_MS);
    return () => window.clearInterval(timer);
  }, [running, load]);

  const start = async () => {
    setError(null);
    const body = await api.startBoardCheck().catch(() => ({ started: false, message: "Could not start the check." }));
    if (!body.started) {
      setError(body.message ?? "Could not start the check.");
      return;
    }
    setCheck(await api.boardCheck());
  };

  const sources = Object.entries(check?.sources ?? {});
  const done = sources.reduce((sum, [, s]) => sum + s.done, 0);
  const total = sources.reduce((sum, [, s]) => sum + s.total, 0);

  return (
    <section className="panel boards" aria-labelledby="boards-title">
      <div className="boards-row">
        <div className="boards-text">
          <h2 id="boards-title">Job boards</h2>
          <p className="boards-line">
            {summary ? (
              <>
                Runs fetch {count(summary.relevant)} boards that have posted matching titles ·{" "}
                {count(summary.other)} other boards skipped · {when(summary.last_checked_at)}
              </>
            ) : (
              "Counting boards"
            )}
          </p>
        </div>
        {running ? (
          <button className="btn stop" onClick={() => void api.stopBoardCheck()}>
            <Square aria-hidden="true" />
            Stop check
          </button>
        ) : (
          <button
            className="btn secondary"
            onClick={() => void start()}
            disabled={runRunning || !summary || summary.other === 0}
            title={
              runRunning
                ? "Wait for the run to finish"
                : "Fetch every skipped board once; any with a matching job joins the runs"
            }
          >
            <RefreshCw aria-hidden="true" />
            Check other boards
          </button>
        )}
      </div>

      {summary?.includes_dead && !running && summary.other > 0 && (
        <p className="hint">The first check also retries boards marked dead, once.</p>
      )}

      {running && (
        <div className="boards-progress" aria-live="polite">
          <p className="boards-line tabular">
            {count(done)} of {count(total)} boards fetched
          </p>
          <ul className="boards-sources">
            {sources.map(([name, s]) => (
              <li key={name} className="tabular">
                <span>{name}</span>
                <span>
                  {s.status === "storing" ? "storing" : s.status === "done" ? "done" : `${count(s.done)} / ${count(s.total)}`}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {!running && check?.finished_at && (
        <p className="boards-line">
          {check.error
            ? `The last check stopped with an error: ${check.error}`
            : `Last check found ${count(check.became_relevant)} board${check.became_relevant === 1 ? "" : "s"} with matching jobs; runs now fetch them.`}
        </p>
      )}
      {error && <div className="err">{error}</div>}
    </section>
  );
}
