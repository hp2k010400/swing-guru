// Processing: uploading, then the server's real stages (no invented percentage). Survives leaving
// the screen, backgrounding and app restarts: the job id is persisted and the server keeps working.
// Feedback is restrained: one soft cue per newly observed stage completion (never for stages that
// were already done when the screen opened, never a catch-up burst after the app returns), one
// success cue when the job is really complete, then the finished swing is preloaded before the
// Summary opens (never blocking it).
import { File } from 'expo-file-system';
import { Image } from 'expo-image';
import { router, useFocusEffect, useLocalSearchParams } from 'expo-router';
import { SymbolView } from 'expo-symbols';
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react';
import Animated, { cancelAnimation, Easing, useAnimatedStyle, useReducedMotion, useSharedValue, withRepeat, withTiming } from 'react-native-reanimated';
import { AppState, StyleSheet, Text, View } from 'react-native';

import { Button, Card, Caption, Screen, StageList } from '@/components/ui';
import { api, ApiError, cachedSwing } from '@/lib/api';
import { pending, resume, submit } from '@/lib/analysis';
import { scoreBand, track, trackOnce } from '@/lib/analytics';
import { filmingTip, type FilmingTip } from '@/lib/filming-tips';
import { FilmingIllustration } from '@/components/filming-illustration';
import { SIGN_IN_REQUIRED_COPY } from '@/lib/billing-core';
import { haptic } from '@/lib/haptics';
import { createProgressTracker } from '@/lib/processing-state';
import type { Job } from '@/lib/types';
import { colors, type, themed } from '@/theme';

const TIPS = [
  'Film from behind your hands, looking along your target line.',
  'Keep your head and feet in the frame for the whole swing.',
  '60 fps keeps the club sharper through the fastest part of the swing.',
  'Upload the original video. Chat apps shrink it and blur the club.',
  'A propped-up phone gives the steadiest analysis.',
];

