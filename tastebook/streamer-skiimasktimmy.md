# Taste book: streamer-skiimasktimmy (DRAFT)

Built 2026-11-17 from 12 top recent Shorts (12 inspected in frames). Not applied until `python -m clipforge taste-approve streamer-skiimasktimmy`. The campaign brief always overrides this book.

## Sample

| Short | Views | Seconds | Cuts/10 s |
|---|---|---|---|
| https://www.youtube.com/shorts/mzg0CX0mUgc | 2,128 | 28.62 | 0.0 |
| https://www.youtube.com/shorts/QRqU_eKdJVo | 1,975 | 16.04 | 1.25 |
| https://www.youtube.com/shorts/8C3NzEEOvG0 | 1,937 | 36.2 | 0.0 |
| https://www.youtube.com/shorts/ThAo8zPk2cA | 1,871 | 27.02 | 0.0 |
| https://www.youtube.com/shorts/QRVVFB9TTAU | 1,802 | 21.7 | 1.38 |
| https://www.youtube.com/shorts/CtMIbPMMcxI | 1,747 | 39.08 | 0.26 |
| https://www.youtube.com/shorts/KCx2YD81ZQk | 1,583 | 25.68 | 0.0 |
| https://www.youtube.com/shorts/RXqXv_xCRrc | 1,474 | 20.36 | 0.0 |
| https://www.youtube.com/shorts/urDi5MNQ5kQ | 1,422 | 21.02 | 0.48 |
| https://www.youtube.com/shorts/13XYWix96nQ | 1,404 | 25.62 | 0.78 |
| https://www.youtube.com/shorts/-tqQqPjEEVU | 1,391 | 23.04 | 0.87 |
| https://www.youtube.com/shorts/Bc38HKlRoyo | 1,375 | 19.46 | 0.0 |

## Measured (numbers from ffmpeg / Whisper)

- **Median 0.13 hard cuts per 10 s, median shot 19.5 s** (12 of 12)
  - https://www.youtube.com/shorts/QRqU_eKdJVo?t=3 at 3.03 s: first hard cut
  - https://www.youtube.com/shorts/QRVVFB9TTAU?t=8 at 8.2 s: first hard cut
  - https://www.youtube.com/shorts/CtMIbPMMcxI?t=14 at 14.13 s: first hard cut
  - https://www.youtube.com/shorts/urDi5MNQ5kQ?t=6 at 6.47 s: first hard cut
- **Speech runs at a median 2.5 words per second** (12 of 12)

## Looked at (vision model over frames, each cited)

- **Burned-in captions are used** (12 of 12)
  - https://www.youtube.com/shorts/mzg0CX0mUgc?t=0 at 0.4 s: middle, yellow
  - https://www.youtube.com/shorts/QRqU_eKdJVo?t=0 at 0.4 s: bottom, white
  - https://www.youtube.com/shorts/8C3NzEEOvG0?t=0 at 0.4 s: middle, white
  - https://www.youtube.com/shorts/ThAo8zPk2cA?t=0 at 0.4 s: middle, white
- **Captions show about 4 words at a time** (12 of 12)
- **Captions sit at the middle** (6 of 12)
- **The spoken word is highlighted** (7 of 12)
  - https://www.youtube.com/shorts/QRqU_eKdJVo?t=0 at 0.4 s: white
  - https://www.youtube.com/shorts/ThAo8zPk2cA?t=0 at 0.4 s: white
  - https://www.youtube.com/shorts/KCx2YD81ZQk?t=0 at 0.4 s: white
  - https://www.youtube.com/shorts/urDi5MNQ5kQ?t=0 at 0.4 s: white
- **A large hook title is on screen from the first frame** (12 of 12)
  - https://www.youtube.com/shorts/mzg0CX0mUgc?t=0 at 0.4 s: top
  - https://www.youtube.com/shorts/QRqU_eKdJVo?t=0 at 0.4 s: middle
  - https://www.youtube.com/shorts/8C3NzEEOvG0?t=0 at 0.4 s: middle
  - https://www.youtube.com/shorts/ThAo8zPk2cA?t=0 at 0.4 s: middle
- **Added logo graphics appear** (12 of 12)
  - https://www.youtube.com/shorts/mzg0CX0mUgc?t=0 at 0.4 s: logo
  - https://www.youtube.com/shorts/mzg0CX0mUgc?t=7 at 7.16 s: logo
  - https://www.youtube.com/shorts/mzg0CX0mUgc?t=14 at 14.31 s: logo
  - https://www.youtube.com/shorts/mzg0CX0mUgc?t=21 at 21.46 s: logo
- **Added text banner graphics appear** (4 of 12)
  - https://www.youtube.com/shorts/QRqU_eKdJVo?t=0 at 0.4 s: text_banner
  - https://www.youtube.com/shorts/8C3NzEEOvG0?t=0 at 0.4 s: text_banner
  - https://www.youtube.com/shorts/QRVVFB9TTAU?t=0 at 0.4 s: text_banner
  - https://www.youtube.com/shorts/KCx2YD81ZQk?t=0 at 0.4 s: text_banner
- **Added other graphics appear** (1 of 12)
  - https://www.youtube.com/shorts/CtMIbPMMcxI?t=0 at 0.4 s: other

## Interpretation (judgement, not measured)

- Punch-in zoom is OFF: the references cut 0.13 times per 10 s (a slow edit). A zoom is a stand-in for that pace; zooms themselves were not measured. Jump cuts are OFF: the references are not cut tightly enough to justify trimming lines.

## Not supported by the renderer yet

- logo graphics are in the references; the renderer has no support for them yet
- text_banner graphics are in the references; the renderer has no support for them yet
- other graphics are in the references; the renderer has no support for them yet

## Unknown

- Zoom and punch-in use in the references (frame sampling cannot tell a zoom from a camera move).
- Music, sound effects and sound design (only loudness peaks were measured).
- Which edits were made by the original creator and which were later additions.
