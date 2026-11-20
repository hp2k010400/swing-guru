// Placeholder route for the centre tab; the tab button opens the Analyze sheet instead.
import { Redirect } from 'expo-router';

export default function NewSwing() {
  return <Redirect href="/analyze" />;
}
