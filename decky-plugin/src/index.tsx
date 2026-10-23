import {
  ButtonItem,
  ConfirmModal,
  Field,
  Menu,
  MenuItem,
  PanelSection,
  PanelSectionRow,
  ProgressBarWithInfo,
  TextField,
  showContextMenu,
  showModal,
  staticClasses,
} from "@decky/ui";
import { addEventListener, callable, definePlugin, removeEventListener, toaster } from "@decky/api";
import { useEffect, useRef, useState } from "react";
import { FaSyncAlt } from "react-icons/fa";

type Job = { state: "running" | "done" | "error"; label: string; progress: number; message: string };
type Asset = { name: string; size: number };
type App = {
  id: string;
  name: string;
  display_repo: string;
  installed: { tag: string; verified?: boolean; path: string; kind: string } | null;
  has_launcher?: boolean;
  launcher?: string;
  latest: { tag: string; asset: Asset | null } | null;
  error: string;
  has_update: boolean;
  has_backup: boolean;
  exists: boolean;
  skipped: boolean;
  in_steam: boolean;
};
// An AppImage in ~/Applications that DeckPort could keep up to date (src is "" when unrecognized).
type Found = { path: string; file: string; src: string; name: string; app_id?: string };
type State = { apps: App[]; jobs: Record<string, Job>; version: string; found: Found[] };

// One backend entry point; main.py forwards to DeckPort's Server.handle (an allowlisted subset).
const call = callable<[path: string, body: Record<string, unknown>], any>("call");

const fail = (e: unknown) =>
  toaster.toast({ title: "DeckPort", body: e instanceof Error ? e.message : String(e) });

function statusOf(a: App, job?: Job): string {
  if (job?.state === "running") return job.label + "…";
  if (a.error) return "Error";
  if (!a.installed || !a.exists) return "Not installed";
  if (a.has_update) return "Update available";
  if (a.skipped) return "Skipped " + (a.latest?.tag ?? "");
  return a.latest?.tag ? "Up to date" : "…";
}

function describe(a: App): string {
  if (a.error) return a.error;
  const inst = a.installed && a.exists ? a.installed.tag : "not installed";
  const latest = a.latest?.tag ?? "?";
  const check = a.installed && a.exists ? (a.installed.verified ? " · ✓ SHA-256" : " · not verified") : "";
  return inst === latest ? inst + check : `${inst} → ${latest}${check}`;
}

// Steam is running in Game Mode, so the shortcut goes through its own API instead of shortcuts.vdf.
async function addToSteam(app: App) {
  const folder = app.installed!.kind === "archive";
  const exe = folder ? app.launcher! : app.installed!.path;
  const dir = folder ? app.installed!.path : exe.slice(0, exe.lastIndexOf("/"));
  try {
    const id = await SteamClient.Apps.AddShortcut(app.name, `"${exe}"`, `"${dir}"`, "");
    SteamClient.Apps.SetShortcutName(id, app.name);
    SteamClient.Apps.SetShortcutExe(id, `"${exe}"`);
    SteamClient.Apps.SetShortcutStartDir(id, `"${dir}"`);
    toaster.toast({ title: "DeckPort", body: `${app.name} added to your library` });
  } catch (e) {
    fail(e);
  }
}

function AppRows({ app, job, act }: { app: App; job?: Job; act: (path: string, body: object, ok?: string) => void }) {
  const running = job?.state === "running";
  const installed = !!app.installed && app.exists;

  const openMenu = (e: CustomEvent | MouseEvent) =>
    showContextMenu(
      <Menu label={app.name}>
        <MenuItem onSelected={() => act("/api/check", { id: app.id })}>Check for updates</MenuItem>
        {app.latest?.asset && (!installed || app.has_update) && (
          <MenuItem onSelected={() => act("/api/update", { id: app.id })}>
            {installed ? `Update to ${app.latest.tag}` : "Install"}
          </MenuItem>
        )}
        {app.has_update && installed && (
          <MenuItem onSelected={() => act("/api/skip", { id: app.id }, `Skipped ${app.latest?.tag}`)}>
            Skip this version
          </MenuItem>
        )}
        {app.skipped && (
          <MenuItem onSelected={() => act("/api/skip", { id: app.id, undo: true })}>Undo skip</MenuItem>
        )}
        {installed && (app.installed!.kind !== "archive" || app.has_launcher) && !app.in_steam && (
          <MenuItem onSelected={() => addToSteam(app)}>Add to Steam library</MenuItem>
        )}
        {app.has_backup && (
          <MenuItem onSelected={() => act("/api/rollback", { id: app.id }, `${app.name} rolled back`)}>
            Roll back to previous version
          </MenuItem>
        )}
        <MenuItem
          tone="destructive"
          onSelected={() =>
            showModal(
              <ConfirmModal
                strTitle={`Remove ${app.name}?`}
                strDescription="DeckPort stops tracking it. Downloaded files stay in ~/Applications."
                strOKButtonText="Remove"
                onOK={() => act("/api/remove", { id: app.id }, `${app.name} removed`)}
              />,
            )
          }
        >
          Remove
        </MenuItem>
      </Menu>,
      e.currentTarget ?? window,
    );

  return (
    <>
      <PanelSectionRow>
        <Field
          label={app.name}
          description={describe(app)}
          focusable
          highlightOnFocus
          onActivate={openMenu}
          onClick={openMenu}
          bottomSeparator={running || (app.has_update && !running) ? "none" : "standard"}
        >
          <span style={{ fontSize: 12, opacity: 0.8 }}>{statusOf(app, job)}</span>
        </Field>
      </PanelSectionRow>
      {running && (
        <PanelSectionRow>
          <ProgressBarWithInfo
            indeterminate={job!.label !== "Downloading" || !job!.progress}
            nProgress={Math.round(job!.progress * 100)}
            sOperationText={job!.message || job!.label}
          />
        </PanelSectionRow>
      )}
      {app.has_update && !running && app.latest?.asset && (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => act("/api/update", { id: app.id })}>
            {installed ? `Update to ${app.latest.tag}` : "Install"}
          </ButtonItem>
        </PanelSectionRow>
      )}
    </>
  );
}

