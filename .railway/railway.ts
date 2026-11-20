// Production API service (replaces railway.json, which Railway stops reading on 2026-12-01).
// Applied with `railway config apply` (deploys do not read this file). Variables are managed in Railway.
// Staging lives in its own partial: .railway/staging.ts (apply with --file .railway/staging.ts).
import { defineRailway, project, service, volume } from "railway/iac";

export const partial = "swing-guru-api";

export default defineRailway(() => {
  const swingGuruApiVolume = volume("swing-guru-api-volume", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "sfo", sizeMB: 5000 });
  const swingGuruApi = service("swing-guru-api", {
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    healthcheck: "/ready",
    healthcheckTimeout: 300,
    deploy: { restartPolicyType: "ON_FAILURE", restartPolicyMaxRetries: 5 },   // as railway.json had
    replicas: { "sfo": 1 },
    volumeMounts: { "/data": swingGuruApiVolume },
  });

  return project("swing-guru", {
    variables: { managed: false },
    resources: [swingGuruApi, swingGuruApiVolume],
  });
});
