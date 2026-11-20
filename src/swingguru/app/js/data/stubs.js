// Everything the app shows that the engine does not produce yet lives here.
// Each value is visibly tagged "stub" in dev mode (?dev=1).

export const STUBS = {
  user: { name: "Harry Phillips", firstName: "Harry", memberSince: "2026-09-28" },
  plan: {
    tier: "free",               // "free" | "pro"
    monthlyFreeAnalyses: null,  // null = no limit enforced yet
    trialDays: 7,
  },
  preferences: { handedness: "auto", reminders: "weekly" },
  signedIn: false,
  appVersion: "0.1 (V1 preview)",
};
