import "./theme.css";
import { Profile } from "./Profile";
import { StudioPage } from "./Revise";
import { TailoringPage } from "./Tailor";
import { Templates } from "./Templates";
import { useRoute } from "./app/router";
import { Shell } from "./app/Shell";
import { HuntProvider } from "./app/store";
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

  return (
    <Shell route={route} go={go}>
      {route.page === "run" && <RunPage runId={route.runId} go={go} />}
      {route.page === "shortlist" && <ShortlistPage runId={route.runId} jobId={route.jobId} go={go} />}
      {route.page === "tailoring" &&
        (route.jobId != null ? <StudioPage jobId={route.jobId} go={go} /> : <TailoringPage go={go} />)}
      {route.page === "cv" && <Profile onOpenTemplates={() => go({ page: "templates" })} />}
      {route.page === "templates" && <Templates />}
    </Shell>
  );
}
