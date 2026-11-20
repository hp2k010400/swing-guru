// Build variants. app.json is the release app, unchanged. APP_VARIANT=staging (eas.json "staging"
// profile only) builds a separate test app - its own name, bundle id and icon - so it installs next to
// the TestFlight/App Store app and can never replace it or reach production.
module.exports = ({ config }) => {
  // Never ship a build without the real Meta client token (the SDK cannot send events without it).
  const fb = (config.plugins || []).find((p) => Array.isArray(p) && p[0] === 'react-native-fbsdk-next');
  if (process.env.EAS_BUILD && (!fb || !fb[1].clientToken || fb[1].clientToken.startsWith('REPLACE_'))) {
    throw new Error('Meta client token missing in app.json (react-native-fbsdk-next plugin).');
  }
  if (process.env.APP_VARIANT !== 'staging') return config;
  return {
    ...config,
    name: 'SG Staging',
    scheme: 'swingguru-staging',
    icon: './assets/images/icon-staging.png',
    ios: {
      ...config.ios,
      bundleIdentifier: 'com.swingguru.app.staging',
      icon: './assets/images/icon-staging.png',
      // Staging uses the server's development sign-in, not Sign in with Apple.
      usesAppleSignIn: false,
    },
    android: { ...config.android, package: 'com.swingguru.app.staging' },
  };
};