export default function Processing() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const local = useSyncExternalStore(pending.subscribe, () => JSON.stringify(pending.get(id)));
  const upload = local && local !== 'null' ? (JSON.parse(local) as ReturnType<typeof pending.get>) : null;
  const [job, setJob] = useState<Job | null>(null);
  const [offline, setOffline] = useState(false);
  const [tip, setTip] = useState(0);
  const started = useRef(0);
  const [elapsed, setElapsed] = useState(0);
  const [tracker] = useState(createProgressTracker);

  // Coming back from the paywall: if a plan was the only thing missing, start the same job now.
  useFocusEffect(
    useCallback(() => {
      const phase = pending.get(id)?.phase;
      if (phase === 'purchase_required' || phase === 'sign_in_required') void resume(id);
    }, [id]),
  );

  useEffect(() => {
    let alive = true;
    let timer: ReturnType<typeof setTimeout>;
    started.current = Date.now();
    let finishing = false;
    const poll = async () => {
      try {
        const next = await api.job(id);
        if (!alive) return;
        setOffline(false);
        setJob(next);
        finishing = next.stages.some((stage) => stage.id === 'build' && stage.state === 'active');
        const cue = tracker.observe({
          jobId: id, status: next.status, stages: next.stages,
          uploadDone: next.status !== 'awaiting_upload' || pending.get(id)?.phase === 'uploaded',
        }, AppState.currentState === 'active');
        if (cue === 'stage') haptic('stage');
        if (cue === 'ready') haptic('success', id);
        if (next.status === 'awaiting_upload' && pending.get(id)?.phase === 'uploaded') await resume(id);
        if (next.status === 'complete') {
          pending.remove(id);
          await preload(id);
          const detail = cachedSwing(id);
          trackOnce('swing_analysis_completed', id, { score_band: scoreBand(detail?.score.total), checks_measured: detail?.score.measured ?? null });
          if (!alive) return;
          router.replace({ pathname: '/swing/[id]', params: { id, fresh: '1' } });
          return;
        }
        if (next.status === 'failed') {
          pending.remove(id);
          trackOnce('swing_analysis_failed', id, { error_code: next.error?.code ?? 'failed' });
          return;
        }
      } catch (error) {
        if (!alive) return;
        if (error instanceof ApiError && error.status === 404) {
          pending.remove(id);
          setJob(null);
          return;
        }
        setOffline(error instanceof ApiError && error.code === 'offline');
      }
      // Poll faster only once the last real stage is running, so Ready is seen sooner.
      timer = setTimeout(poll, finishing ? 1000 : 2000);
    };
    void resume(id).finally(poll);
    const sub = AppState.addEventListener('change', (state) => {
      if (state === 'active') {
        clearTimeout(timer);
        tracker.quietNext(); // what finished while away is shown, not re-announced
        void poll();
      }
    });
    const clock = setInterval(() => setElapsed(Math.round((Date.now() - started.current) / 1000)), 1000);
    const tips = setInterval(() => setTip((t) => (t + 1) % TIPS.length), 6000);
    return () => {
      alive = false;
      clearTimeout(timer);
      clearInterval(clock);
      clearInterval(tips);
      sub.remove();
    };
  }, [id, tracker]);

  if (upload?.phase === 'upload_failed') {
    return (
      <Failure
        title="Your video didn't finish uploading"
        advice="Check your connection and try again."
        retry={async () => {
          const file = new File(upload.fileUri);
          if (!file.exists) return router.replace('/analyze');
          pending.remove(id);
          const next = await submit({ uri: upload.fileUri, fileName: file.name, mimeType: file.name.toLowerCase().endsWith('.mov') ? 'video/quicktime' : 'video/mp4', size: file.size ?? 0, durationMs: null });
          router.replace({ pathname: '/processing/[id]', params: { id: next } });
        }}
      />
    );
  }
  if (job?.status === 'failed' && job.error) {
    const tip = filmingTip(job.error.code);
    if (tip) return <FilmingFailure tip={tip} code={job.error.code} />;
    return <Failure title={job.error.title} advice={job.error.advice} />;
  }
  if (upload?.phase === 'purchase_required') {
    return (
      <Screen edges={['top', 'bottom']}>
        <View style={styles.hero}>
          <View style={[styles.orb, { backgroundColor: colors.greenSoft }]}>
            <SymbolView name="lock.open" size={40} tintColor={colors.green} />
          </View>
          <Text style={[type.title1, { color: colors.ink, marginTop: 22, textAlign: 'center' }]}>You&apos;ve reached this month&apos;s free limit</Text>
          <Text style={[type.callout, { color: colors.ink2, marginTop: 8, textAlign: 'center' }]}>
            This month&apos;s free allowance is used up. Your video is uploaded and safe: upgrade to Swing Guru Pro and this analysis starts by itself.
          </Text>
        </View>
        <View style={{ gap: 12, marginTop: 28 }}>
          <Button label="See plans" icon="sparkles" block onPress={() => router.push({ pathname: '/paywall', params: { from: 'limit' } })} />
          <Button label="Back to Home" kind="quiet" block onPress={() => router.replace('/')} />
        </View>
      </Screen>
    );
  }
  if (upload?.phase === 'sign_in_required') {
    // The bytes are uploaded and safe; what is missing is an account, so this is never a paywall.
    return (
      <Screen edges={['top', 'bottom']}>
        <View style={styles.hero}>
          <View style={[styles.orb, { backgroundColor: colors.surface2 }]}>
            <SymbolView name="person.crop.circle" size={40} tintColor={colors.ink2} />
          </View>
          <Text style={[type.title1, { color: colors.ink, marginTop: 22, textAlign: 'center' }]}>Sign-in needed</Text>
          <Text style={[type.callout, { color: colors.ink2, marginTop: 8, textAlign: 'center' }]}>
            {SIGN_IN_REQUIRED_COPY}
          </Text>
        </View>
        <View style={{ gap: 12, marginTop: 28 }}>
          <Button label="Sign in with Apple" icon="apple.logo" block onPress={() => router.push('/profile')} />
          <Button label="Back to Home" kind="quiet" block onPress={() => router.replace('/')} />
        </View>
      </Screen>
    );
  }

  const uploading = !job || job.status === 'awaiting_upload' ? (upload?.phase === 'uploaded' ? 'done' : 'active') : 'done';
  const stages = job?.stages ?? [];
  return (
    <Screen edges={['top', 'bottom']}>
      <View style={styles.hero}>
        <Breathing>
          <SymbolView name="figure.golf" size={40} tintColor={colors.green} />
        </Breathing>
        <Text style={[type.title1, { color: colors.ink, marginTop: 22, textAlign: 'center' }]}>Analysing your swing</Text>
        <Text style={[type.callout, { color: colors.ink2, marginTop: 8, textAlign: 'center' }]}>
          {elapsed > 150 ? 'Longer clips take a little more time. Hang tight.' : 'This usually takes a minute or two.'}
        </Text>
      </View>
      <StageList stages={stages} uploading={uploading} />
      <Text style={[type.footnote, { color: colors.ink3, textAlign: 'center', marginTop: 8, minHeight: 18 }]}>
        {offline ? "You're offline. We'll keep going when you're back." : elapsed >= 5 ? `Still working · ${elapsed}s` : ''}
      </Text>
      {job?.notice ? <Text style={[type.footnote, { color: colors.ink2, textAlign: 'center' }]}>{job.notice}</Text> : null}
      <Card tone="flat" style={{ marginTop: 20 }}>
        <Caption>Tip</Caption>
        <Text style={[type.callout, { color: colors.ink, marginTop: 6 }]}>{TIPS[tip]}</Text>
      </Card>
      <Text style={[type.footnote, { color: colors.ink3, textAlign: 'center', marginTop: 16 }]}>
        You can leave this screen. We&apos;ll keep analysing and your swing will be in your Library.
      </Text>
      <Button label="Back to Home" kind="quiet" block onPress={() => router.replace('/')} />
    </Screen>
  );
}

