// "How to film" guidance: the onboarding screen and the matching tip after a filming-related failure.
// Pure data, so the copy is testable and shared by both places.

export type FilmingIssue = 'no_setup' | 'no_swing' | 'framing' | 'camera_angle' | 'too_far';

/** What the illustration highlights. */
export type Highlight = 'record_early' | 'whole_swing' | 'whole_body' | 'angle' | 'closer';

export interface FilmingTip {
  title: string;
  body: string;
  highlight: Highlight;
}

export const FILMING_STEPS: { icon: string; text: string }[] = [
  { icon: 'arrow.up.and.down.and.arrow.left.and.right', text: 'Film down the line: stand behind your hands, looking along your target line.' },
  { icon: 'figure.golf', text: 'Get your whole body and the club in the picture, head to feet.' },
  { icon: 'iphone', text: 'Prop your phone at hip height, about 3 to 4 metres away.' },
  { icon: 'record.circle', text: 'Start recording before you set up, and stop after your finish.' },
];

export const FILMING_TIPS: Record<FilmingIssue, FilmingTip> = {
  no_setup: {
    title: 'Start recording a little earlier',
    body: 'We found your swing, but the video starts after you set up. Hit record before you step up to the ball, and keep it rolling until after your finish.',
    highlight: 'record_early',
  },
  no_swing: {
    title: "We couldn't spot a swing",
    body: 'Make sure the whole swing is in the video, from standing still at the ball to your finish. One swing per video works best.',
    highlight: 'whole_swing',
  },
  framing: {
    title: 'Get all of you in the picture',
    body: 'Part of you went out of the frame. Step back or tilt the phone so your head, feet and club stay in view the whole time.',
    highlight: 'whole_body',
  },
  camera_angle: {
    title: 'Try filming from behind',
    body: "This works best filmed down the line: stand the phone behind your hands, pointing along your target line, not side-on.",
    highlight: 'angle',
  },
  too_far: {
    title: 'Move the phone a bit closer',
    body: 'You look quite small in the video. Put the phone about 3 to 4 metres away so you fill most of the picture, head to feet.',
    highlight: 'closer',
  },
};

export function filmingTip(code: string | null | undefined): FilmingTip | null {
  return code && code in FILMING_TIPS ? FILMING_TIPS[code as FilmingIssue] : null;
}
