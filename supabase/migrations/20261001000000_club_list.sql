-- Swing Guru: the full club list, chosen before analysis (app_api.CLUBS).
-- "I'm not sure" is stored as null. The first picker's groups (Hybrid, Irons, Wedges)
-- stay valid so swings tagged with them keep loading.
alter table public.swings drop constraint if exists swings_club_check;
alter table public.swings add constraint swings_club_check check (club in (
  'Driver', '3 Wood', '5 Wood', '7 Wood',
  '2 Hybrid', '3 Hybrid', '4 Hybrid', '5 Hybrid',
  '2 Iron', '3 Iron', '4 Iron', '5 Iron', '6 Iron', '7 Iron', '8 Iron', '9 Iron',
  'Pitching Wedge', 'Gap Wedge', 'Sand Wedge', 'Lob Wedge',
  'Other', 'Hybrid', 'Irons', 'Wedges'
));
