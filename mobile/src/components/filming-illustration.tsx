// A simple drawn "how to film" picture (no stock pros): the phone's view of a golfer at address, filmed
// down the line, with the thing that matters for this tip highlighted in gold.
import Svg, { Circle, G, Line, Path, Rect, Text as SvgText } from 'react-native-svg';

import type { Highlight } from '@/lib/filming-tips';
import { colors } from '@/theme';

const W = 280;
const H = 220;

/** A right-handed golfer at address, filmed down the line (from behind the hands): body on the left,
 *  club and ball out to the right. Drawn facing left, then mirrored. Feet at y=0, ~100 px tall. */
export function Golfer({ x, y, scale = 1, tone }: { x: number; y: number; scale?: number; tone: string }) {
  return (
    <G transform={`translate(${x} ${y}) scale(${-scale} ${scale})`}>
      <Circle cx={-8} cy={-92} r={9} fill={tone} />
      {/* spine tilted over the ball, hips back */}
      <Line x1={-4} y1={-82} x2={10} y2={-48} stroke={tone} strokeWidth={6} strokeLinecap="round" />
      {/* legs, slight knee flex */}
      <Path d="M10 -48 L4 -24 L8 0" stroke={tone} strokeWidth={5} fill="none" strokeLinecap="round" strokeLinejoin="round" />
      <Path d="M10 -48 L16 -24 L14 0" stroke={tone} strokeWidth={5} fill="none" strokeLinecap="round" strokeLinejoin="round" />
      {/* arms hanging to the grip */}
      <Line x1={-2} y1={-76} x2={-14} y2={-46} stroke={tone} strokeWidth={4} strokeLinecap="round" />
      {/* club to the ball */}
      <Line x1={-14} y1={-46} x2={-44} y2={-2} stroke={colors.ink2} strokeWidth={2.5} strokeLinecap="round" />
      <Circle cx={-48} cy={-2} r={3} fill={colors.ink} />
    </G>
  );
}

export function FilmingIllustration({ highlight }: { highlight?: Highlight }) {
  const gold = colors.gold;
  const tone = colors.green;
  // The phone screen (portrait) on the left; a timeline underneath when timing is the point.
  const timeline = highlight === 'record_early' || highlight === 'whole_swing';
  const small = highlight === 'closer';
  const frame = { x: 70, y: 8, w: 140, h: timeline ? 160 : 200 };
  const feetY = frame.y + frame.h - 18;
  const scale = small ? 0.45 : timeline ? 1.15 : 1.35;
  const golferX = frame.x + (small ? 66 : 52);
  const ballX = golferX + 48 * scale;               // the ball sits out to the golfer's right
  return (
    <Svg width="100%" height={H} viewBox={`0 0 ${W} ${H}`} accessibilityLabel="How to film your swing">
      {/* phone screen */}
      <Rect x={frame.x} y={frame.y} width={frame.w} height={frame.h} rx={18}
        fill={colors.surface2} stroke={highlight === 'whole_body' ? gold : colors.lineStrong} strokeWidth={highlight === 'whole_body' ? 3 : 1.5} />
      {/* ground and the target line running away from the camera (down the line) */}
      <Line x1={frame.x + 10} y1={feetY + 2} x2={frame.x + frame.w - 10} y2={feetY + 2} stroke={colors.lineStrong} strokeWidth={1.5} />
      <Line x1={ballX} y1={feetY + 2} x2={ballX - 6} y2={frame.y + frame.h * 0.42}
        stroke={highlight === 'angle' ? gold : colors.ink3} strokeWidth={highlight === 'angle' ? 2.5 : 1.2} strokeDasharray="4 4" />
      {highlight === 'angle' ? (
        <SvgText x={ballX - 24} y={frame.y + frame.h * 0.42 - 6} fill={gold} fontSize={10} fontWeight="700">Target</SvgText>
      ) : null}
      <Golfer x={golferX} y={feetY} scale={scale} tone={tone} />
      {/* head-and-feet brackets: whole body in the picture */}
      {highlight === 'whole_body' ? (
        <G stroke={gold} strokeWidth={3} strokeLinecap="round">
          <Path d={`M${frame.x + 12} ${frame.y + 30} v-14 h14`} fill="none" />
          <Path d={`M${frame.x + frame.w - 12} ${frame.y + 30} v-14 h-14`} fill="none" />
          <Path d={`M${frame.x + 12} ${feetY - 4} v14 h14`} fill="none" />
          <Path d={`M${frame.x + frame.w - 12} ${feetY - 4} v14 h-14`} fill="none" />
        </G>
      ) : null}
      {/* distance hint: 3 to 4 metres */}
      {highlight === 'closer' || !highlight ? (
        <G>
          <Line x1={frame.x + frame.w + 10} y1={feetY} x2={frame.x + frame.w + 54} y2={feetY} stroke={highlight === 'closer' ? gold : colors.ink3} strokeWidth={2} />
          <SvgText x={frame.x + frame.w + 14} y={feetY - 8} fill={highlight === 'closer' ? gold : colors.ink2} fontSize={11} fontWeight="700">3–4 m</SvgText>
        </G>
      ) : null}
      {/* timeline: record before the set-up, stop after the finish */}
      {timeline ? (
        <G>
          <Rect x={30} y={190} width={220} height={6} rx={3} fill={colors.surface3} />
          <Rect x={highlight === 'record_early' ? 30 : 60} y={190} width={highlight === 'record_early' ? 220 : 160} height={6} rx={3} fill={gold} />
          <Circle cx={30} cy={193} r={7} fill="#e5484d" />
          {[['Set-up', 80], ['Swing', 150], ['Finish', 215]].map(([label, x]) => (
            <SvgText key={label as string} x={x as number} y={214} fill={colors.ink2} fontSize={10} textAnchor="middle">{label}</SvgText>
          ))}
          <SvgText x={30} y={182} fill={colors.ink2} fontSize={10} textAnchor="middle">Rec</SvgText>
        </G>
      ) : null}
    </Svg>
  );
}