/** Warm the finished swing (Summary detail, thumbnail) for at most 1.5 s; never blocks the Summary. */
function preload(id: string): Promise<void> {
  const work = api.swing(id)
    .then((detail) => (detail.thumb_url ? Image.prefetch(detail.thumb_url).then(() => undefined) : undefined))
    .catch(() => undefined);
  return Promise.race([work, new Promise<void>((done) => setTimeout(done, 1500))]);
}

/** A calm, repeating sign that work is ongoing (not progress). Still under Reduce Motion. */
function Breathing({ children }: { children: React.ReactNode }) {
  const reduced = useReducedMotion();
  const pulse = useSharedValue(0);
  useEffect(() => {
    if (reduced) return;
    pulse.value = withRepeat(withTiming(1, { duration: 1600, easing: Easing.inOut(Easing.quad) }), -1, true);
    return () => cancelAnimation(pulse);
  }, [reduced, pulse]);
  const ring = useAnimatedStyle(() => ({ opacity: 0.35 - pulse.value * 0.3, transform: [{ scale: 1 + pulse.value * 0.18 }] }));
  return (
    <View style={styles.orbWrap}>
      <Animated.View style={[styles.halo, ring]} />
      <View style={styles.orb}>{children}</View>
    </View>
  );
}

/** A filming problem (not a fault): the matching picture and tip, then straight back to record/choose. */
function FilmingFailure({ tip, code }: { tip: FilmingTip; code: string }) {
  const again = () => {
    track('retry_after_failure', { error_code: code });
    router.replace('/analyze');
  };
  return (
    <Screen edges={['top', 'bottom']}>
      <View style={{ paddingTop: 24 }}>
        <Text style={[type.title1, { color: colors.ink, textAlign: 'center' }]}>{tip.title}</Text>
        <Text style={[type.callout, { color: colors.ink2, marginTop: 8, textAlign: 'center' }]}>{tip.body}</Text>
        <View style={{ marginTop: 20 }}>
          <FilmingIllustration highlight={tip.highlight} />
        </View>
        <Text style={[type.footnote, { color: colors.ink3, marginTop: 8, textAlign: 'center' }]}>
          This didn&apos;t use up your free analysis.
        </Text>
      </View>
      <View style={{ gap: 12, marginTop: 24 }}>
        <Button label="Try again" icon="video.fill" block onPress={again} />
        <Button label="Back to Home" kind="quiet" block onPress={() => router.replace('/')} />
      </View>
    </Screen>
  );
}

function Failure({ title, advice, retry }: { title: string; advice: string; retry?: () => void }) {
  return (
    <Screen edges={['top', 'bottom']}>
      <View style={styles.hero}>
        <View style={[styles.orb, { backgroundColor: colors.attentionSoft }]}>
          <SymbolView name="exclamationmark.circle" size={40} tintColor={colors.attentionInk} />
        </View>
        <Text style={[type.title1, { color: colors.ink, marginTop: 22, textAlign: 'center' }]}>{title}</Text>
        <Text style={[type.callout, { color: colors.ink2, marginTop: 8, textAlign: 'center' }]}>{advice}</Text>
      </View>
      <View style={{ gap: 12, marginTop: 28 }}>
        {retry ? <Button label="Try again" icon="arrow.clockwise" block onPress={retry} /> : null}
        <Button label="Try another video" icon="square.and.arrow.up" kind={retry ? 'secondary' : 'primary'} block onPress={() => router.replace('/analyze')} />
        <Button label="Back to Home" kind="quiet" block onPress={() => router.replace('/')} />
      </View>
    </Screen>
  );
}

const styles = themed(() => StyleSheet.create({
  hero: { alignItems: 'center', paddingTop: 48 },
  orbWrap: { width: 120, height: 120, alignItems: 'center', justifyContent: 'center' },
  halo: { position: 'absolute', width: 112, height: 112, borderRadius: 56, backgroundColor: colors.greenSoft },
  orb: { width: 96, height: 96, borderRadius: 48, backgroundColor: colors.greenSoft, alignItems: 'center', justifyContent: 'center' },
}));
