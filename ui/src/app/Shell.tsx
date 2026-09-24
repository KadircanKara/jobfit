import { useEffect, useState, type ReactNode } from "react";
import {
  Activity as ActivityIcon,
  Briefcase,
  ChevronRight,
  FileText,
  LayoutTemplate,
  ListChecks,
  Menu,
  Moon,
  Play,
  SlidersHorizontal,
  Sparkles,
  Sun,
  X,
} from "lucide-react";
import { followLink, href, type Page, type Route } from "./router";
import { RunHistory } from "./RunHistory";
import { PHASE_LABEL, runStatus, useHunt } from "./store";

type Nav = { page: Page; label: string; icon: typeof Play; to: Route };

const GROUPS: { label: string; items: Nav[] }[] = [
  {
    label: "Hunt",
    items: [
      { page: "run", label: "Run", icon: Play, to: { page: "run" } },
      { page: "shortlist", label: "Shortlist", icon: ListChecks, to: { page: "shortlist" } },
      { page: "tailoring", label: "Tailoring", icon: Sparkles, to: { page: "tailoring" } },
    ],
  },
  {
    label: "Search setup",
    items: [
      { page: "boards", label: "Job boards", icon: SlidersHorizontal, to: { page: "boards" } },
      { page: "upwork", label: "Upwork", icon: Briefcase, to: { page: "upwork" } },
    ],
  },
  {
    label: "CV",
    items: [
      { page: "cv", label: "Profile", icon: FileText, to: { page: "cv" } },
      { page: "templates", label: "Templates", icon: LayoutTemplate, to: { page: "templates" } },
    ],
  },
];

export function Shell({
  route,
  go,
  children,
}: {
  route: Route;
  go: (to: Route | string) => void;
  children: ReactNode;
}) {
  const hunt = useHunt();
  const [mode, setMode] = useState(() => stored("jh-mode") ?? preferredMode());
  const [navOpen, setNavOpen] = useState(false);
  // The run list opens by itself on the pages it belongs to; a click on the
  // chevron overrides that until the next page change.
  const [historyPick, setHistoryPick] = useState<boolean | null>(null);
  const historyOpen = historyPick ?? (route.page === "shortlist" || route.page === "run");
  useEffect(() => setHistoryPick(null), [route.page]);

  useEffect(() => {
    document.documentElement.dataset.mode = mode;
    try {
      localStorage.setItem("jh-mode", mode);
    } catch {
      /* private browsing; the choice just will not stick */
    }
  }, [mode]);

  useEffect(() => setNavOpen(false), [route]);

  // The tab title says when work is running, so it shows even from another tab.
  const busy = hunt.run.running || hunt.tailor.running;
  useEffect(() => {
    const page = TITLES[route.page];
    document.title = busy ? `● ${page} · Jobhunt` : `${page} · Jobhunt`;
  }, [busy, route.page]);

  const kept = hunt.run.results.filter((row) => !row.below_bar).length;
  const counts: Partial<Record<Page, { value: string; tone?: string }>> = {
    shortlist: kept ? { value: String(kept) } : undefined,
    tailoring: hunt.tailor.running
      ? { value: String(hunt.tailor.jobs.filter((job) => job.state === "running").length), tone: "accent" }
      : hunt.tailor.jobs.length
        ? { value: String(hunt.tailor.jobs.length) }
        : undefined,
    boards: hunt.filtersDirty.boards ? { value: "", tone: "warn" } : undefined,
    upwork: hunt.filtersDirty.upwork ? { value: "", tone: "warn" } : undefined,
  };

  return (
    <div className="app" data-nav={navOpen ? "open" : undefined}>
      <aside className="sidebar" aria-label="Sections">
        <div className="sidebar-top">
          <a className="brand" href="/" onClick={(event) => followLink(event, () => go({ page: "run" }))}>
            <span className="brand-mark" aria-hidden="true">
              <ActivityIcon />
            </span>
            Jobhunt
          </a>
          <button
            type="button"
            className="btn quiet sm icon-only mobile-only"
            aria-label="Close navigation"
            onClick={() => setNavOpen(false)}
          >
            <X />
          </button>
        </div>

        <nav className="nav">
          {GROUPS.map((group) => (
            <div key={group.label}>
              <div className="nav-group">{group.label}</div>
              {group.items.map((item) => {
                const Icon = item.icon;
                const count = counts[item.page];
                const link = (
                  <a
                    key={item.page}
                    className="nav-item"
                    href={href(item.to)}
                    aria-current={route.page === item.page && !isPast(route) ? "page" : undefined}
                    onClick={(event) => followLink(event, () => go(item.to))}
                  >
                    <Icon className="icon" />
                    {item.label}
                    {count && (
                      <span
                        className="count"
                        data-tone={count.tone}
                        title={count.tone === "warn" ? "unsaved changes" : count.tone ? "running now" : undefined}
                      >
                        {count.tone && <span className="dot" data-tone={count.tone} data-live={count.tone === "accent" || undefined} />}
                        {count.value}
                        {count.tone === "warn" && <span className="sr-only">unsaved changes</span>}
                      </span>
                    )}
                  </a>
                );
                if (item.page !== "shortlist" || !hunt.past.length) return link;
                return (
                  <div key={item.page}>
                    <div className="nav-row">
                      {link}
                      <button
                        type="button"
                        className="nav-toggle"
                        aria-label={historyOpen ? "Hide past runs" : "Show past runs"}
                        aria-expanded={historyOpen}
                        onClick={() => setHistoryPick(!historyOpen)}
                      >
                        <ChevronRight aria-hidden="true" />
                      </button>
                    </div>
                    {historyOpen && <RunHistory route={route} go={go} />}
                  </div>
                );
              })}
            </div>
          ))}
        </nav>

        <Activity go={go} />

        <div className="sidebar-foot">
          <div className="seg" role="group" aria-label="Colour mode">
            <button type="button" aria-pressed={mode === "light"} onClick={() => setMode("light")}>
              <Sun />
              Light
            </button>
            <button type="button" aria-pressed={mode === "dark"} onClick={() => setMode("dark")}>
              <Moon />
              Dark
            </button>
          </div>
        </div>
      </aside>
      <div className="nav-scrim" onClick={() => setNavOpen(false)} />

      <div className="main">
        <div className="mobilebar">
          <button
            type="button"
            className="btn quiet sm icon-only"
            aria-label="Open navigation"
            onClick={() => setNavOpen(true)}
          >
            <Menu />
          </button>
          <span className="brand-mark" aria-hidden="true">
            <ActivityIcon />
          </span>
          <b>{TITLES[route.page]}</b>
          {busy && (
            <span className="badge live" data-tone="accent">
              <span className="dot" data-live="true" />
              {hunt.run.running ? "Run in progress" : "Tailoring"}
            </span>
          )}
        </div>
        {children}
      </div>
    </div>
  );
}

