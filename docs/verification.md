# Verification of the PRD's `VERIFY` items

Checked 12 Nov 2026 from a cloud session, re-checked the same evening on the laptop. Where this file and `CLIPFORGE_PRD.md` differ, this file wins.

> **Read this first.** Item 1 — the fact the whole publishing design was built on — **is no longer true.** YouTube removed the restriction on 2 November 2026, ten days before this document was written. Direct upload from our own project is now possible and Buffer may not be needed at all. See "The big change" below. Nothing has been rebuilt yet: this is a Phase 5 decision and Yaish's call.

Status key: **Confirmed** (vendor's own page says so) · **Partly** (only third-party write-ups, or vendor page is silent on part of it) · **Open** (not checked or not answerable yet).

| # | Claim | Status | What the source says |
|---|---|---|---|
| 1 | Uploads from an unaudited YouTube API project are locked private | **FALSE since 2 Nov 2026** | Read from the laptop, 12 Nov 2026. `videos.insert` now says, word for word: "Videos uploaded from unverified API projects are not restricted to private viewing mode." and "If you don't specify a value for this property, the uploaded video's privacy status defaults to `public`." The revision history dates the change: 28 July 2020 added the restriction, and **2 November 2026** removed it ("Videos uploaded through the `videos.insert` method from unverified API projects are no longer restricted to private viewing mode"). The write-ups the cloud session relied on were describing the old rule. See "The big change". |
| 2 | Re-uploaded Shorts with nothing added get less reach; reused-content policy applies even with permission | Partly | Reported by Search Engine Journal and others. Content Rewards' own guide agrees on the second half: permission to use footage does not by itself satisfy YouTube's reused-content rules. |
| 3 | Campaign footage is licensed only for that campaign | Confirmed | Content Rewards: brand-supplied footage is licensed only to make clips for that campaign. |
| 4a | Content Rewards: submit within 30 minutes of posting | Confirmed | Every submission must pass three checks: posted from an account connected in Content Rewards, not already submitted, and posted within the last 30 minutes. A miss cannot be fixed; you must post a fresh upload. The help-centre FAQ does not repeat the deadline, so treat the stricter page as binding. |
| 4b | Vyro counts views from submission | Partly | Third-party guides only. Not found on a Vyro page. |
| 5a | Clips are reviewed by the campaign owner | Confirmed | Content Rewards FAQ. Most reviews take a few days. |
| 5b | Third-party watermarks cause rejection | Partly | Third-party guides only. Keep the no-watermark rule regardless. |
| 6 | Payout route for Sri Lanka | Open, and **blocked by a conflict** | Re-read on the laptop 12 Nov 2026; the flow is now known but Sri Lanka still is not. Withdrawal is **two hops**: Content Rewards *Earnings* → **Withdraw** on the balance card (nothing below $1; "below $1 the Withdraw button stays unavailable") moves money to the Whop balance; then on Whop, *Balances* → withdraw to a payout method. Content Rewards says to add that method "on Whop under **Dashboard**, **Balances**, **Payout methods**, then **Add payout method**" — but on a fresh clipper account **Whop's Dashboard asks whether you have a business or want to create one** (seen 12 Nov 2026). So the documented path runs through seller onboarding a clipper may not need. **Unresolved: whether a clipper can add a payout method without creating a Whop business.** Whop lists bank (standard/instant), mobile wallet, crypto wallet, PayPal and Coinbase across 200+ countries, "depending on your country"; PayPal and Coinbase are conditional on how *customers* paid, so they are probably not offered on the Content Rewards route. Settlement in USD/EUR/GBP/CAD/AUD, 0.5% conversion fee. Nothing names Sri Lanka. Vyro's methods were not confirmed on a Vyro page. The PayPal-in-Sri-Lanka point was not checked against PayPal. |
| 7 | Platform API for listing campaigns | Open | Whop documents a Bounties API. Whether Content Rewards campaigns are bounties is not stated. No public Vyro API was found. |
| 8 | Platform API for submitting a link | Partly | Whop's `POST /bounty_submissions` accepts links to posted work (a YouTube Shorts URL is one of its examples). It needs a **user OAuth token** with scope `bounty:submission:create`; account API keys cannot author submissions. Unknown whether it applies to Content Rewards campaigns. |
| 9 | Buffer API can post YouTube Shorts | Partly | Confirmed: `createPost` GraphQL mutation at `api.buffer.com`, Bearer API key, YouTube is a supported channel, API available on the free plan (Buffer's own blog). Shorts up to 3 minutes, 9:16 or 1:1, title up to 100 characters, description up to 5,000. Not confirmed: the YouTube metadata fields and whether auto-published Shorts are public. The `PostInputMetaData` page was read on the laptop on 12 Nov 2026 and only says "Metadata for Youtube post" — it lists no fields at all, so `YoutubePostMetadataInput` cannot be verified from the docs. **Test with one real post before building on it** — and note that item 1 may remove the need for Buffer entirely. |
| 10 | Terms allow automated reading of discover pages / automated submission | Open | Not checked. Both stay switched off until read and until Yaish agrees. |
| 11 | Public stats readable with an API key, no audit | Partly | Standard Data API behaviour; official page not re-read from here. |
| 12 | Actual payouts readable from the platform | Open | Nothing found. Record payouts by hand on the dashboard. |

## The big change: direct upload is allowed again

PRD section 2's first row, and all of section 8, exist to work around a restriction that no longer exists. What follows from item 1:

- **ClipForge can upload to YouTube itself**, public, through `videos.insert` on our own Google Cloud project, with no compliance audit.
- **Buffer may not be needed.** If it goes, so do design changes 1, 2 and 3 below: no media host to rent and configure (that was an open blocker for Yaish), and the new video's id comes straight back from the upload instead of being guessed by polling the channel and matching on title. That matters a lot for the 30-minute submission window in item 4a — the clock currently starts on a publish we can only detect after the fact.
- **Quota:** the page says `videos.insert` costs "1 unit in the Video Uploads quota bucket". A separate upload bucket is new; its daily ceiling is not on that page and is **not yet checked**. At 3-8 posts a day this is very unlikely to bind, but confirm before relying on it.

Two things to settle before any of this is built, both Yaish's:

1. **Uploading needs a user OAuth token**, not an API key — so an OAuth consent screen, and his approval on it. The read-only key in `.env` stays for stats either way.
2. **The 7-day refresh-token trap.** Google's OAuth 2.0 page, verbatim: "A Google Cloud Platform project with an OAuth consent screen configured for an external user type and a publishing status of 'Testing' is issued a refresh token expiring in 7 days, unless the only OAuth scopes requested are a subset of name, email address, and user profile." An always-on uploader left in Testing would therefore stop working every week. The consent screen has to be moved to "In production" — which for the `youtube.upload` scope may pull in Google's own verification review. **Not yet checked: whether a single-user app can publish without that review.** Until it is, assume either weekly re-authentication or a review.

**Recommendation.** Keep the `Publisher` interface the PRD asks for and add a `YouTubeDirect` implementation beside the Buffer one, rather than deleting Buffer first. Build direct upload as first choice in Phase 5 and keep Buffer as the fallback; decide which to keep once Yaish has seen whether the consent screen can go to production. Do not start this before Phase 5 — it changes nothing in Phases 1 to 4.

## What this changes in the design

> Design changes 1, 2 and 3 are consequences of Buffer, and all three disappear if direct upload replaces it. They are kept here because that decision has not been made.

1. **Buffer needs a public URL for the video; it does not take a file upload.** Clips live on the laptop, so Phase 5 needs a small media host. Proposed: an object-storage bucket (for example Cloudflare R2's free tier) where each clip is uploaded, posted, then deleted. This adds a setup step for Yaish and two `.env` values. Decide at the start of Phase 5.
2. **Buffer does not return the YouTube video ID.** After a post is sent, find the new video by reading the channel's uploads through the YouTube Data API (read-only key) and matching on title. The 30-minute clock starts at publish, so poll every minute after a post.
3. **Buffer does not support custom thumbnails for Shorts.** This matches the PRD: the first frame carries the hook.
4. **Content Rewards has its own account connection.** The YouTube channel must be connected and verified inside Content Rewards; an account linked in Whop's settings does not count. Add to Yaish's checklist.
5. **One post, one campaign, once.** Never submit the same post to two campaigns. Enforce in the `posts` table.
6. **Some campaigns need pre-approval**: upload a draft, wait for the team, then post. The brief parser must flag these (`rules_json.pre_approval = true`) and the pipeline must hold those clips instead of posting.
7. **Videos must be made after joining the campaign.** Never clip footage for a campaign before its `joined_at`.
8. **The fee is not a flat 7%.** Content Rewards' own help, re-read 12 Nov 2026: "The creator fee is taken at the moment a payout pays" and "the rate depends on the campaign and sometimes the creator", with nothing taken on per-post and retainer models for campaigns with budgets of $5,000 or more. So the earnings maths cannot hold a 7% constant — the fee is a **per-campaign value**, and `campaigns` has no column for it yet (a migration for Phase 5). Until a real payout confirms a number, show estimated earnings gross with the fee named as unknown rather than quietly subtracting 7%. Each campaign's own minimum payout still applies.
9. **Earning windows are time-boxed, which sets how long stats must be collected.** Clips submitted before 25 September 2026 stop earning 7 days after approval, then a 3-day hold, paying about day 10; newer clips run on 30-day cycles with several payment rounds. Per-post payouts arrive about 15 minutes after approval. The stats collector (PRD section 10, every 6 hours) therefore has to keep polling a clip for at least its full earning window, not just a few days, and the dashboard's "pending" vs "approved" split matters more than the PRD assumed.
9. **Bot detection is on views, not tools.** The FAQ says clips with suspicious view spikes are flagged and reviewed by hand. Hard limit 4 (no fake engagement) already covers this.

## Sources

- https://contentrewards.com/articles/get-paid-to-clip-videos
- https://help.contentrewards.com/en/articles/13260718-faqs
- https://docs.whop.com/developer/bounties/overview.md
- https://docs.whop.com/api-reference/beta/bounty-submissions/create-bounty-submission.md
- https://docs.whop.com/manage-your-business/manage-payouts/payout-methods.md
- https://developers.buffer.com/examples/create-video-post.html
- https://developers.buffer.com/guides/posts-and-scheduling.md
- https://support.buffer.com/article/562-using-youtube-shorts-with-buffer
- https://buffer.com/resources/social-media-api-multi-platform-posting/
- https://developers.google.com/youtube/v3/docs/videos/insert (read 12 Nov 2026 on the laptop)
- https://developers.google.com/youtube/v3/revision_history (dates the 2 Nov 2026 reversal)
- https://developers.google.com/identity/protocols/oauth2 (the 7-day refresh token in Testing status)
- https://singhamandeep.com/?p=1462 (quotes Google's videos.insert page as it read BEFORE 2 Nov 2026)
- https://searchenginejournal.com/youtube-original-shorts-reposted-clips-reach/591774/

## Still to read

Done on the laptop, 12 Nov 2026:

- [x] `videos.insert` and the revision history → item 1 is false; see "The big change".
- [x] Google's OAuth 2.0 page → the 7-day refresh token in Testing publishing status.
- [~] `developers.buffer.com/types/PostInputMetaData.md` → the page only says "Metadata for Youtube post" and lists no fields, so `YoutubePostMetadataInput` is **still unverified**. If Buffer survives the direct-upload decision, the fields have to come from a real API call, not the docs.

Still open:

- The daily ceiling on the new "Video Uploads" quota bucket.
- **Whether a clipper can add a Whop payout method without creating a Whop business.** Content Rewards sends you to Dashboard -> Balances -> Payout methods; a fresh clipper account is asked to create a business there instead. Ask Whop support rather than creating one blind: seller onboarding means accepting seller terms and probably tax details, which is Yaish's call (hard limit 6).
- Which payout methods Whop actually offers a creator in **Sri Lanka** (ask support; the in-app list may only render in the withdraw modal, which needs a balance).
- The real creator-fee rate for the campaigns Yaish joins, to replace the dropped 7% constant.
- Whether an OAuth consent screen using `youtube.upload` can be moved to "In production" for a single user without Google's verification review.
- Buffer's rate-limit guide (only if Buffer survives).
- Whop and Vyro terms of service, for automated access (item 10). Both stay switched off until read and until Yaish agrees.
- Vyro's help centre: payouts, submission rules.
