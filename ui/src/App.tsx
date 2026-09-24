import "./theme.css";
import { FiltersPanel } from "./Filters";
import { Profile } from "./Profile";
import { StudioPage } from "./Revise";
import { TailoringPage } from "./Tailor";
import { Templates } from "./Templates";
import { useRoute } from "./app/router";
import { Shell } from "./app/Shell";
import { HuntProvider, useHunt } from "./app/store";
import { RunPage } from "./pages/RunPage";
import { ShortlistPage } from "./pages/ShortlistPage";

export default function App() {
  return (
    <HuntProvider>
      <Routes />
    </HuntProvider>
  );
}

function Routes() {
  const [route, go] = useRoute();
  const hunt = useHunt();
  const filterPage = route.page === "boards" || route.page === "upwork" ? route.page : null;

  return (
    <Shell route={route} go={go}>
      {route.page === "run" && <RunPage runId={route.runId} go={go} />}
      {route.page === "shortlist" && <ShortlistPage runId={route.runId} jobId={route.jobId} go={go} />}
      {route.page === "tailoring" &&
        (route.jobId != null ? <StudioPage jobId={route.jobId} go={go} /> : <TailoringPage go={go} />)}
      {route.page === "cv" && <Profile onOpenTemplates={() => go({ page: "templates" })} />}
      {route.page === "templates" && <Templates />}

      {/* Kept mounted whichever page is up, so an unsaved draft survives a trip
          elsewhere and back, and its validity still gates Start run. */}
      {hunt.filters && hunt.vocab ? (
        <div className="page" style={{ display: filterPage ? undefined : "none" }}>
          <FiltersPanel
            filters={hunt.filters}
            vocab={hunt.vocab}
            onSaved={hunt.setFilters}
            onValidity={hunt.setValid}
            sources={hunt.sources}
            section={filterPage}
            onDirty={hunt.setFiltersDirty}
            onEnableUpwork={() => void hunt.pickSources([...hunt.sources, "upwork"])}
          />
        </div>
      ) : (
        filterPage && (
          <div className="page">
            <header className="page-header">
              <h1>{filterPage === "upwork" ? "Upwork" : "Job boards"}</h1>
            </header>
            {hunt.sourcesError ? (
              <div className="notice" data-tone="danger" role="alert">
                {hunt.sourcesError}
              </div>
            ) : (
              <div className="empty">
                <span className="spin" aria-hidden="true" /> Loading the filters
              </div>
            )}
          </div>
        )
      )}
    </Shell>
  );
}
