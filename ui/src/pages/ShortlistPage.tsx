import { useCallback, useState } from "react";
import { Sparkles } from "lucide-react";
import { OutreachDrawer } from "../Outreach";
import { Shortlist } from "../Run";
import { TemplateStep } from "../cv/TemplateStep";
import { api } from "../api";
import { followLink, href, type Route } from "../app/router";
import { Sheet } from "../app/Sheet";
import { stamp, useHunt, useShownRun } from "../app/store";

export function ShortlistPage({
  runId,
  jobId,
  go,
}: {
  runId?: string;
  jobId?: number;
  go: (to: Route | string, options?: { replace?: boolean }) => void;
}) {
  const hunt = useHunt();
  const { run } = useShownRun(runId);
  const [choosing, setChoosing] = useState(false);
  const picked = hunt.picked;
  const kept = run.results.filter((row) => !row.below_bar).length;
  const openRow = jobId != null ? run.results.find((row) => row.job_id === jobId) : undefined;

  const setPick = (id: number, on: boolean) => {
    const next = new Set(picked);
    if (on) next.add(id);
    else next.delete(id);
    hunt.setPicked(next);
    if (!next.size) setChoosing(false);
  };

  const startTailoring = useCallback(
    async (templates?: Record<number, string>) => {
      const ids = [...picked];
      if (!ids.length) return;
      const body = await api.startTailoring(ids, templates);
      // Refreshed either way: a 409 means another tab started a batch this one
      // has not seen, and showing it is the point of the refusal.
      await hunt.refreshTailor();
      if (body.started === false) throw new Error(body.message ?? "Tailoring did not start.");
      setChoosing(false);
      hunt.setPicked(new Set());
      go({ page: "tailoring" });
    },
    [picked, hunt, go],
  );

  const closeOutreach = useCallback(() => go({ page: "shortlist", runId }, { replace: true }), [go, runId]);

  return (
    <div className="page">
      <header className="page-header">
        <h1>Shortlist</h1>
        <span className="badge">
          {run.outcome === "stopped_early" ? `${kept} · stopped early` : `${kept} above the bar`}
        </span>
        <div className="page-actions">
          {hunt.past.length > 0 && (
            <div className="seg" role="group" aria-label="Which run">
              <a
                href={href({ page: "shortlist" })}
                aria-current={!runId ? "page" : undefined}
                onClick={(e) => followLink(e, () => go({ page: "shortlist" }))}
              >
                Live
              </a>
              {hunt.past.map((row) => (
                <a
                  key={row.run_id}
                  href={href({ page: "shortlist", runId: row.run_id })}
                  aria-current={runId === row.run_id ? "page" : undefined}
                  onClick={(e) => followLink(e, () => go({ page: "shortlist", runId: row.run_id }))}
                >
                  {stamp(row.run_id)}
                </a>
              ))}
            </div>
          )}
        </div>
      </header>

      <div className="panel" style={{ padding: 0 }}>
        <Shortlist
          rows={run.results}
          applied={hunt.applied}
          onApplied={hunt.setApplied}
          picked={picked}
          bar={run.gate?.plan.bar ?? null}
          onPick={setPick}
          onPickAll={(ids, on) => {
            const next = new Set(picked);
            for (const id of ids) {
              if (on) next.add(id);
              else next.delete(id);
            }
            hunt.setPicked(next);
          }}
          outreachStates={hunt.outreachStates}
          openJob={jobId}
          onOutreach={(id) => go(jobId === id ? { page: "shortlist", runId } : { page: "shortlist", runId, jobId: id })}
        />
      </div>

      {picked.size > 0 && (
        <div className="selbar" role="region" aria-label="Selection">
          <span className="count">
            <b>{picked.size}</b> selected
          </span>
          {picked.size > 5 && (
            <span className="warn">Each is a full tailoring run with up to three reviewer rounds.</span>
          )}
          {hunt.tailor.running && <span className="hint">A tailoring batch is already running.</span>}
          <span className="acts">
            <button className="btn quiet sm" onClick={() => hunt.setPicked(new Set())}>
              Clear
            </button>
            <button className="btn sm" onClick={() => setChoosing(true)} disabled={hunt.tailor.running}>
              <Sparkles aria-hidden="true" />
              Tailor selected
            </button>
          </span>
        </div>
      )}

      {choosing && picked.size > 0 && (
        <Sheet
          label="Choose a template for each CV"
          title={`Tailor ${picked.size} CV${picked.size > 1 ? "s" : ""}`}
          sub="Each CV is cut from your profile in the template you pick, then checked by the reviewer."
          onClose={() => setChoosing(false)}
        >
          <TemplateStep
            jobs={[...picked].map((id) => {
              const row = run.results.find((result) => result.job_id === id) ??
                hunt.run.results.find((result) => result.job_id === id);
              return { job_id: id, title: row?.title ?? "", company: row?.company ?? "" };
            })}
            onStart={startTailoring}
            onCancel={() => setChoosing(false)}
          />
        </Sheet>
      )}

      {jobId != null && (
        <Sheet
          label="Outreach"
          title={openRow ? openRow.title : `Job ${jobId}`}
          sub={openRow ? `${openRow.company} · outreach` : "Outreach"}
          onClose={closeOutreach}
        >
          <OutreachDrawer key={jobId} jobId={jobId} onBudget={hunt.refreshOutreach} />
        </Sheet>
      )}
    </div>
  );
}
