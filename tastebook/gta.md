# Taste book: gta (DRAFT)

Built 2026-11-15 from 10 top recent Shorts (10 inspected in frames). Not applied until `python -m clipforge taste-approve gta`. The campaign brief always overrides this book.

## Sample

| Short | Views | Seconds | Cuts/10 s |
|---|---|---|---|
| https://www.youtube.com/shorts/VFfv92Jatpg | 68,033,268 | 53.52 | 1.68 |
| https://www.youtube.com/shorts/smybfovlQUQ | 54,751,888 | 21.36 | 2.81 |
| https://www.youtube.com/shorts/PMj1rfqUARs | 35,087,423 | 57.28 | 1.57 |
| https://www.youtube.com/shorts/jDGS0R4eHuU | 27,223,270 | 27.36 | 4.39 |
| https://www.youtube.com/shorts/b3_4zd0--OM | 25,689,210 | 30.04 | 3.33 |
| https://www.youtube.com/shorts/aDvKwpRT31Y | 23,063,152 | 30.04 | 3.33 |
| https://www.youtube.com/shorts/7bgKiLrQW5U | 22,321,284 | 19.98 | 4.0 |
| https://www.youtube.com/shorts/mRELuCzMwEg | 19,750,894 | 33.52 | 1.79 |
| https://www.youtube.com/shorts/EocsE3pFnAI | 17,935,943 | 21.42 | 5.14 |
| https://www.youtube.com/shorts/mQUhZgeHv34 | 16,119,743 | 26.02 | 0.0 |

## Measured (numbers from ffmpeg / Whisper)

- **Median 3.07 hard cuts per 10 s, median shot 1.65 s** (10 of 10)
  - https://www.youtube.com/shorts/VFfv92Jatpg?t=7 at 7.6 s: first hard cut
  - https://www.youtube.com/shorts/smybfovlQUQ?t=2 at 2.97 s: first hard cut
  - https://www.youtube.com/shorts/PMj1rfqUARs?t=12 at 12.4 s: first hard cut
  - https://www.youtube.com/shorts/jDGS0R4eHuU?t=1 at 1.9 s: first hard cut
- **Speech runs at a median 0.92 words per second** (10 of 10)

## Looked at (vision model over frames, each cited)

- **Burned-in captions are used** (4 of 10)
  - https://www.youtube.com/shorts/VFfv92Jatpg?t=0 at 0.4 s: bottom, white
  - https://www.youtube.com/shorts/PMj1rfqUARs?t=0 at 0.4 s: top, white
  - https://www.youtube.com/shorts/EocsE3pFnAI?t=0 at 0.4 s: middle, white
  - https://www.youtube.com/shorts/mQUhZgeHv34?t=0 at 0.4 s: bottom, white
- **A large hook title is on screen from the first frame** (4 of 10)
  - https://www.youtube.com/shorts/VFfv92Jatpg?t=0 at 0.4 s: top
  - https://www.youtube.com/shorts/smybfovlQUQ?t=0 at 0.4 s: top
  - https://www.youtube.com/shorts/PMj1rfqUARs?t=0 at 0.4 s: top
  - https://www.youtube.com/shorts/mRELuCzMwEg?t=0 at 0.4 s: top
- **Added emoji graphics appear** (2 of 10)
  - https://www.youtube.com/shorts/VFfv92Jatpg?t=40 at 40.14 s: emoji
  - https://www.youtube.com/shorts/mRELuCzMwEg?t=0 at 0.4 s: emoji
- **Added logo graphics appear** (1 of 10)
  - https://www.youtube.com/shorts/smybfovlQUQ?t=0 at 0.4 s: logo
  - https://www.youtube.com/shorts/smybfovlQUQ?t=5 at 5.34 s: logo
  - https://www.youtube.com/shorts/smybfovlQUQ?t=10 at 10.68 s: logo
  - https://www.youtube.com/shorts/smybfovlQUQ?t=16 at 16.02 s: logo
- **Added arrow graphics appear** (1 of 10)
  - https://www.youtube.com/shorts/mQUhZgeHv34?t=0 at 0.4 s: arrow

## Interpretation (judgement, not measured)

- Punch-in zoom is ON: the references cut 3.07 times per 10 s (a fast edit). A zoom is a stand-in for that pace; zooms themselves were not measured.

## Not supported by the renderer yet

- fewer than half the references show captions; ClipForge keeps its default captions
- emoji graphics are in the references; the renderer has no support for them yet
- logo graphics are in the references; the renderer has no support for them yet
- arrow graphics are in the references; the renderer has no support for them yet

## Unknown

- Zoom and punch-in use in the references (frame sampling cannot tell a zoom from a camera move).
- Music, sound effects and sound design (only loudness peaks were measured).
- Which edits were made by the original creator and which were later additions.
