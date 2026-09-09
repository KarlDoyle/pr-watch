/**
 * PR Watch Forge backend: two functions wired in manifest.yml.
 *
 *   refreshLandscape (scheduledTrigger, hourly)
 *     Egress-fetches the JSON landscape snapshot rollup.py can write
 *     (`PR_WATCH_LANDSCAPE_JSON_OUT` — see ../../rollup.py) from wherever
 *     Karl publishes it, and caches it in Forge storage. The fetch target
 *     host MUST be declared under `permissions.external.fetch.backend` in
 *     manifest.yml or Forge will block the request.
 *
 *   handler (dashboardGadget resolver)
 *     Serves the cached snapshot to the gadget's frontend. Never calls out
 *     directly from the gadget render path — Forge dashboard gadgets fetch
 *     through a resolver, not client-side, by design.
 *
 * Forge apps cannot reach arbitrary/local URLs — the digest source must be
 * a real HTTPS endpoint Atlassian's infrastructure can resolve. A GitHub
 * Pages / static host / small endpoint on the same box running the GitHub
 * App server (`github_app/server.py`) all work; localhost does not.
 */

import Resolver from "@forge/resolver";
import { storage } from "@forge/api";

const STORAGE_KEY = "pr-watch:landscape:latest";
const LANDSCAPE_URL = process.env.PR_WATCH_LANDSCAPE_URL; // set via `forge variables set`

const resolver = new Resolver();

resolver.define("handler", async () => {
  const cached = await storage.get(STORAGE_KEY);
  if (!cached) {
    return {
      status: "no_data",
      message:
        "No landscape data cached yet. Waiting for the next hourly refresh, " +
        "or PR_WATCH_LANDSCAPE_URL is not configured.",
    };
  }
  return { status: "ok", ...cached };
});

export async function refreshLandscape() {
  if (!LANDSCAPE_URL) {
    console.warn("PR_WATCH_LANDSCAPE_URL is not set — skipping refresh.");
    return;
  }
  const res = await fetch(LANDSCAPE_URL);
  if (!res.ok) {
    console.error(`Landscape fetch failed: ${res.status} ${res.statusText}`);
    return;
  }
  const data = await res.json();
  await storage.set(STORAGE_KEY, { fetchedAt: new Date().toISOString(), data });
}

export const handler = resolver.getDefinitions();
