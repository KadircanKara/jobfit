import { useEffect, useState } from "react";
import { ExternalLink, Search, UserPlus } from "lucide-react";
import { api } from "./api";
import type { ContactCandidate, OutreachBudget, OutreachContact, OutreachRoute } from "./api";

/**
 * Outreach for one job, opened from its shortlist row.
 *
 * The route is decided, not offered: where LinkedIn leaves a free path that is
 * the path, and the three fallbacks appear only when it does not. Every send is
 * one press for one person - there is deliberately no way to approve in bulk.
 *
 * A draft survives a route change. Someone who has written three careful
 * sentences must not lose them for clicking a radio button, so only Redraft
 * replaces the text.
 */
const ROUTE_LABEL: Record<OutreachRoute, string> = {
  dm: "Regular DM",
  free_inmail: "Free InMail",
  invite_note: "Invite with the message in the note",
  invite_then_dm: "Invite bare, DM after they accept",
  paid_inmail: "Paid InMail",
};

/**
 * Why this route cannot go out right now, or null.
 *
 * Caps are a refusal, not a warning: at cap the button is disabled and says so,
 * rather than sending a press to the server to be turned down. The order matches
 * `caps.check` - a paid InMail is a message like any other, so the daily cap is
 * read before the credit stock.
 */
function capReason(route: OutreachRoute | null, budget: OutreachBudget | null): string | null {
  if (!route || !budget) return null;
  const isInvite = route === "invite_note" || route === "invite_then_dm";
  if (isInvite) {
    if (budget.invites_used >= budget.invites_max) {
      return `Daily invite cap reached (${budget.invites_max}). It resets at midnight UTC.`;
    }
    if (budget.invites_week_used >= budget.invites_week_max) {
      return `Weekly invite cap reached (${budget.invites_week_max}). LinkedIn restricts accounts over invite volume, so this one is a rolling week.`;
    }
    return null;
  }
  if (budget.dms_used >= budget.dms_max) {
    return `Daily message cap reached (${budget.dms_max}). It resets at midnight UTC.`;
  }
  if (route === "paid_inmail" && budget.credits <= 0) {
    return "No InMail credits left. Use an invite instead.";
  }
  return null;
}

const ROUTE_WHY: Record<string, string> = {
  dm: "You are connected, so the message goes straight to their inbox.",
  free_inmail: "Not connected, but their profile takes a free InMail - no credit is spent.",
};

