import { useState } from "react";
import { api } from "./api";

/**
 * Records that an application actually went out.
 *
 * Cutting a CV is not applying, so a tailored job keeps showing up on the
 * shortlist until this is pressed. That makes this the one control that
 * removes a job for good - which is exactly why it toggles instead of
 * committing: a mis-click costs one more click, not a job you meant to send.
 *
 * A marked row stays put until the view is next rebuilt. Vanishing under the
 * cursor would put the undo out of reach the moment it is most wanted.
 */
export function MarkApplied({
  jobId,
  applied,
  onChange,
}: {
  jobId: number;
  applied: boolean;
  onChange: (applied: boolean) => void;
}) {
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function toggle() {
    setSaving(true);
    try {
      const body = await api.setApplied(jobId, !applied);
      onChange(body.applied);
      setError(null);
    } catch (err) {
      setError(String(err));
    } finally {
      setSaving(false);
    }
  }

  return (
    <button
      type="button"
      className={applied ? "btn sm applied" : "btn sm ghost"}
      onClick={toggle}
      disabled={saving}
      title={
        error ??
        (applied
          ? "Applied. Click to put it back on the shortlist."
          : "Record that you sent this application")
      }
    >
      {saving ? "…" : applied ? "Applied \u2713" : "Mark applied"}
    </button>
  );
}
