# Taste books

One `<niche>.json` per niche: the editing style the renderer applies by default.
The niche is the campaign's category (`gta`, `minecraft`, `gaming`...), an
official trailer uses `trailer`, anything else falls back to `general`.

**A campaign's brief always wins.** Whatever the brief bans (effects, zooms,
cutting, caption looks) is switched off for that campaign's clips, and the clip's
`meta.json` records what was dropped and why.

- `status: active` books are used. `status: draft` books (built by `taste-build`
  from reference Shorts) are ignored until `taste-approve <niche>`.
- `source: prior` means a starting guess, not measured from references.
- Every claim in a measured book's `.md` cites a file and timestamp.
