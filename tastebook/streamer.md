# Taste book: streamer (DRAFT)

Built 2026-11-15 from 12 top recent Shorts (12 inspected in frames). Not applied until `python -m clipforge taste-approve streamer`. The campaign brief always overrides this book.

## Sample

| Short | Views | Seconds | Cuts/10 s |
|---|---|---|---|
| https://www.youtube.com/shorts/1m_ce9nYqLw | 101,585,683 | 20.87 | 0.96 |
| https://www.youtube.com/shorts/6nMuG0s3mwk | 50,777,873 | 58.96 | 0.68 |
| https://www.youtube.com/shorts/7AxDq2_6Q3w | 35,343,032 | 49.62 | 2.62 |
| https://www.youtube.com/shorts/wI89V8wQwi8 | 34,014,469 | 21.92 | 0.0 |
| https://www.youtube.com/shorts/1i8kGgZEdC0 | 30,045,072 | 42.98 | 4.42 |
| https://www.youtube.com/shorts/df3cY2Zz7P8 | 29,785,458 | 31.28 | 0.0 |
| https://www.youtube.com/shorts/YkdcveGeDg8 | 26,183,535 | 21.62 | 0.0 |
| https://www.youtube.com/shorts/L4G1gjAgUuQ | 23,849,920 | 40.6 | 1.97 |
| https://www.youtube.com/shorts/BriWNm0aD7g | 21,059,438 | 34.1 | 1.17 |
| https://www.youtube.com/shorts/SaerEANcuBc | 20,883,834 | 22.4 | 4.91 |
| https://www.youtube.com/shorts/cngOte_m4f8 | 20,155,578 | 15.22 | 3.94 |
| https://www.youtube.com/shorts/T_tHLL8YRBo | 18,751,950 | 51.3 | 0.97 |

## Measured (numbers from ffmpeg / Whisper)

- **Median 1.07 hard cuts per 10 s, median shot 4.13 s** (12 of 12)
  - https://www.youtube.com/shorts/1m_ce9nYqLw?t=3 at 3.2 s: first hard cut
  - https://www.youtube.com/shorts/6nMuG0s3mwk?t=19 at 19.9 s: first hard cut
  - https://www.youtube.com/shorts/7AxDq2_6Q3w?t=2 at 2.9 s: first hard cut
  - https://www.youtube.com/shorts/1i8kGgZEdC0?t=2 at 2.3 s: first hard cut
- **Speech runs at a median 1.46 words per second** (12 of 12)

## Looked at (vision model over frames, each cited)

- **Burned-in captions are used** (9 of 12)
  - https://www.youtube.com/shorts/6nMuG0s3mwk?t=0 at 0.4 s: lower_third, white
  - https://www.youtube.com/shorts/7AxDq2_6Q3w?t=0 at 0.4 s: middle, purple
  - https://www.youtube.com/shorts/wI89V8wQwi8?t=0 at 0.4 s: bottom, white
  - https://www.youtube.com/shorts/df3cY2Zz7P8?t=0 at 0.4 s: middle, white
- **Captions show about 3 words at a time** (9 of 9)
- **Captions sit at the middle** (6 of 9)
- **A large hook title is on screen from the first frame** (9 of 12)
  - https://www.youtube.com/shorts/1m_ce9nYqLw?t=0 at 0.4 s: top
  - https://www.youtube.com/shorts/6nMuG0s3mwk?t=0 at 0.4 s: top
  - https://www.youtube.com/shorts/1i8kGgZEdC0?t=0 at 0.4 s: top
  - https://www.youtube.com/shorts/df3cY2Zz7P8?t=0 at 0.4 s: top
- **Added emoji graphics appear** (3 of 12)
  - https://www.youtube.com/shorts/1m_ce9nYqLw?t=0 at 0.4 s: emoji
  - https://www.youtube.com/shorts/L4G1gjAgUuQ?t=0 at 0.4 s: emoji
  - https://www.youtube.com/shorts/L4G1gjAgUuQ?t=10 at 10.15 s: emoji
  - https://www.youtube.com/shorts/L4G1gjAgUuQ?t=20 at 20.3 s: emoji
- **Added logo graphics appear** (3 of 12)
  - https://www.youtube.com/shorts/1m_ce9nYqLw?t=0 at 0.4 s: logo
  - https://www.youtube.com/shorts/6nMuG0s3mwk?t=0 at 0.4 s: logo
  - https://www.youtube.com/shorts/6nMuG0s3mwk?t=14 at 14.74 s: logo
  - https://www.youtube.com/shorts/6nMuG0s3mwk?t=29 at 29.48 s: logo
- **Added text banner graphics appear** (2 of 12)
  - https://www.youtube.com/shorts/1m_ce9nYqLw?t=0 at 0.4 s: text_banner
  - https://www.youtube.com/shorts/df3cY2Zz7P8?t=0 at 0.4 s: text_banner
  - https://www.youtube.com/shorts/df3cY2Zz7P8?t=7 at 7.82 s: text_banner
  - https://www.youtube.com/shorts/df3cY2Zz7P8?t=15 at 15.64 s: text_banner

## Interpretation (judgement, not measured)

- Punch-in zoom is OFF: the references cut 1.07 times per 10 s (a slow edit). A zoom is a stand-in for that pace; zooms themselves were not measured.

## Not supported by the renderer yet

- emoji graphics are in the references: reaction emoji are on (up to 3 a clip, from the words spoken)
- logo graphics are in the references; the renderer has no support for them yet
- text_banner graphics are in the references; the renderer has no support for them yet

## Unknown

- Zoom and punch-in use in the references (frame sampling cannot tell a zoom from a camera move).
- Music, sound effects and sound design (only loudness peaks were measured).
- Which edits were made by the original creator and which were later additions.
