import { useCallback, useEffect, useState } from "react";

type Job = { state: "idle" | "running" | "done" | "failed" };

/**
 * A server-side job the page polls while it runs: the import and a template
 * upload both call an agent that takes minutes, so neither holds a request open.
 * `version` moves on whenever the job settles, for anything cached against it
 * (an embedded PDF preview) to reload.
 */
export function usePolledJob<T extends Job>(fetchState: () => Promise<T>) {
  const [job, setJob] = useState<T | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [version, setVersion] = useState(0);

  useEffect(() => {
    fetchState()
      .then(setJob)
      .catch((error) => setProblem(String(error)));
  }, [fetchState]);

  useEffect(() => {
    if (job?.state !== "running") return;
    const timer = window.setInterval(async () => {
      const next = await fetchState();
      setJob(next);
      if (next.state !== "running") setVersion((current) => current + 1);
    }, 2000);
    return () => window.clearInterval(timer);
  }, [job?.state, fetchState]);

  const act = useCallback(async (call: () => Promise<void>) => {
    setProblem(null);
    try {
      await call();
    } catch (error) {
      setProblem(error instanceof Error ? error.message : String(error));
    }
  }, []);

  const settle = useCallback((next: T) => {
    setJob(next);
    setVersion((current) => current + 1);
  }, []);

  return { job, setJob, settle, problem, version, act };
}
