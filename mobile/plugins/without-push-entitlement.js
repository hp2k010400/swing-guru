// Practice reminders are LOCAL notifications scheduled on the phone, so the app needs no remote push.
// expo-notifications adds the `aps-environment` (Push Notifications) entitlement automatically; it would
// require the Push capability on the App ID and new provisioning, which breaks unattended EAS signing.
// This strips it again. Remove this plugin only if we ever add server-sent push.
const { withEntitlementsPlist } = require('expo/config-plugins');

module.exports = function withoutPushEntitlement(config) {
  return withEntitlementsPlist(config, (cfg) => {
    delete cfg.modResults['aps-environment'];
    return cfg;
  });
};