/** A past run is highlighted in the run list, not on the Run or Shortlist row. */
function isPast(route: Route) {
  return (route.page === "shortlist" || route.page === "run") && Boolean(route.runId);
}

const TITLES: Record<Page, string> = {
  run: "Run",
  shortlist: "Shortlist",
  tailoring: "Tailoring",
  boards: "Job boards",
  upwork: "Upwork",
  cv: "Profile",
  templates: "Templates",
};

/** Every piece of work in flight, from any page, each linking to where it lives. */
function Activity({ go }: { go: (to: Route | string) => void }) {
  const { run, tailor, desks, outreachStates } = useHunt();
  const items: ReactNode[] = [];

  if (run.running || run.phase === "paused") {
    const phase = PHASE_LABEL[run.phase] ?? run.phase;
    const scored = run.rank && run.phase === "rank" && run.rank.corpus
      ? Math.round(((run.rank.scored + run.rank.skipped) / run.rank.corpus) * 100)
      : null;
    items.push(
      <ActivityItem
        key="run"
        to={{ page: "run" }}
        go={go}
        tone={run.phase === "paused" ? "warn" : "accent"}
        live={run.running}
        title={`Run · ${runStatus(run)}`}
        sub={run.phase === "paused" ? "Resume on the Run page" : phase}
        progress={scored}
      />,
    );
  }

  if (tailor.running) {
    const done = tailor.jobs.filter((job) => job.state === "approved" || job.state === "failed").length;
    const approved = tailor.jobs.filter((job) => job.state === "approved").length;
    items.push(
      <ActivityItem
        key="tailor"
        to={{ page: "tailoring" }}
        go={go}
        tone="accent"
        live
        title="Tailoring"
        sub={`${done} of ${tailor.jobs.length} done · ${approved} approved`}
        progress={tailor.jobs.length ? Math.round((done / tailor.jobs.length) * 100) : null}
      />,
    );
  }

  if (desks.import.state === "running") {
    items.push(
      <ActivityItem key="import" to={{ page: "cv" }} go={go} tone="accent" live title="CV import" sub="Reading master.tex" />,
    );
  }
  if (desks.upload.state === "running") {
    items.push(
      <ActivityItem
        key="upload"
        to={{ page: "templates" }}
        go={go}
        tone="accent"
        live
        title="Template upload"
        sub={`Checking ${desks.upload.label}`}
      />,
    );
  }

  const queued = Object.values(outreachStates).filter((state) => state === "queued").length;
  if (queued) {
    items.push(
      <ActivityItem
        key="outreach"
        to={{ page: "shortlist" }}
        go={go}
        tone="warn"
        title="Outreach"
        sub={`${queued} DM${queued > 1 ? "s" : ""} waiting for an accepted invite`}
      />,
    );
  }

  return (
    <section className="activity" aria-label="Activity">
      <div className="activity-head">
        <span>Activity</span>
        {items.length > 0 && <span className="num">{items.length}</span>}
      </div>
      {items.length ? items : <div className="act-idle">Nothing running.</div>}
    </section>
  );
}

function ActivityItem({
  to,
  go,
  tone,
  live,
  title,
  sub,
  progress,
}: {
  to: Route;
  go: (to: Route | string) => void;
  tone: string;
  live?: boolean;
  title: string;
  sub: string;
  progress?: number | null;
}) {
  return (
    <a className="act" href={href(to)} onClick={(event) => followLink(event, () => go(to))}>
      <span className="act-top" data-tone={tone}>
        <span className="dot" data-live={live ? "true" : undefined} />
        {title}
      </span>
      <div className="act-sub">{sub}</div>
      {progress != null && (
        <div className="progress" aria-hidden="true">
          <i style={{ width: `${progress}%` }} />
        </div>
      )}
    </a>
  );
}

function preferredMode() {
  return window.matchMedia?.("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function stored(key: string) {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}
