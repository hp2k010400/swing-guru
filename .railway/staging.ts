// Staging service. Applied 2026-11-16 (`railway config apply --file .railway/staging.ts`).
import { defineRailway, project, service, volume } from "railway/iac";

export const partial = "swing-guru-staging";

export default defineRailway(() => {
  const swingGuruStagingVolume = volume("swing-guru-staging-volume", { alerts: { usage: { "100": {}, "80": {}, "95": {} } }, allowOnlineResize: true, region: "sfo", sizeMB: 5000 });
  const swingGuruStaging = service("swing-guru-staging", {
    build: { buildEnvironment: "V3", builder: "DOCKERFILE", dockerfilePath: "Dockerfile" },
    healthcheck: "/ready",
    healthcheckTimeout: 300,
    deploy: { restartPolicyType: "ON_FAILURE", restartPolicyMaxRetries: 5 },   // as railway.json had
    replicas: { "sfo": 1 },
    volumeMounts: { "/data": swingGuruStagingVolume },
  });

  return project("swing-guru", {
    variables: { managed: false },
    resources: [swingGuruStaging, swingGuruStagingVolume],
  });
});
