# Taste book: streamer-replays (DRAFT)

Built 2026-11-17 from 12 top recent Shorts (12 inspected in frames). Not applied until `python -m clipforge taste-approve streamer-replays`. The campaign brief always overrides this book.

## Sample

| Short | Views | Seconds | Cuts/10 s |
|---|---|---|---|
| https://www.youtube.com/shorts/mL7BYp8QRSU | 9,795,939 | 15.68 | 0.0 |
| https://www.youtube.com/shorts/_IE9yLGg8SY | 2,728,105 | 40.28 | 0.99 |
| https://www.youtube.com/shorts/ikWV9HelIXo | 2,058,264 | 15.2 | 0.66 |
| https://www.youtube.com/shorts/E7qnIP_MNI8 | 1,987,333 | 21.32 | 1.41 |
| https://www.youtube.com/shorts/VR09RyEUflY | 1,319,150 | 46.76 | 2.57 |
| https://www.youtube.com/shorts/ipfK23ODkrI | 1,110,985 | 25.04 | 0.0 |
| https://www.youtube.com/shorts/fjXLuWzjLtI | 496,594 | 30.62 | 0.0 |
| https://www.youtube.com/shorts/xIVyyRVTxiQ | 463,308 | 16.58 | 1.81 |
| https://www.youtube.com/shorts/zN94GJfblYc | 382,172 | 24.06 | 0.83 |
| https://www.youtube.com/shorts/KxQIlWH-Zcg | 368,990 | 35.8 | 1.68 |
| https://www.youtube.com/shorts/xKESBGy1wAY | 348,591 | 50.74 | 0.39 |
| https://www.youtube.com/shorts/sKz_p9vQrb0 | 331,836 | 26.4 | 2.27 |

## Measured (numbers from ffmpeg / Whisper)

- **Median 0.91 hard cuts per 10 s, median shot 6.61 s** (12 of 12)
  - https://www.youtube.com/shorts/_IE9yLGg8SY?t=7 at 7.0 s: first hard cut
  - https://www.youtube.com/shorts/ikWV9HelIXo?t=9 at 9.93 s: first hard cut
  - https://www.youtube.com/shorts/E7qnIP_MNI8?t=7 at 7.4 s: first hard cut
  - https://www.youtube.com/shorts/VR09RyEUflY?t=6 at 6.43 s: first hard cut
- **Speech runs at a median 1.0 words per second** (12 of 12)

## Looked at (vision model over frames, each cited)

- **Burned-in captions are used** (6 of 12)
  - https://www.youtube.com/shorts/_IE9yLGg8SY?t=0 at 0.4 s: middle, white
  - https://www.youtube.com/shorts/E7qnIP_MNI8?t=0 at 0.4 s: lower_third, white
  - https://www.youtube.com/shorts/VR09RyEUflY?t=0 at 0.4 s: lower_third, white
  - https://www.youtube.com/shorts/KxQIlWH-Zcg?t=0 at 0.4 s: middle, white
- **Captions sit at the lower third** (3 of 6)
- **A large hook title is on screen from the first frame** (7 of 12)
  - https://www.youtube.com/shorts/_IE9yLGg8SY?t=0 at 0.4 s: top
  - https://www.youtube.com/shorts/E7qnIP_MNI8?t=0 at 0.4 s: lower_third
  - https://www.youtube.com/shorts/ipfK23ODkrI?t=0 at 0.4 s: top
  - https://www.youtube.com/shorts/fjXLuWzjLtI?t=0 at 0.4 s: top
- **Added emoji graphics appear** (4 of 12)
  - https://www.youtube.com/shorts/_IE9yLGg8SY?t=0 at 0.4 s: emoji
  - https://www.youtube.com/shorts/E7qnIP_MNI8?t=0 at 0.4 s: emoji
  - https://www.youtube.com/shorts/VR09RyEUflY?t=0 at 0.4 s: emoji
  - https://www.youtube.com/shorts/xIVyyRVTxiQ?t=0 at 0.4 s: emoji
- **Added text banner graphics appear** (3 of 12)
  - https://www.youtube.com/shorts/E7qnIP_MNI8?t=10 at 10.66 s: text_banner
  - https://www.youtube.com/shorts/E7qnIP_MNI8?t=15 at 15.99 s: text_banner
  - https://www.youtube.com/shorts/zN94GJfblYc?t=0 at 0.4 s: text_banner
  - https://www.youtube.com/shorts/zN94GJfblYc?t=6 at 6.01 s: text_banner
- **Added logo graphics appear** (3 of 12)
  - https://www.youtube.com/shorts/zN94GJfblYc?t=0 at 0.4 s: logo
  - https://www.youtube.com/shorts/zN94GJfblYc?t=6 at 6.01 s: logo
  - https://www.youtube.com/shorts/zN94GJfblYc?t=12 at 12.03 s: logo
  - https://www.youtube.com/shorts/zN94GJfblYc?t=18 at 18.04 s: logo

## Interpretation (judgement, not measured)

- Punch-in zoom is OFF: the references cut 0.91 times per 10 s (a slow edit). A zoom is a stand-in for that pace; zooms themselves were not measured. Jump cuts are OFF: the references are not cut tightly enough to justify trimming lines.

## Not supported by the renderer yet

- captions show 6 words at once in the references; ClipForge keeps 1-4 word captions
- emoji graphics are in the references: reaction emoji are on (up to 3 a clip, from the words spoken)
- text_banner graphics are in the references; the renderer has no support for them yet
- logo graphics are in the references; the renderer has no support for them yet

## Unknown

- Zoom and punch-in use in the references (frame sampling cannot tell a zoom from a camera move).
- Music, sound effects and sound design (only loudness peaks were measured).
- Which edits were made by the original creator and which were later additions.
