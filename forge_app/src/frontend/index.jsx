/**
 * PR Watch Forge dashboard gadget (UI Kit 2 / "native" render).
 *
 * Deliberately plain: aggregate counts, no per-PR list, no team named as
 * "underperforming" — the same awareness framing as rollup.py's Slack
 * digest and for the same reason (see rollup.py's module docstring):
 * team-level managers read a named, specific "you're slow" view as an
 * attack; the same data read as a landscape a director skims is useful.
 */
import React, { useEffect, useState } from "react";
import ForgeReconciler, {
  Text,
  Heading,
  Stack,
  SectionMessage,
  Box,
} from "@forge/react";
import { invoke } from "@forge/bridge";

const FRAMING_NOTE =
  "Awareness digest, not a performance ranking — flow varies with team " +
  "size, review load and time zone. Useful for spotting where support " +
  "might help, not for judging who's behind.";

function App() {
  const [snapshot, setSnapshot] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    invoke("handler")
      .then(setSnapshot)
      .catch((err) => setError(String(err)));
  }, []);

  if (error) {
    return (
      <SectionMessage appearance="error" title="Could not load landscape data">
        <Text>{error}</Text>
      </SectionMessage>
    );
  }

  if (!snapshot || snapshot.status === "no_data") {
    return (
      <SectionMessage appearance="information" title="No data yet">
        <Text>
          {snapshot?.message ??
            "Waiting for the first hourly refresh from PR Watch."}
        </Text>
      </SectionMessage>
    );
  }

  const { data, fetchedAt } = snapshot;

  return (
    <Stack space="space.200">
      <Heading size="small">Cross-Team Review Landscape</Heading>
      <Text>{FRAMING_NOTE}</Text>
      <Text>
        Teams observed: {data.teams_observed} · Teams with items past SLA:{" "}
        {data.teams_with_breaches}
      </Text>
      {data.teams.map((team) => (
        <Box key={team.label} padding="space.100">
          <Text>
            <Text weight="bold">{team.label}</Text> — {team.breached} past
            SLA, {team.approaching} approaching, {team.fresh} fresh
            {team.breached > 0 &&
              ` (oldest ${team.oldest_breach_hrs.toFixed(1)}h)`}
          </Text>
        </Box>
      ))}
      <Text size="small" color="color.text.subtle">
        Last refreshed: {fetchedAt}
      </Text>
    </Stack>
  );
}

ForgeReconciler.render(<App />);
