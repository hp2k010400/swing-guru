// "Save this analysis": Sign in with Apple offered AFTER the first score (the free analysis no longer needs
// an account first). Linking keeps the same account, so this swing and the free-analysis count stay put.
import * as AppleAuthentication from 'expo-apple-authentication';
import { useEffect, useRef, useState } from 'react';
import { Alert, Text, View } from 'react-native';

import { track } from '@/lib/analytics';
import { friendly } from '@/lib/api';
import { shouldOfferSave } from '@/lib/first-result';
import { appleSignInAvailable, signInWithApple, switchToAppleAccount, useAccount } from '@/lib/identity';
import { outcomeCopy } from '@/lib/identity-core';
import { colors, theme, type } from '@/theme';

import { Caption, Card } from './ui';

export function SaveSwingCard({ fresh, example }: { fresh: boolean; example: boolean }) {
  const account = useAccount();
  const [appleReady, setAppleReady] = useState(false);
  const [saved, setSaved] = useState(false);
  const busy = useRef(false);
  const viewed = useRef(false);
  useEffect(() => {
    let alive = true;
    appleSignInAvailable().then((ok) => alive && setAppleReady(ok)).catch(() => undefined);
    return () => { alive = false; };
  }, []);
  const show = !saved && shouldOfferSave({ fresh, anonymous: account.anonymous && account.userId !== null, appleAvailable: appleReady, example });
  useEffect(() => {
    if (show && !viewed.current) {
      viewed.current = true;
      track('save_prompt_viewed', {});
    }
  }, [show]);
  if (!show) return null;

  async function save() {
    if (busy.current) return;
    busy.current = true;
    try {
      const { outcome, pending } = await signInWithApple();
      if (outcome === 'linked' || outcome === 'signed_in') {
        track('signup_completed', { method: 'apple', from: 'result' });
        setSaved(true);
        return;
      }
      const copy = outcomeCopy(outcome);
      if (outcome === 'existing_account' && pending && copy) {
        Alert.alert(copy.title, copy.body, [
          { text: 'Cancel', style: 'cancel' },
          {
            text: 'Switch account',
            onPress: async () => {
              try {
                const after = outcomeCopy(await switchToAppleAccount(pending));
                if (after) Alert.alert(after.title, after.body);
              } catch (error) {
                Alert.alert("Couldn't switch account", friendly(error));
              }
            },
          },
        ]);
        return;
      }
      if (copy) Alert.alert(copy.title, copy.body);
    } catch (error) {
      Alert.alert("Couldn't sign in with Apple", friendly(error));
    } finally {
      busy.current = false;
    }
  }

  return (
    <Card style={{ marginTop: 16 }}>
      <Caption>Save this analysis</Caption>
      <Text style={[type.callout, { color: colors.ink, marginTop: 8 }]}>
        Sign in with Apple so this swing and your progress stay with you if you change phone or reinstall.
      </Text>
      <View style={{ marginTop: 12 }}>
        <AppleAuthentication.AppleAuthenticationButton
          buttonType={AppleAuthentication.AppleAuthenticationButtonType.CONTINUE}
          buttonStyle={theme().scheme === 'dark' ? AppleAuthentication.AppleAuthenticationButtonStyle.WHITE : AppleAuthentication.AppleAuthenticationButtonStyle.BLACK}
          cornerRadius={12}
          style={{ height: 46 }}
          onPress={() => void save()}
        />
      </View>
    </Card>
  );
}