function Content() {
  const [state, setState] = useState<State | null>(null);
  const [url, setUrl] = useState("");
  const [adding, setAdding] = useState(false);
  const prevJobs = useRef<Record<string, Job>>({});
  const timer = useRef<number | undefined>(undefined);

  const refresh = async () => {
    window.clearTimeout(timer.current);
    try {
      const s: State = await call("/api/state", {});
      // Toast when a job finishes, since the panel may be closed by then.
      for (const [id, prev] of Object.entries(prevJobs.current)) {
        const now = s.jobs[id];
        if (prev.state === "running" && now && now.state !== "running" && prev.label !== "Checking") {
          const app = s.apps.find((a) => a.id === id);
          toaster.toast({
            title: "DeckPort",
            body: now.state === "error" ? `${app?.name}: ${now.message}` : `${app?.name} updated to ${app?.installed?.tag}`,
          });
        }
      }
      prevJobs.current = s.jobs;
      setState(s);
      const busy = Object.values(s.jobs).some((j) => j.state === "running");
      timer.current = window.setTimeout(refresh, busy ? 1000 : 10000);
    } catch (e) {
      fail(e);
    }
  };

  useEffect(() => {
    refresh();
    return () => window.clearTimeout(timer.current);
  }, []);

  const act = async (path: string, body: object, ok?: string) => {
    try {
      await call(path, body as Record<string, unknown>);
      if (ok) toaster.toast({ title: "DeckPort", body: ok });
    } catch (e) {
      fail(e);
    }
    refresh();
  };

  const add = async () => {
    if (!url.trim()) return;
    setAdding(true);
    try {
      await call("/api/add", { repo: url.trim() });
      setUrl("");
      toaster.toast({ title: "DeckPort", body: "Added, checking for releases…" });
    } catch (e) {
      fail(e);
    }
    setAdding(false);
    refresh();
  };

  if (!state) {
    return (
      <PanelSection>
        <PanelSectionRow>Loading…</PanelSectionRow>
      </PanelSection>
    );
  }

  const updatable = state.apps.filter((a) => a.has_update && state.jobs[a.id]?.state !== "running");
  const found = (state.found ?? []).filter((f) => f.src);

  return (
    <>
      <PanelSection>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={!updatable.length} onClick={() => act("/api/update", {})}>
            {updatable.length ? `Update all (${updatable.length})` : "Everything is up to date"}
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => act("/api/check", {})}>
            Check for updates
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      {found.length > 0 && (
        <PanelSection title="Found in ~/Applications">
          <PanelSectionRow>
            <Field
              description={`${found.map((f) => f.name).join(", ")}. DeckPort can update these in place, so Steam shortcuts keep working.`}
              bottomSeparator="none"
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              onClick={() =>
                act(
                  "/api/track_found",
                  { items: found.map((f) => ({ path: f.path, src: f.src, name: f.name, app_id: f.app_id ?? "" })) },
                  `Tracking ${found.length} app(s)`,
                )
              }
            >
              {`Keep ${found.length} up to date`}
            </ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}

      <PanelSection title="Apps">
        {state.apps.length === 0 && (
          <PanelSectionRow>
            <Field description="No apps yet. Add a repo link below, or use DeckPort in Desktop Mode." />
          </PanelSectionRow>
        )}
        {state.apps.map((a) => (
          <AppRows key={a.id} app={a} job={state.jobs[a.id]} act={act} />
        ))}
      </PanelSection>

      <PanelSection title="Add app">
        <PanelSectionRow>
          <TextField
            label="Repo link"
            description="GitHub, GitLab, Codeberg, Gitea or Forgejo. Press STEAM + X for the keyboard."
            value={url}
            onChange={(e) => setUrl(e.target.value)}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" disabled={adding || !url.trim()} onClick={add}>
            {adding ? "Adding…" : "Add"}
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field
            description={`DeckPort ${state.version}. Asset patterns, settings and GitHub token: open DeckPort in Desktop Mode.`}
            bottomSeparator="none"
          />
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  const listener = addEventListener<[names: string[]]>("updates_available", (names) => {
    toaster.toast({
      title: "DeckPort: updates available",
      body: names.length <= 3 ? names.join(", ") : `${names.slice(0, 3).join(", ")} and ${names.length - 3} more`,
    });
  });

  return {
    name: "DeckPort",
    titleView: <div className={staticClasses.Title}>DeckPort</div>,
    content: <Content />,
    icon: <FaSyncAlt />,
    onDismount() {
      removeEventListener("updates_available", listener);
    },
  };
});
