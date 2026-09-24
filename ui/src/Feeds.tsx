import { useEffect, useState } from "react";
import { api, type FeedsBody } from "./api";

type Props = {
  // Bumped by the Filters panel after a save, since the proposals are scored
  // against the current titles and a stale list would misrepresent them.
  reloadToken: number;
};

function key(provider: string, token: string) {
  return `${provider} ${token}`;
}

// The API's timestamps are naive UTC (models.utcnow() strips tzinfo), so
// `new Date(...)` would read them as local time and render hours off. Every
// other timestamp in the app formats the string itself; this matches.
function formatWhen(iso: string): string {
  return iso.replace("T", " ").slice(0, 19);
}

// Below this many postings a rate says more about the sample than about the
// category: 2 matches out of 2 reads as a sure thing next to 40 out of 400.
// The absolute matched count is shown regardless, and is what the ordering
// uses, so nothing is hidden by suppressing the percentage here.
const MIN_SAMPLE_FOR_RATE = 10;

function statusLabel(status: string): string {
  if (status === "dead") return "did not resolve";
  if (status === "empty") return "no jobs in 30 days";
  return status;
}

/**
 * "Where to look": lets the user turn a scored category proposal into a feed
 * that actually gets fetched. `matched` is the number that matters — it is
 * the count of stored postings whose title already matches the user's titles
 * — so it leads every row; sample size and rate are secondary, and never
 * change the order the API returned.
 */
export function FeedsPanel({ reloadToken }: Props) {
  const [data, setData] = useState<FeedsBody | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Keyed by key(provider, token) for React-key/lookup convenience, but the
  // value carries provider and token as their own fields — never decoded back
  // out of the composite string, so there is no split step that could ever
  // truncate a token containing a space (most category names do).
  const [picked, setPicked] = useState<Map<string, { provider: string; token: string }>>(
    new Map(),
  );
  const [saving, setSaving] = useState(false);
  // Tracks the one row a retire is in flight for, so only that row's control
  // disables — approving other proposals stays available meanwhile.
  const [retiring, setRetiring] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    api
      .readFeeds()
      .then((body) => {
        if (!alive) return;
        setData(body);
        setError(null);
        // The refetch is scored against whatever titles are now current, so a
        // tick made before this reload may point at a category that no longer
        // appears (or appears with different numbers). Either way, holding
        // onto it would let "Fetch selected" approve something the user never
        // saw on screen.
        setPicked(new Map());
        setRetiring(null);
      })
      .catch((err) => {
        if (alive) setError(String(err));
      });
    return () => {
      alive = false;
    };
  }, [reloadToken]);

  function toggle(provider: string, token: string) {
    const id = key(provider, token);
    setPicked((current) => {
      const next = new Map(current);
      if (next.has(id)) next.delete(id);
      else next.set(id, { provider, token });
      return next;
    });
  }

  async function approve() {
    if (!picked.size) return;
    setSaving(true);
    try {
      const approveList = [...picked.values()];
      const body = await api.saveFeeds({ approve: approveList });
      setData(body);
      setPicked(new Map());
      setError(null);
    } catch (err) {
      setError(String(err));
    } finally {
      setSaving(false);
    }
  }

  // Retiring marks the board dead and stops it being fetched; it does not
  // delete the row, and approving the same category again later re-enables it.
  async function retire(provider: string, token: string) {
    const id = key(provider, token);
    setRetiring(id);
    try {
      const body = await api.saveFeeds({ retire: [{ provider, token }] });
      setData(body);
      setError(null);
    } catch (err) {
      setError(String(err));
    } finally {
      setRetiring(null);
    }
  }

  const empty = !data?.proposals.length && !data?.approved.length;

  return (
    <section className="subsection">
      <div className="subsection-head">
        <h3>Where to look</h3>
        <span className="hint">Board categories, scored against your titles</span>
      </div>

      {error && <div className="err">{error}</div>}

      {!data && !error && (
        <div className="empty">
          <span className="spin" aria-hidden="true" /> Loading feeds
        </div>
      )}

      {data && empty && (
        <div className="empty">
          {data.has_titles
            ? "Run a sync first. These are counted from jobs already fetched."
            : "Add a job title first. Categories are scored by how many stored postings match your titles."}
        </div>
      )}

      {data && data.approved.length > 0 && (
        <div className="feedgroup">
          <div className="feedgroup-title">Approved</div>
          <div className="feedlist">
            {data.approved.map((row) => {
              const id = key(row.provider, row.token);
              return (
                <div className="approvedrow" key={id}>
                  <span className="nm">
                    {row.provider} · {row.token}
                  </span>
                  <span className="status">{statusLabel(row.status)}</span>
                  <span className="meta">
                    {row.last_job_count !== null
                      ? `${row.last_job_count.toLocaleString()} jobs`
                      : "not fetched yet"}
                    {row.last_fetched_at
                      ? ` · ${formatWhen(row.last_fetched_at)}`
                      : ""}
                  </span>
                  <button
                    className="btn ghost sm"
                    disabled={retiring === id}
                    onClick={() => retire(row.provider, row.token)}
                  >
                    {retiring === id ? "Stopping" : "Stop fetching"}
                  </button>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {data && data.proposals.length > 0 && (
        <div className="feedgroup">
          <div className="feedgroup-title">Proposals</div>
          <div className="feedlist">
            {data.proposals.map((row) => {
              const disabled = row.token === null;
              const id = disabled ? null : key(row.provider, row.token as string);
              return (
                <label
                  className={disabled ? "feedrow disabled" : "feedrow"}
                  key={`${row.provider}:${row.category}`}
                >
                  {disabled ? (
                    <span className="feedcheck-empty" aria-hidden="true" />
                  ) : (
                    <input
                      type="checkbox"
                      checked={picked.has(id as string)}
                      onChange={() => toggle(row.provider, row.token as string)}
                    />
                  )}
                  <span className="nm">
                    {row.provider} · {row.category}
                  </span>
                  <span className="matched">{row.matched.toLocaleString()} matched</span>
                  <span className="rate">
                    {disabled
                      ? "no feed for this category"
                      : row.sample < MIN_SAMPLE_FOR_RATE
                        ? "not enough data yet"
                        : `${Math.round((row.matched / row.sample) * 100)}% of ${row.sample.toLocaleString()}`}
                  </span>
                </label>
              );
            })}
          </div>
          <button className="btn ghost sm" style={{ marginTop: 10 }} disabled={!picked.size || saving} onClick={approve}>
            {saving ? "Saving" : "Fetch selected"}
          </button>
        </div>
      )}
    </section>
  );
}
