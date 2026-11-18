# Taste book: entertainment (DRAFT)

Built 2026-11-16 from 12 top recent Shorts (12 inspected in frames). Not applied until `python -m clipforge taste-approve entertainment`. The campaign brief always overrides this book.

## Sample

| Short | Views | Seconds | Cuts/10 s |
|---|---|---|---|
| https://www.youtube.com/shorts/tQknquZzi0Y | 152,329,945 | 51.5 | 2.91 |
| https://www.youtube.com/shorts/U_KqjSHSiJw | 104,794,936 | 42.9 | 6.06 |
| https://www.youtube.com/shorts/Hwjai8X7v04 | 104,252,777 | 21.44 | 0.93 |
| https://www.youtube.com/shorts/1m_ce9nYqLw | 102,820,557 | 20.87 | 0.96 |
| https://www.youtube.com/shorts/Z3y2LIKXV8o | 88,931,607 | 19.4 | 4.64 |
| https://www.youtube.com/shorts/0flO-ekO-qo | 86,487,239 | 34.78 | 9.49 |
| https://www.youtube.com/shorts/mnSUV0JqnsQ | 82,770,011 | 49.58 | 0.0 |
| https://www.youtube.com/shorts/0jvFKiHjfX8 | 81,058,683 | 14.6 | 8.22 |
| https://www.youtube.com/shorts/BUwZ0K4VrtU | 59,601,620 | 23.64 | 2.54 |
| https://www.youtube.com/shorts/JmCWzbphad4 | 55,784,338 | 59.92 | 6.51 |
| https://www.youtube.com/shorts/tlOwmTRsCbc | 49,722,044 | 37.6 | 6.12 |
| https://www.youtube.com/shorts/MP-kdWYT9qM | 42,464,376 | 33.64 | 2.08 |

## Measured (numbers from ffmpeg / Whisper)

- **Median 3.77 hard cuts per 10 s, median shot 1.42 s** (12 of 12)
  - https://www.youtube.com/shorts/tQknquZzi0Y?t=3 at 3.73 s: first hard cut
  - https://www.youtube.com/shorts/U_KqjSHSiJw?t=1 at 1.57 s: first hard cut
  - https://www.youtube.com/shorts/Hwjai8X7v04?t=18 at 18.7 s: first hard cut
  - https://www.youtube.com/shorts/1m_ce9nYqLw?t=3 at 3.2 s: first hard cut
- **Speech runs at a median 1.62 words per second** (12 of 12)

## Looked at (vision model over frames, each cited)

- **Burned-in captions are used** (4 of 12)
  - https://www.youtube.com/shorts/U_KqjSHSiJw?t=0 at 0.4 s: middle, black
  - https://www.youtube.com/shorts/mnSUV0JqnsQ?t=0 at 0.4 s: bottom, white
  - https://www.youtube.com/shorts/BUwZ0K4VrtU?t=0 at 0.4 s: middle, white
  - https://www.youtube.com/shorts/tlOwmTRsCbc?t=0 at 0.4 s: middle, white
- **A large hook title is on screen from the first frame** (7 of 12)
  - https://www.youtube.com/shorts/tQknquZzi0Y?t=0 at 0.4 s: bottom
  - https://www.youtube.com/shorts/1m_ce9nYqLw?t=0 at 0.4 s: top
  - https://www.youtube.com/shorts/Z3y2LIKXV8o?t=0 at 0.4 s: top
  - https://www.youtube.com/shorts/mnSUV0JqnsQ?t=0 at 0.4 s: top
- **Added emoji graphics appear** (4 of 12)
  - https://www.youtube.com/shorts/U_KqjSHSiJw?t=0 at 0.4 s: emoji
  - https://www.youtube.com/shorts/1m_ce9nYqLw?t=0 at 0.4 s: emoji
  - https://www.youtube.com/shorts/1m_ce9nYqLw?t=5 at 5.22 s: emoji
  - https://www.youtube.com/shorts/1m_ce9nYqLw?t=10 at 10.44 s: emoji
- **Added text banner graphics appear** (1 of 12)
  - https://www.youtube.com/shorts/1m_ce9nYqLw?t=0 at 0.4 s: text_banner
- **Added logo graphics appear** (1 of 12)
  - https://www.youtube.com/shorts/Z3y2LIKXV8o?t=9 at 9.7 s: logo

## Interpretation (judgement, not measured)

- Punch-in zoom is ON: the references cut 3.77 times per 10 s (a fast edit). A zoom is a stand-in for that pace; zooms themselves were not measured. Jump cuts are ON: at 3.77 cuts per 10 s the references are tightly edited, so filler lines and long silences are cut from our clips.

## Not supported by the renderer yet

- fewer than half the references show captions; ClipForge keeps its default captions
- emoji graphics are in the references; the renderer has no support for them yet
- text_banner graphics are in the references; the renderer has no support for them yet
- logo graphics are in the references; the renderer has no support for them yet

## Unknown

- Zoom and punch-in use in the references (frame sampling cannot tell a zoom from a camera move).
- Music, sound effects and sound design (only loudness peaks were measured).
- Which edits were made by the original creator and which were later additions.
