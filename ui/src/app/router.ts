import { useCallback, useEffect, useState, type MouseEvent } from "react";

/* Every page has its own address, so a view can be linked, bookmarked, reopened
   and walked back through with the browser's own buttons. A handful of routes
   with two parameters did not justify a router dependency. */

export type Route =
  | { page: "run"; runId?: string }
  | { page: "shortlist"; runId?: string; jobId?: number }
  | { page: "tailoring"; jobId?: number }
  | { page: "boards" }
  | { page: "upwork" }
  | { page: "cv" }
  | { page: "templates" };

export type Page = Route["page"];

/** Paths from before the redesign, kept working for bookmarks. */
const MOVED: Record<string, string> = {
  "/filters": "/search/boards",
  "/search": "/search/boards",
  "/profile": "/cv",
  "/templates": "/cv/templates",
};

export function parse(pathname: string, search: string): Route {
  const path = pathname.replace(/\/+$/, "") || "/";
  const query = new URLSearchParams(search);
  const run = /^\/runs\/([\w-]+)$/.exec(path);
  if (run) return { page: "run", runId: run[1] };
  if (path === "/shortlist") {
    return { page: "shortlist", runId: query.get("run") || undefined, jobId: numberOr(query.get("job")) };
  }
  const studio = /^\/tailoring\/(\d+)$/.exec(path);
  if (studio) return { page: "tailoring", jobId: Number(studio[1]) };
  if (path === "/tailoring") return { page: "tailoring" };
  if (path === "/search" || path === "/search/boards") return { page: "boards" };
  if (path === "/search/upwork") return { page: "upwork" };
  if (path === "/cv") return { page: "cv" };
  if (path === "/cv/templates") return { page: "templates" };
  return { page: "run" };
}

export function href(route: Route): string {
  switch (route.page) {
    case "run":
      return route.runId ? `/runs/${route.runId}` : "/";
    case "shortlist": {
      const query = new URLSearchParams();
      if (route.runId) query.set("run", route.runId);
      if (route.jobId != null) query.set("job", String(route.jobId));
      const text = query.toString();
      return text ? `/shortlist?${text}` : "/shortlist";
    }
    case "tailoring":
      return route.jobId != null ? `/tailoring/${route.jobId}` : "/tailoring";
    case "boards":
      return "/search/boards";
    case "upwork":
      return "/search/upwork";
    case "cv":
      return "/cv";
    case "templates":
      return "/cv/templates";
  }
}

type Guard = () => boolean;
let guard: Guard | null = null;

/** A page with unsaved work registers here; a move away asks first. */
export function setLeaveGuard(next: Guard | null) {
  guard = next;
}

export function useRoute(): [Route, (to: Route | string, options?: { replace?: boolean }) => void] {
  const [route, setRoute] = useState<Route>(() => {
    const moved = MOVED[window.location.pathname];
    if (moved) window.history.replaceState(null, "", moved + window.location.search);
    return parse(window.location.pathname, window.location.search);
  });

  useEffect(() => {
    const onPop = () => setRoute(parse(window.location.pathname, window.location.search));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const navigate = useCallback((to: Route | string, options?: { replace?: boolean }) => {
    const target = typeof to === "string" ? to : href(to);
    const here = window.location.pathname + window.location.search;
    if (target === here) return;
    const samePage =
      parse(target.split("?")[0], target.includes("?") ? target.slice(target.indexOf("?")) : "").page ===
      parse(window.location.pathname, window.location.search).page;
    if (!samePage && guard && !guard()) return;
    if (options?.replace) window.history.replaceState(null, "", target);
    else window.history.pushState(null, "", target);
    const [path, query = ""] = target.split("?");
    setRoute(parse(path, query ? `?${query}` : ""));
    if (!samePage) window.scrollTo({ top: 0 });
  }, []);

  return [route, navigate];
}

/** A plain left click on an in-app link is handled here; anything else (a new
 *  tab, a copied address) falls through to the browser. */
export function followLink(event: MouseEvent, go: () => void) {
  if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
  event.preventDefault();
  go();
}

function numberOr(value: string | null): number | undefined {
  if (value == null || value === "") return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}