export function OutreachDrawer({
  jobId,
  onBudget,
}: {
  jobId: number;
  onBudget?: (budget: OutreachBudget) => void;
}) {
  const [contacts, setContacts] = useState<OutreachContact[]>([]);
  const [budget, setBudget] = useState<OutreachBudget | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [candidates, setCandidates] = useState<ContactCandidate[]>([]);
  const [finding, setFinding] = useState(false);
  const [findError, setFindError] = useState<string | null>(null);
  const [findNote, setFindNote] = useState<string | null>(null);

  async function load() {
    try {
      const body = await api.outreach(jobId);
      setContacts(body.contacts);
      setBudget(body.budget);
      onBudget?.(body.budget);
      setError(null);
    } catch (err) {
      setError(String(err));
    }
  }

  /**
   * The stated poster, if the posting named one - a free DB read, no click
   * needed. Runs alongside `load()` on open rather than folded into it: this
   * one is allowed to fail quietly (an empty list is a normal answer, not an
   * error worth a banner), where `load` failing means the drawer has nothing.
   */
  async function loadStated() {
    try {
      const body = await api.statedContacts(jobId);
      setCandidates(body.candidates);
    } catch {
      // No stated poster is not an error; leave the candidate list empty.
    }
  }

  useEffect(() => {
    load();
    loadStated();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId]);

  /**
   * Runs one drawer action against busy/error state. Returns whether `work`
   * succeeded so a caller that needs to know - `addCandidate` below - can act
   * on it; every existing call site already ignores the return value, so this
   * is additive and does not change their behaviour.
   */
  async function act(work: () => Promise<unknown>): Promise<boolean> {
    setBusy(true);
    try {
      await work();
      await load();
      setError(null);
      return true;
    } catch (err) {
      setError(String(err));
      return false;
    } finally {
      setBusy(false);
    }
  }

  /**
   * A candidate's identity for de-duplication: its normalized profile URL, or
   * its name when it has none. `/find` returns the stated poster again on
   * every call - it does not know the drawer already loaded it on open - so
   * merging by this key is what keeps that one poster from ever appearing
   * twice, or from being replaced by an inferred copy of itself.
   */
  function candidateKey(candidate: ContactCandidate): string {
    return candidate.profile_url
      ? candidate.profile_url.replace(/^https?:\/\//, "").replace(/\/+$/, "").toLowerCase()
      : `name:${candidate.full_name.trim().toLowerCase()}`;
  }

  /**
   * Appends `fresh` below `prev` without disturbing `prev`'s order - the
   * stated poster loaded on open must stay first and stay put, with the
   * company search's results arriving after it, not reshuffling it in.
   */
  function mergeCandidates(prev: ContactCandidate[], fresh: ContactCandidate[]): ContactCandidate[] {
    const seen = new Set(prev.map(candidateKey));
    const additions = fresh.filter((c) => !seen.has(candidateKey(c)));
    return [...prev, ...additions];
  }

  /**
   * A 502 from `find` still carries the stated candidate in `detail.candidates`
   * - the inferred company search failing is not a reason to throw away a
   * perfectly good named contact. `api.findContacts` goes through `detailJson`,
   * which stringifies a non-string `detail` into the error message, so that
   * JSON is parsed back apart here rather than discarding the whole response.
   */
  async function find() {
    setFinding(true);
    try {
      const body = await api.findContacts(jobId);
      setCandidates((prev) => mergeCandidates(prev, body.candidates));
      setFindError(null);
      // Not an error: the search was skipped because there is no company to
      // search for. Saying so is the whole difference between a button that
      // looks broken and one that explains itself.
      setFindNote(body.note ?? null);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      let shown = message;
      try {
        const parsed = JSON.parse(message) as {
          detail?: { message?: string; candidates?: ContactCandidate[] };
        };
        if (parsed.detail && typeof parsed.detail === "object") {
          if (typeof parsed.detail.message === "string") shown = parsed.detail.message;
          if (Array.isArray(parsed.detail.candidates)) {
            setCandidates((prev) => mergeCandidates(prev, parsed.detail!.candidates!));
          }
        }
      } catch {
        // Not a JSON body - show it raw rather than swallow it.
      }
      setFindError(shown);
      setFindNote(null);
    } finally {
      setFinding(false);
    }
  }

  /**
   * `origin` and `headline` travel with the candidate: a person the posting
   * itself named is not the same as a name typed into the box below, and
   * dropping them here stored every found contact as "added by hand".
   *
   * Marked by index rather than by object identity - `act` reloads the drawer
   * in between, so the object in `prev` need not be the one clicked.
   */
  async function addCandidate(candidate: ContactCandidate, index: number) {
    const ok = await act(() =>
      api.addContact(jobId, {
        full_name: candidate.full_name,
        profile_url: candidate.profile_url,
        headline: candidate.headline,
        origin: candidate.origin,
      }),
    );
    if (ok) {
      setCandidates((prev) => prev.map((c, i) => (i === index ? { ...c, existing: true } : c)));
    }
  }

  return (
    <div className="drawerbox">
      <div className="drawerhead">
        <span>
          {contacts.length} contact{contacts.length === 1 ? "" : "s"}
        </span>
        {budget && (
          <span className="drawerbudget">
            {budget.invites_used}/{budget.invites_max} invites ·{" "}
            {budget.invites_week_used}/{budget.invites_week_max} this week ·{" "}
            {budget.dms_used}/{budget.dms_max} messages · {budget.credits} credits
          </span>
        )}
      </div>

      {error && (
        <div className="notice" data-tone="danger" role="alert">
          {error}
        </div>
      )}

      {contacts.length === 0 && (
        <div className="hint">No contacts yet. Find people at the company, or add one by hand below.</div>
      )}

      <div className="contacts">
        {contacts.map((contact) => (
          <ContactCard
            key={contact.contact_id}
            jobId={jobId}
            contact={contact}
            budget={budget}
            busy={busy}
            act={act}
          />
        ))}
      </div>

      <div className="findrow">
        <button type="button" className="btn ghost sm" disabled={finding} onClick={find}>
          {finding ? <span className="spin" aria-hidden="true" /> : <Search aria-hidden="true" />}
          {finding ? "Looking" : "Find more contacts"}
        </button>
        <span className="hint">
          Searches LinkedIn for who else works at the company. Uses your account.
        </span>
      </div>

      {findError && <div className="err">{findError}</div>}
      {findNote && <div className="hint">{findNote}</div>}

      {candidates.length > 0 && (
        <div className="candidates">
          {candidates.map((candidate, i) => (
            <div className="candidate" key={`${candidate.profile_url ?? candidate.full_name}-${i}`}>
              <div>
                <div className="name">{candidate.full_name}</div>
                {candidate.headline && <div className="headline">{candidate.headline}</div>}
                <div className="src">
                  <span className={candidate.origin === "job_poster" ? "chip stated" : "chip inferred"}>
                    {candidate.origin === "job_poster" ? "posted this job" : "works here"}
                  </span>
                  {candidate.profile_url && (
                    <a
                      className="extlink"
                      href={`https://${candidate.profile_url.replace(/^https?:\/\//, "")}`}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {candidate.profile_url.replace(/^https?:\/\/(www\.)?/, "")}
                      <ExternalLink aria-hidden="true" />
                    </a>
                  )}
                </div>
              </div>
              {candidate.existing ? (
                <span className="existingtag">already a contact</span>
              ) : (
                <button
                  type="button"
                  className="btn ghost sm"
                  disabled={busy}
                  onClick={() => addCandidate(candidate, i)}
                >
                  Add
                </button>
              )}
            </div>
          ))}
        </div>
      )}

      <div className="addrow">
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Name"
          aria-label="Contact name"
        />
        <input
          value={url}
          onChange={(e) => setUrl(e.target.value)}
          placeholder="linkedin.com/in/…"
          aria-label="LinkedIn profile URL"
        />
        <button
          type="button"
          className="btn ghost sm"
          disabled={busy || !name.trim()}
          onClick={() =>
            act(async () => {
              await api.addContact(jobId, { full_name: name.trim(), profile_url: url.trim() || null });
              setName("");
              setUrl("");
            })
          }
        >
          <UserPlus aria-hidden="true" />
          Add contact
        </button>
      </div>
      <div className="hint">Add someone by hand when the posting and the search did not name them.</div>
    </div>
  );
}

function ContactCard({
  jobId,
  contact,
  budget,
  busy,
  act,
}: {
  jobId: number;
  contact: OutreachContact;
  budget: OutreachBudget | null;
  busy: boolean;
  act: (work: () => Promise<unknown>) => Promise<boolean>;
}) {
  const [body, setBody] = useState(contact.body ?? "");
  useEffect(() => setBody(contact.body ?? ""), [contact.body]);

  const chosen = contact.route;
  const over = body.length > contact.limit;
  const capped = capReason(chosen, budget);

  if (contact.state === "queued") {
    return (
      <div className="contact" data-active="true">
        <Who contact={contact} />
        <div className="rail">
          <div className="statusline">
            <span className="pulse" />
            <span>
              <b>Invite sent, DM waiting.</b> The queue releases it once they accept.
            </span>
          </div>
          <div className="acts">
            <button
              type="button"
              className="btn ghost sm"
              disabled={busy}
              onClick={() => act(() => api.cancelOutreach(jobId, contact.contact_id))}
            >
              Cancel the queued DM
            </button>
          </div>
        </div>
      </div>
    );
  }

  if (contact.state === "failed") {
    return (
      <div className="contact" data-active="true">
        <Who contact={contact} />
        <div className="rail">
          <div className="statusline">
            <span className="dot" data-state="failed" />
            <span>
              <b>{chosen ? ROUTE_LABEL[chosen] : "Send"} failed.</b>{" "}
              {contact.failure || "LinkedIn refused it."}
            </span>
          </div>
          <div className="acts">
            <button
              type="button"
              className="btn ghost sm"
              disabled={busy}
              onClick={() => act(() => api.draftOutreach(jobId, contact.contact_id, chosen))}
            >
              Redraft
            </button>
          </div>
        </div>
      </div>
    );
  }

  if (contact.state === "cancelled") {
    return (
      <div className="contact" data-active="true">
        <Who contact={contact} />
        <div className="rail">
          <div className="statusline">
            <span className="dot" data-state="cancelled" />
            <span>
              <b>Cancelled.</b>{" "}
              {contact.body
                ? "The draft is still there - Redraft picks up where you left off."
                : "Nothing was sent."}
            </span>
          </div>
          <div className="acts">
            <button
              type="button"
              className="btn ghost sm"
              disabled={busy}
              onClick={() => act(() => api.draftOutreach(jobId, contact.contact_id, chosen))}
            >
              Redraft
            </button>
          </div>
        </div>
      </div>
    );
  }

  if (contact.state === "sent") {
    return (
      <div className="contact" data-active="true">
        <Who contact={contact} />
        <div className="rail">
          <div className="statusline">
            <span className="dot" data-state="sent" />
            <span>
              <b>{chosen ? ROUTE_LABEL[chosen] : "Message"} recorded.</b> Stub provider - nothing
              left this machine.
            </span>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="contact" data-active={contact.state !== "none"}>
      <Who contact={contact} />
      <div className="stubs">
        <button
          type="button"
          className="stub"
          aria-pressed={contact.is_connection === true}
          disabled={busy}
          onClick={() =>
            act(() =>
              api.setContactStatus(contact.contact_id, { is_connection: !contact.is_connection }),
            )
          }
        >
          <span className="mk" />
          Connection
        </button>
        <button
          type="button"
          className="stub"
          aria-pressed={contact.can_send_inmail === true}
          disabled={busy}
          onClick={() =>
            act(() =>
              api.setContactStatus(contact.contact_id, {
                can_send_inmail: !contact.can_send_inmail,
              }),
            )
          }
        >
          <span className="mk" />
          Premium
        </button>
      </div>

      <div className="rail">
        <div className={contact.needs_choice ? "route fallback" : "route"}>
          <span className="lbl">Route</span>
          <span className="pick">
            {contact.needs_choice ? "No free route" : chosen ? ROUTE_LABEL[chosen] : "—"}
          </span>
          <span className="why">
            {contact.needs_choice
              ? "Not connected, and their profile does not take a free InMail. Pick how to reach them."
              : (chosen && ROUTE_WHY[chosen]) || ""}
          </span>
        </div>

        {contact.needs_choice && (
          <div className="options">
            {contact.allowed_routes.map((route) => (
              <button
                key={route}
                type="button"
                className="opt"
                aria-pressed={chosen === route}
                disabled={busy}
                onClick={() =>
                  act(() =>
                    contact.body
                      ? api.saveOutreachBody(jobId, contact.contact_id, body, route)
                      : api.draftOutreach(jobId, contact.contact_id, route),
                  )
                }
              >
                <span className="mk" />
                <span className="ttl">{ROUTE_LABEL[route]}</span>
                <span className={route === "paid_inmail" ? "cost spend" : "cost"}>
                  {route === "paid_inmail" ? `1 credit · ${budget?.credits ?? 0} left` : "1 invite"}
                </span>
              </button>
            ))}
          </div>
        )}

        {contact.body != null && (
          <div className="draft">
            <textarea
              className="draftbox"
              value={body}
              onChange={(e) => setBody(e.target.value)}
              onBlur={() => act(() => api.saveOutreachBody(jobId, contact.contact_id, body))}
              aria-label={`Message to ${contact.full_name}`}
            />
            <div className="draftmeta">
              <span className={over ? "over" : undefined}>
                {body.length} / {contact.limit} characters
              </span>
              <span>mock draft · not generated</span>
            </div>
          </div>
        )}

        <div className="acts">
          <button
            type="button"
            className="btn ghost sm"
            disabled={busy || (!chosen && contact.needs_choice)}
            onClick={() => act(() => api.draftOutreach(jobId, contact.contact_id, chosen))}
          >
            {contact.body ? "Redraft" : "Draft"}
          </button>
          {contact.body != null && (
            <button
              type="button"
              className="btn sm"
              disabled={busy || over || !chosen || capped !== null}
              onClick={() => {
                const recipient = contact.profile_url
                  ? `${contact.full_name} (${contact.profile_url})`
                  : contact.full_name;
                if (!window.confirm(`Send to ${recipient}?`)) return;
                act(async () => {
                  await api.saveOutreachBody(jobId, contact.contact_id, body);
                  await api.approveOutreach(jobId, contact.contact_id, chosen);
                });
              }}
            >
              {chosen === "invite_then_dm" ? "Send invite, queue the DM" : "Approve and send"}
            </button>
          )}
          {contact.state === "drafted" && (
            <button
              type="button"
              className="btn ghost sm"
              disabled={busy}
              onClick={() => act(() => api.cancelOutreach(jobId, contact.contact_id))}
            >
              Cancel
            </button>
          )}
          <span className="spacer" />
          <span className="hint">
            {over
              ? "Too long for this route. Trim it, or redraft."
              : capped || "Approve-first. Changing route keeps what you wrote."}
          </span>
        </div>
      </div>
    </div>
  );
}

// How this person was found. Worth showing plainly: a contact the posting named
// carries more weight than one a company search turned up, and both more than a
// name typed in by hand.
const ORIGIN_LABEL: Record<string, string> = {
  job_poster: "job poster",
  company_search: "found at the company",
  manual: "added by hand",
};

function Who({ contact }: { contact: OutreachContact }) {
  return (
    <div>
      <div className="name">{contact.full_name}</div>
      {contact.headline && <div className="headline">{contact.headline}</div>}
      <div className="src">
        <span className="srctag">{ORIGIN_LABEL[contact.origin] ?? "added by hand"}</span>
        {contact.profile_url && (
          <a
            className="extlink"
            href={`https://${contact.profile_url.replace(/^https?:\/\//, "")}`}
            target="_blank"
            rel="noreferrer"
          >
            {contact.profile_url.replace(/^https?:\/\/(www\.)?/, "")}
            <ExternalLink aria-hidden="true" />
          </a>
        )}
      </div>
    </div>
  );
}
