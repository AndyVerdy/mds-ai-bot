# Session Log

Newest entry at top. Each entry uses the context-handoff template: what was
done, decisions, files touched, QA, known issues, test environment, open
questions, next steps.

The canonical version of this log is mirrored to ClickUp Page 12:
https://app.clickup.com/2264119/docs/2531q-98297/2531q-61477 — keep both in
sync per the context-handoff skill protocol.

* * *

## 2026-05-06 (latest) — Builds (28)→(40): Listen feature, full-name enrichment, Live Activity removed, splash, 11 build iterations

**AI / dev:** Claude Opus 4.7
**Duration:** ~6h continuous (after the build (27) entry below)
**ClickUp doc:** [2531q-98297](https://app.clickup.com/2264119/docs/2531q-98297/2531q-61237)
**Branches / repos touched:**
- `mds-ai-bot/main` — 6 commits (devices/push payload, /api/tts, today fallback, full-name enrichment, links formatter, payload cleanup)
- `mds-ios-app/design-system-trial` — 13 commits, builds (28) → (40)

### What was done

Andy installed (26) on TestFlight, walked through the app, sent feedback in
batches as he tested. We iterated through 13 builds across the trial branch
plus 6 backend commits in the same session. Headline outcomes:

- **Listen feature now uses ElevenLabs** (Mark voice) with full audio
  controls + lock-screen Now Playing.
- **Live Activity feature scrapped** entirely after Andy saw it on real
  device — duplicative with the push, confusing, can't be cleared by user.
- **Backend digest read path enriches** first names → full names from AT
  Members and formats `links_shared` into readable paragraphs.
- **Splash animation** on cold launch.
- **Tab renamed Search → Home** with house icon.
- **Six bug fixes** (timezone, horizontal pan, Brief empty state, audio
  bar overlap with tab bar, voice fallback to George stale-cache,
  /api/today single-day fallback).

#### A. iOS audio playback (the biggest single area)

`SpeechController` rewrote in build (32) → (39) into a real two-mode
player:

- **`.remote`** — `/api/tts` MP3 streamed via AVAudioPlayer with full
  controls: pause/resume, seek(by:)/(to:), playbackRate (0.75 / 1.0 /
  1.25 / 1.5 / 2.0 — persisted in UserDefaults), exposed duration +
  currentTime via @Published, 0.25s timer pumps the UI.
- **`.local`** — AVSpeechSynthesizer fallback when /api/tts fails. No
  seek, no rate, no lock-screen integration; the audio bar shows
  "Apple voice (fallback)" so the user knows.

Lock-screen Now Playing (`MPNowPlayingInfoCenter` + `MPRemoteCommandCenter`):

- Title + "MDS Knowledge Base" artist + album + duration + elapsed +
  rate + mediaType + isLiveStream + 512×512 generated artwork.
- Commands armed: play, pause, togglePlayPause, skipForward[10],
  skipBackward[10], nextTrack, previousTrack, changePlaybackPosition.
  Other commands explicitly disabled so the lock screen doesn't show
  ghost buttons.
- `UIApplication.shared.beginReceivingRemoteControlEvents()` called
  once on first config (still required in practice despite being
  "deprecated" in iOS 13+).
- Setup moved from lazy (first speak) to eager (SpeechController.init)
  in build (39) — iOS won't surface the lock-screen widget unless
  these are armed before audio actually starts.
- `.duckOthers` option dropped in build (39) — iOS may treat ducking
  apps as "secondary" audio and skip Now Playing.

`AudioPlayerBar` (new view) — sticky bottom bar shown when speech.mode
!= .idle:

- Layout (after build 39 reorder): `[⋯][1×] | [⏪10][▶/⏸][⏩10] | [✕]`.
  Settings cluster on left, transport CENTERED with 48pt accent-filled
  play button, close on right. Matches Apple Music / Spotify / podcast
  convention.
- Progress bar + elapsed/total times.
- Speed pill → confirmation dialog.
- ⋯ menu → autoplay-next toggle.
- ✕ → stop and dismiss.

In build (39), `SpeechController` was lifted from DigestDetailView's
@StateObject to MDSKnowledgeBaseApp's app-level @StateObject + injected
as env-object. `AudioPlayerBar` moved to ContentView's ZStack, conditional
on speech.mode. Audio + the bar now persist across screens — start Listen
on a digest, navigate to Home, the bar stays floating. Andy's request:
"when the audio is playing if im in the app and go to a new screen i
should still see the play controls."

Autoplay-next: `speech.onFinishedNaturally` fires when an MP3 plays to
completion (not on user-stop). When `autoplayNext` is on AND siblings
has next, DigestDetailView moves currentId forward and calls speak() on
the new digest. Setting exposed in two places: audio bar's overflow
menu + Settings → Listen section.

`Info.plist UIBackgroundModes += [audio]` so playback continues with
the screen locked.

#### B. iOS UI fixes & polish

- **Build (28)**: Today section dropped from Georgia 24 → body 17 (was
  rendering as one giant title, not a summary). "Today across MDS"
  header, per-channel rows now navigate to digest detail (not seed
  search). Hero subtitle dropped the hardcoded "5". "From the archive"
  placeholder removed entirely. Listen audio session set to .playback /
  .spokenAudio so it actually emits sound. Source rows two-line layout
  (title row 1, date+meta row 2) + dedupe on (kind, identity-key) to
  collapse 4× duplicate WA digest sources.
- **Build (29)**: Auto-prompt notification permission after login
  (system dialog appears once after first sign-in).
- **Build (30)**: Pure version bump after Andy archived (29) twice and
  Apple rejected the duplicate identifier.
- **Build (33)**: "TODAY ACROSS MDS" → "**THE BRIEF · TUE · MAY 7**"
  (date kicker, accent color). Prev/next digest navigation: bottom-of-
  content nav row, sibling list from DigestsStore, mark-read on page,
  fade between digests, 1/N counter.
- **Build (35)**: Audio bar bottom padding bumped to clear the
  GlassTabBar (was hidden behind it). Lock-screen artwork added.
- **Build (36)**: Timezone fix — `parsedDate` parses UTC, but three
  formatters rendered in local timezone. Pacific devices saw "Monday,
  May 4" for May 5 UTC data. Fixed in DigestsView.subtitle,
  DigestDetailView.kickerText, EmptyStateView.briefDateLabel.
- **Build (37)**: Horizontal pan fix v1 — explicit
  `ScrollView(.vertical)`, `.scrollBounceBehavior(.basedOnSize, axes:
  .horizontal)`, `.frame(maxWidth: .infinity)` on inner VStack.
  Insufficient on iOS 17+ — pan still possible, fixed in (39).
- **Build (38)**: Live Activity FEATURE SCRAPPED. Andy's call after
  seeing it on device: "i can't clear it, not sure i like it. i see
  this island with number 8 and im confused, wtf is this." Removed
  MDSWidgets target + folder, LiveActivityManager, the
  live_activity:start branch in PushManager, the Test Live Activity
  Settings button, the live_activity:start key from the backend
  payload. Kept MorningDigestAttributes.swift only so the cleanup
  hook can call `Activity<MorningDigestAttributes>.activities` ->
  `.end(.immediate)` on next launch and dismiss any pills lingering
  from earlier builds.
- **Build (39)**: Horizontal pan fix v2 —
  `.containerRelativeFrame(.horizontal)` on the inner VStack locks it
  to the exact scroll-container width. The `.frame(maxWidth: .infinity)`
  used in (37) wasn't enough on iOS 17+ ScrollView. Tab "Search" → "Home"
  with house icon. Audio bar persistence (above).
- **Build (40)**: Splash animation. Cream M scales 0.85 → 1.0 + opacity
  0 → 1, accent radial bloom expands behind, "KNOWLEDGE BASE" mono
  kicker resolves with letterspacing easing 8 → 1.2. ~0.95s, then
  parent cross-fades (0.35s) to LoginView/ContentView. Per-process so
  it plays only on cold launch.

#### C. Backend (`mds-ai-bot`)

- **`/api/tts` (commit `89398f1`)** — ElevenLabs proxy. POST
  {text, voice_id?} → audio/mpeg. Server-side proxy so the xi-api-key
  stays out of the iOS binary. Auth-gated. In-process cache keyed on
  (sha1(text), voice_id) with 1h TTL — re-Listens are free. 1500-char
  ceiling (~$0.45 per Listen).
- **`/api/today` fallback rework (commit `341b06b`)** — was
  today → yesterday only. Andy hit it on 2026-05-07 when latest data
  was 2026-05-05. New `_fetch_latest_nonempty_digests()` queries AT
  sorted by date desc, returns the most-recent date's records. Covers
  weekends, holidays, batch lag. iOS Brief always shows the freshest
  available data with the kicker correctly labeled to that date.
- **Full-name enrichment (commit `dccdb41`)** —
  `_members_first_name_index()` builds `{first_name_lower: full_name}`
  from AT Members where exactly one matched member has that first name.
  228 unambiguous mappings on first run. `_enrich_full_names(text)`
  replaces standalone first names in tl_dr / summary / notable_members
  / Today TLDR. Lookahead `(?!\s+last_first_word)` prevents
  "Brandon Himmel" → "Brandon Himmel Himmel" in already-correct text.
  Common first names with multiple owners (Brandon, Daniel, Jonathan)
  stay as-is to avoid wrong attribution.
- **`links_shared` formatter (commit `f16e2de`)** —
  `_format_links_shared(text)` injects `\n\n` after each URL using a
  boundary heuristic of URL → CapitalLetter+lowercase. n8n's Claude
  output was one wall of text where URLs absorbed the leading word of
  the next title. iOS already renders the field as AttributedString
  markdown so paragraph breaks turn into per-entry layout immediately.
- **Push payload cleanup (commit `cc3107d`)** — removed
  `live_activity: "start"` and `content-available: 1` from
  `/api/admin/push/today` payload after Andy scrapped Live Activity.

Render env vars added (this session):
- `ELEVENLABS_API_KEY` (Andy's starter tier, 40k chars/mo)
- `ELEVENLABS_VOICE_ID` = `UgBBYS2sOqTuMpoF3BR0` (Mark — Natural Conversations)
- `ELEVENLABS_MODEL_ID` = `eleven_turbo_v2_5`

#### D. Settings expansion

- **Notifications section** gains an in-app push toggle. iOS-level
  permission stays granted; the toggle hits POST/DELETE /api/devices
  to enable/disable the device row. Lets the user pause pushes without
  revoking the OS permission.
- **New Listen section**: Voice (read-only "Mark · ElevenLabs"), Default
  speed picker, Autoplay-next toggle.
- New `ListenSettings` ObservableObject (UserDefaults-backed).

### Decisions made

- **Live Activity removed entirely.** Andy's user test made the case:
  one-shot "morning digest is ready" event is what push notifications
  are FOR. Live Activities shine for ongoing/changing data (timer,
  Uber, sports, audio playback). The morning-digest LA was confusing
  ("8" = channel count looked like an unread badge), couldn't be
  cleared by the user, and added Dynamic Island clutter without info.
  Pushed this point to Andy with three options (kill / improve /
  repurpose for audio); he picked kill. Push handles the case fine.
- **Listen voice → ElevenLabs Mark.** AVSpeechSynthesizer's quality is
  too low for editorial digest reading. Mark (UgBBYS2sOqTuMpoF3BR0) is
  Andy's pick from the ElevenLabs voice library. Voice ID hardcoded on
  iOS (not env-driven) after stale-iOS-cache served George once.
- **Splash animation kept under 1 second.** Andy asked "can you do
  a cool animation when launching" — curiosity, not a hard ask.
  Editorial vibe (cream M + warm accent bloom + mono kicker) over
  bouncy/flashy. Per-process so it doesn't replay on auth flips.
- **Full-name enrichment on read, not at n8n source.** n8n is Andy's
  separate workflow; bot has direct access to AT and the digest endpoint.
  Cheaper iteration to fix at /api/digests + /api/today than to edit
  the n8n Claude prompt + provide member context. Ambiguous first
  names stay as-is; we don't pick the most-active match (risks wrong
  attribution).
- **Audio bar lifted to app level (build 39).** Persistent across
  navigation. SpeechController as @StateObject in MDSKnowledgeBaseApp,
  injected to ContentView + DigestDetailView via env-object.
- **Lock-screen reliability moves applied incrementally** without
  Andy seeing it work yet. Each retry built on the last:
  (35) added artwork + media type + beginReceivingRemoteControlEvents,
  (39) moved setup to app startup + dropped .duckOthers. Status TBD —
  Andy's most recent screenshot pre-(39) still missing the widget.
- **Bumped version on every iOS commit, no exceptions.** Iron rule
  from the handoff. Andy archived (29) twice → Apple rejected →
  bumped to (30) with no other code changes. Two duplicate-identifier
  archives is a pattern that bites every time the rule lapses.

### Files / modules touched

`mds-ai-bot`:
- `apns.py` — unchanged from build (27)
- `web.py` — +210 lines: tts endpoint, members enrichment helpers,
  links_shared formatter, /api/today fallback, push payload cleanup
- `requirements.txt` — unchanged (httpx[http2] + pyjwt[crypto] from 27)
- `SESSION_LOG.md` — this entry

`mds-ios-app` (`design-system-trial`):
- `MDSKnowledgeBaseApp.swift` — splash overlay, app-level
  SpeechController, stale-LA cleanup, env-object injection
- `ContentView.swift` — persistent AudioPlayerBar, env-object plumbing
- `Storage/SpeechController.swift` — full rewrite (twice — build 32 +
  39): two-mode player, MPNowPlayingInfoCenter, MPRemoteCommandCenter,
  rate persistence, autoplay callback, eager session+commands setup
- `Storage/ListenSettings.swift` — NEW: pushDelivery + rate + autoplay
- `Network/BotClient.swift` — `tts(text:voiceId:)`, `unregisterDevice`,
  `updateLiveActivityToken` (kept; ineffective until LA returns)
- `Push/PushManager.swift` — auto-prompt after login, LA branch removed
- `Push/MorningDigestAttributes.swift` — kept for cleanup-hook reference
- `Push/LiveActivityManager.swift` — DELETED
- `MDSWidgets/` — DELETED (target + folder)
- `Views/SplashView.swift` — NEW
- `Views/AudioPlayerBar.swift` — NEW
- `Views/EmptyStateView.swift` — Brief always-render, removed archive
- `Views/DigestDetailView.swift` — prev/next nav, audio bar wiring,
  scroll-to-top, autoplay, lock-screen callbacks, scroll constraint,
  timezone fix
- `Views/DigestsView.swift` — pendingDigestId nav, timezone fix
- `Views/SettingsView.swift` — Listen section, push toggle, LA test
  row added then removed
- `Views/SourceCardView.swift` — two-line row layout, dedup
- `Models/Today.swift` — `fallback_date` consumption
- `DesignSystem/Glass/GlassTabBar.swift` — Search → Home, house icon
- `Info.plist` (via project.yml) — UIBackgroundModes += audio,
  NSSupportsLiveActivities=true (kept for cleanup), aps-environment
- `project.yml` — version bumps 0.5.0 (27) → 0.6.3 (40), MDSWidgets
  target removed
- New iOS Devices Airtable table (created via Meta API in build 27,
  unchanged): `tblz80VMR7kqxfnnz`

### QA / Verification

**Backend:**
- ✅ `web.py` parses cleanly across every commit.
- ✅ `_format_links_shared` smoke-tested on Andy's actual messy data
  → 8 entries each on its own line.
- ✅ `_members_first_name_index()` smoke-tested on live AT base —
  228 unambiguous first names. Sample: Jacob → Jacob Sufrin (replaced),
  Brandon → ambiguous (preserved), Brandon Himmel → intact (lookahead
  works).
- ✅ ElevenLabs JWT-less auth (xi-api-key header) works against /v1/voices
  and /v1/text-to-speech endpoints. Subscription = starter, 40k chars/mo.
- ✅ `/api/today` returns 7 channels of 2026-05-05 data with synthesized
  cross-channel TLDR after fallback fix.
- ✅ `/api/tts` returns 401 unauthenticated as designed.
- ✅ Render redeploys all completed (varying 1-2min cache hits to
  10min for pip-dep adds).

**iOS:**
- ✅ Every build (28) → (40) compiled clean for iPhone 17 Pro sim.
- ✅ Sim-installed + screenshot-validated each build's launch path
  (LoginView intact across all 13 builds — no regressions on the
  unauthenticated path).
- ✅ Splash sim-validated mid-animation (M faintly visible at t=0.4s)
  + end-state (LoginView fully rendered).
- ⏳ Andy's real-device validation: lock screen Now Playing still TBD
  as of build (37) feedback. Builds (38) → (40) have layered fixes;
  awaiting next archive.

### Known issues / broken things

- **Lock-screen Now Playing still unconfirmed on real device.** Build
  (39) added the eager-init + drop-duckOthers fixes. If still missing
  after (40), next attempt: try `.default` mode instead of
  `.spokenAudio`, or check whether the AVAudioSession is conflicting
  with Apple Music / Spotify routing.
- **Multi-Brandon ambiguity.** "Brandon" maps to multiple members so
  enrichment skips it. If Andy wants this resolved, options: use chat
  membership context to disambiguate (Brandon Himmel is in MDS TikTok,
  Brandon X is in MDS Resellers — match the digest's chat). Not
  blocking, but worth a follow-up.
- **n8n integration for `/api/admin/push/today` not wired yet.** The
  endpoint is live + tested (sent: 2 / failed: 0 to Andy's two devices).
  But the morning batch in n8n doesn't yet hit it. One HTTP-request node
  away. ADMIN_PUSH_SECRET still in `/tmp/admin_push_secret.txt` on
  Andy's mac, needs to land in n8n credential store.
- **Source retrieval ranking** issue Andy flagged on build (28) ("for
  GPT vs Claude, irrelevant videos at top, relevant ones at bottom") —
  `query.py` problem, not iOS. Untouched this session. Likely the
  speaker pre-filter from the prior session is over-biasing toward
  chunks that mention "Claude" the speaker over "Claude" the model.
- **Settings → Listen Voice picker is read-only.** Showing "Mark ·
  ElevenLabs" — no UI to swap voices yet. Future work if Andy wants
  to pick a different voice.

### Test environment state

- **Render service:** `srv-d6kf5j56ubrc73ee8sag` — currently live on
  `f16e2de` (links_shared formatter). Auto-deploy ON.
- **APNs config:** `APNS_KEY_ID = FRPX4SRPQC`, `APNS_TEAM_ID =
  M523QN9PMJ`, `APNS_BUNDLE_ID = com.mds.knowledgebase`,
  `APNS_USE_SANDBOX = false`, `.p8` at
  `/Users/Born/Downloads/AuthKey_FRPX4SRPQC.p8`.
- **ElevenLabs:** API key + voice ID `UgBBYS2sOqTuMpoF3BR0` (Mark) +
  model `eleven_turbo_v2_5` in Render env.
- **Admin push secret:** `/tmp/admin_push_secret.txt` (32-byte hex);
  also in Render as `ADMIN_PUSH_SECRET`. Move to n8n when wiring the
  daily fan-out webhook.
- **iOS:** `mds-ios-app/design-system-trial` HEAD = `e755418` (build 40).
  Andy testing builds 37-40 in TestFlight as they process.
- **Reviewer creds:** unchanged. `appstore-reviewer@mds.co` + `837363`.
- **Working tree:** mds-ai-bot main clean post-`f16e2de`. mds-ios-app
  design-system-trial clean post-`e755418`.

### Open questions for next session

- **Lock-screen Now Playing** — does it appear on (39) or (40)?
  Andy's first real device test will tell. If still missing, try
  `.default` mode + investigate audio-route conflicts.
- **Andy's reaction to splash animation.** Too long? Too subtle? Wants
  it on every launch or only first launch?
- **Source retrieval ranking** for the GPT vs Claude case — needs a
  `query.py` debugging session with `verbose=True` to inspect chunk
  rankings.
- **Multi-Brandon ambiguity** — disambiguate by chat membership
  context, or accept the limitation?
- **n8n wiring** for the morning-digest fan-out webhook. Andy hasn't
  asked yet but the endpoint is ready.

### Next steps (specific, actionable, in priority order)

1. **Andy archives + uploads (40) to TestFlight.** Bumped 0.6.3 (40).
   Walk through the 14-item test checklist sent in the session
   (splash, audio persistence, button order, lock screen, horizontal
   pan locked, date correct, Brief populated, links readable, full
   names enriched, LA pill auto-dismissed).
2. **If lock-screen Now Playing still missing on (40)**, try
   `.default` audio session mode (instead of `.spokenAudio`) +
   investigate route conflicts.
3. **Fix multi-Brandon ambiguity** (optional, depends on Andy's
   call) — chat-membership-aware first-name resolution.
4. **Wire n8n → /api/admin/push/today** as the last hop in the
   morning WA digest workflow. Move ADMIN_PUSH_SECRET from /tmp
   into n8n credentials.
5. **Source retrieval ranking** for the GPT vs Claude case — bring
   `query.py` debug session back.
6. **Decide:** merge `design-system-trial` to main once Andy's
   happy. Currently main is build (12); the trial branch is at
   build (40), 28 builds of design + features.

### Deferred (not for next session unless Andy says)

- Editorial Georgia sign-in retry (still waiting on sim-keyboard
  harness).
- Per-tier permission filtering (Phase 2).
- Resend key rotation — Andy declined.
- App Store review submission — paused.
- Voice picker UI in Settings (currently read-only "Mark · ElevenLabs").

* * *

## 2026-05-06 (later) — Build (27): Push notifications + Live Activity scaffolding

**AI / dev:** Claude Opus 4.7
**Duration:** ~2.5 h continuous
**ClickUp doc:** [2531q-98297](https://app.clickup.com/2264119/docs/2531q-98297/2531q-61237)
**Branches / repos touched:**
- `mds-ai-bot/main` — APNs + device endpoints (`f876810`)
- `mds-ios-app/design-system-trial` — push + Live Activity (`aed6854`)

### What was done

#### A. iOS app (`mds-ios-app/design-system-trial`)

Build (27) ships push notifications end-to-end (auth → registration →
upload → server fan-out) plus the full Live Activity scaffolding for
build (28). Version bumped 0.4.11 (26) → **0.5.0 (27)** on both targets.

New `Push/` module:
- `PushManager.swift` — singleton, requests authorization, registers
  for remote notifications, hex-encodes the device token and POSTs to
  `/api/devices`. Caches the last-uploaded token in UserDefaults so
  duplicate uploads on every foregrounding are skipped.
- `AppDelegateAdapter.swift` — `UIApplicationDelegate` bridge so SwiftUI
  can receive `didRegisterForRemoteNotifications…` and
  `didReceiveRemoteNotification…`. Wired via
  `@UIApplicationDelegateAdaptor`.
- `MorningDigestAttributes.swift` — shared `ActivityAttributes` used
  by both the app and the widget extension.
- `LiveActivityManager.swift` — starts/updates/ends Live Activities,
  observes `Activity.pushTokenUpdates` and forwards each token to
  `BotClient.updateLiveActivityToken` so the server can target this
  specific activity with `apns-push-type: liveactivity` payloads.

`PushManager.didReceiveRemoteNotification` reads
`userInfo["live_activity"] == "start"` and starts the Live Activity
with the channels/messages/caption from the push payload (build 28
hook is now in place).

`SettingsView` gains a Notifications section: shows On/Off based on
`UNAuthorizationStatus`, surfaces the system permission prompt on
first opt-in (`.notDetermined` → `requestAuthorization`), deep-links
to iOS Settings via `UIApplication.openSettingsURLString` for
re-enabling after `.denied` or for fine-tuning post-grant.

`BotClient` gets two new methods:
- `registerDevice(token:)` — POSTs hex token + bundle/version/build
  to `/api/devices`.
- `updateLiveActivityToken(activityId:pushToken:date:)` — POSTs the
  per-activity push token to `/api/devices/live-activity`.

New `MDSWidgets/` app-extension target (`com.mds.knowledgebase.widgets`):
- `MDSWidgetsBundle.swift` — `WidgetBundle` entry.
- `MorningDigestLiveActivity.swift` — Lock screen + Dynamic Island
  (compact leading/trailing, expanded leading/trailing/bottom, minimal).
  Hand-tinted with the warm-orange KB tokens because widget extensions
  can't import the app's `DesignSystem` module without a separate shared
  framework target — drift risk is low (one file, one screen).

Entitlements + Info.plist:
- New `MDSKnowledgeBase.entitlements` — `aps-environment: production`
  (TestFlight target; sim uses `simctl push` not real APNs).
- `Info.plist` — `UIBackgroundModes: [remote-notification]` +
  `NSSupportsLiveActivities: true`.

`project.yml`:
- Versions bumped on both targets.
- New `MDSWidgets` target type `app-extension`.
- Shared file: `MorningDigestAttributes.swift` listed under both
  targets' sources.
- Main app declares `MDSWidgets` as a dependency so archives bundle
  the extension.

#### B. Backend (`mds-ai-bot`)

New `apns.py` (~150 lines):
- `APNsClient` with token-based JWT auth (ES256, signed with the .p8
  contents from env), HTTP/2 send via `httpx[http2]`, 50-min cached
  provider token (Apple allows 60).
- `send(...)` accepts `push_type` so the same client handles `alert`
  / `liveactivity` / `background` payloads.
- Auto-disables device records on terminal Apple errors (400 / 410
  with reason `BadDeviceToken` / `Unregistered` /
  `DeviceTokenNotForTopic`).

New endpoints in `web.py`:
- `POST /api/devices` — auth-required; upserts the (token) row in the
  Devices table with email + bundle / version / build. Idempotent on
  the hex token.
- `DELETE /api/devices` — auth-required; soft-disables a single
  device by `?token=<hex>` or every device for the calling email.
- `POST /api/devices/live-activity` — auth-required; stores the
  per-Activity push token + activity id on the most-recent device
  record for the user.
- `POST /api/admin/push/today` — `X-Admin-Secret` gated. Pulls today's
  digest TL;DR (same path as `/api/today`, but bypasses the 1h cache),
  builds an APNs alert payload with `aps.alert.{title,subtitle,body}`
  + custom keys (`today_date`, `n_channels`, `n_messages`,
  `live_activity: "start"`), iterates over enabled iOS devices, sends
  each one a push, and reports `{sent, failed, errors[]}`. n8n hits
  this when the morning batch finishes.

Airtable:
- New table **`iOS Devices`** (`tblz80VMR7kqxfnnz`) in the shared base
  `appT9TVZWhv7io4CN`. Fields: `token` (primary), `email`, `platform`,
  `bundle_id`, `app_version`, `app_build`, `enabled`, `last_seen`,
  `live_activity_token`, `live_activity_id`, `last_error_status`,
  `last_error_reason`. Created via the Meta API.

Render env vars set (via API):
- `APNS_AUTH_KEY` — full `.p8` contents (multi-line env var)
- `APNS_KEY_ID` — `FRPX4SRPQC`
- `APNS_TEAM_ID` — `M523QN9PMJ`
- `APNS_BUNDLE_ID` — `com.mds.knowledgebase`
- `APNS_USE_SANDBOX` — `false`
- `ADMIN_PUSH_SECRET` — 32-byte hex (saved to `/tmp/admin_push_secret.txt`
  on Andy's mac for now; **needs to be moved into n8n's HTTP-request
  node config when wiring the post-batch webhook**).

`requirements.txt`:
- `+httpx[http2]>=0.27.0`
- `+pyjwt[crypto]>=2.8.0`

### Decisions made

- **Token-based JWT auth, not certificate auth.** The .p8 key approach
  is what Apple recommends for new projects and what the new dev-portal
  key flow generates. One key works for both Sandbox & Production
  environments (chose "Sandbox & Production" + "Team Scoped (All Topics)"
  in the dev portal).
- **`aps-environment: production`** in the entitlement, not development.
  The app is TestFlight-targeted; real device tokens issued under
  TestFlight are production-environment tokens. Sim testing uses
  `simctl push` which doesn't go through Apple's APNs at all, so the
  entitlement value doesn't matter there.
- **Token storage in Airtable, not a separate Render KV / Postgres.**
  Operations consistency with the rest of the bot's state. Volume is
  trivially small (<100 devices for the foreseeable future).
- **`POST /api/admin/push/today` is admin-secret gated, not user-token
  gated.** n8n would need to refresh a session token and we don't want
  to grant n8n the standard user-auth flow. A fixed
  `X-Admin-Secret: $ADMIN_PUSH_SECRET` is simpler and scoped to one
  endpoint.
- **Live Activity scaffolding shipped with build 27** rather than waiting
  for build 28. The widget extension target is non-trivial to add and
  testing it requires a real device; getting the scaffolding into TestFlight
  early lets Andy validate the Lock-Screen / Dynamic-Island rendering
  while we still have the trial branch open. The push receive path
  triggers `LiveActivityManager.start` from a `live_activity == "start"`
  custom key, so a single regular push starts the LA — no separate flow
  needed for build 28.
- **Widget extension theme is hand-tinted with KB tokens, not imported
  from `DesignSystem/`.** Importing across an app-extension boundary
  requires a shared framework target. For one screen with five colors
  and three font calls, hand-coding is fine.
- **`ADMIN_PUSH_SECRET` lives in env, not hard-coded.** Per the safety
  rule about never committing secrets. The secret is in `/tmp/` for
  this session — Andy / n8n setup is the next step to move it into
  the n8n credential store.

### Files / modules touched

`mds-ai-bot`:
- `apns.py` (NEW, ~150 lines)
- `web.py` — +470 lines (devices + admin push routes, Optional + json
  imports)
- `requirements.txt` — +2 lines

`mds-ios-app` (`design-system-trial`):
- `MDSKnowledgeBase/Push/` (NEW): `PushManager.swift`,
  `AppDelegateAdapter.swift`, `MorningDigestAttributes.swift`,
  `LiveActivityManager.swift`
- `MDSKnowledgeBase/MDSKnowledgeBase.entitlements` (NEW)
- `MDSKnowledgeBase/MDSKnowledgeBaseApp.swift` — `@UIApplicationDelegateAdaptor`,
  `pushManager.bootstrapIfAuthorized()` after auth.
- `MDSKnowledgeBase/Network/BotClient.swift` — `registerDevice` +
  `updateLiveActivityToken`.
- `MDSKnowledgeBase/Views/SettingsView.swift` — Notifications section
  with permission state machine + system-settings deep link.
- `MDSKnowledgeBase/Info.plist` — `UIBackgroundModes`,
  `NSSupportsLiveActivities`.
- `MDSWidgets/` (NEW): `MDSWidgetsBundle.swift`,
  `MorningDigestLiveActivity.swift`, `Info.plist`
- `project.yml` — version bump, MDSWidgets target, entitlements ref.
- `MDSKnowledgeBase.xcodeproj/project.pbxproj` — regenerated.

### QA / Verification

**Backend:**
- ✅ `web.py` + `apns.py` parse cleanly (`python3 -m ast`).
- ✅ All new function symbols present.
- ✅ APNs JWT signs ES256 against the real .p8 key (length 200 chars).
- ✅ Apple APNs accepted the JWT — rejected a fake test token (`0x40`)
  with HTTP 400 `BadDeviceToken`. That's the **expected** response that
  proves auth + connection work end-to-end.
- ⏳ Render redeploy in flight at session-end (`dep-d7tspj5ckfvc73fmtkt0`,
  `f8768104`). Build longer than usual because of the new pip deps
  (`pyjwt[crypto]` brings cryptography, `httpx[http2]` brings h2 +
  hyperframe + hpack). Should land in ~5–10 min total.

**iOS:**
- ✅ Both targets build clean for iPhone 17 Pro sim
  (`xcodebuild Debug CODE_SIGNING_ALLOWED=NO`).
- ✅ Sim install + launch reaches `LoginView` identically to (26)
  (screenshot at `/tmp/mds-launch-2.png`).
- ✅ `simctl push` accepts the test payload at
  `/tmp/test_push_payload.json` — delivered to the bundle id
  (foreground app suppresses the alert by default which is expected;
  `didReceiveRemoteNotification` would still fire on a backgrounded
  app).
- ⏳ Settings Notifications section visual layout NOT sim-verified
  (sim sign-in requires keyboard input that needs assistive-access
  permission this session doesn't have). Change is additive and
  follows the same pattern as existing sections — risk is layout-only,
  bounded.

### Known issues / broken things

- **Settings Notifications section visual layout unverified in sim.**
  Pure additive change; if Andy sees a layout bug on real device,
  fix is bounded to the new `notificationsSection` computed property.
- **Live Activity rendering on real Dynamic Island NOT verified.**
  Sim supports it but real device is the canonical surface. First
  test path: TestFlight (27) → opt into push → trigger fan-out from
  `/api/admin/push/today` with the manual curl (see below) → watch
  the activity start on lock screen + Dynamic Island.
- **n8n integration NOT wired yet.** The post-batch fan-out is one
  HTTP-request node away from the existing WA-digest workflow:
  `POST https://mds-ai-bot.onrender.com/api/admin/push/today` with
  header `X-Admin-Secret: <env var>`. Once the Render deploy lands and
  Andy's TestFlight token is registered, this is the last hop.
- **Live Activity end timing not yet decided.** Currently the activity
  ends when iOS dismisses it (default policy) or when explicitly
  ended via `endCurrent`. Could add: end on app foreground OR after
  N hours via `staleDate`. Defer until Andy sees one in the wild.

### Test environment state

- **Render service:** `srv-d6kf5j56ubrc73ee8sag` — deploy in flight at
  session-end on `f876810`. Auto-deploy ON.
- **Reviewer creds:** unchanged.
- **APNs config:** `APNS_KEY_ID = FRPX4SRPQC`, `APNS_TEAM_ID =
  M523QN9PMJ`, `APNS_BUNDLE_ID = com.mds.knowledgebase`,
  `APNS_USE_SANDBOX = false`, key file at
  `/Users/Born/Downloads/AuthKey_FRPX4SRPQC.p8` on Andy's mac (move
  to a permanent secure location at convenience).
- **Admin push secret:** ephemeral copy at
  `/tmp/admin_push_secret.txt` on this mac. Set in Render as
  `ADMIN_PUSH_SECRET`. Move to n8n credential store when wiring the
  webhook.
- **Sim push test payload:** `/tmp/test_push_payload.json`.
- **iOS build artifacts:**
  `/Users/Born/Library/Developer/Xcode/DerivedData/MDSKnowledgeBase-…/Build/Products/Debug-iphonesimulator/MDS Knowledge Base.app`
- **iPhone 17 Pro sim:** `7AE15820-62F4-47DF-B972-E0C31BEC5D89`

### Open questions for next session

- **Andy's reaction to (27) on real device.** Notifications toggle
  visible in Settings? Permission prompt cosmetic OK? Push delivers
  on lock screen? Dynamic Island animates correctly? Any of these
  could trigger a small follow-up.
- **n8n wiring cadence.** Once a TestFlight token registers, do we
  add the HTTP-request node to the existing WA-digest workflow today
  or wait for Andy to validate the receive side first?
- **Live Activity dismissal policy.** Default vs. explicit-on-foreground
  vs. staleDate-based. Real-device feel will tell us.
- **Sim keyboard test harness still deferred.** Same ask as last
  session — needed for the Settings Notifications visual sign-off
  AND for a future LoginView retry.

### Next steps (specific, actionable, in priority order)

1. **Andy archives + uploads (27) to TestFlight** — bump number
   `27`, version `0.5.0`. Same steps as (26).
2. **First device test:** open the app, log in, Settings → Notifications
   → turn on. Verify a row lands in the AT iOS Devices table within
   ~5 sec.
3. **Manual fan-out test:** once a token is in AT, curl from local
   ```
   curl -X POST https://mds-ai-bot.onrender.com/api/admin/push/today \
     -H "X-Admin-Secret: $(cat /tmp/admin_push_secret.txt)" \
     -H "Content-Type: application/json" -d '{}'
   ```
   should return `{sent: 1, …}`. Lock-screen alert appears within
   seconds.
4. **Live Activity smoke test:** the same curl above includes
   `live_activity: "start"` in the payload — should kick off a Lock
   Screen / Dynamic Island activity on the real device.
5. **Wire n8n** once (3) + (4) work. Add an HTTP-request node at the
   end of the WA-digest pipeline:
   `POST mds-ai-bot.onrender.com/api/admin/push/today` with
   `X-Admin-Secret: <secret>` header. No body required.
6. **Move .p8 file** out of `~/Downloads/` to a longer-term location
   (e.g. 1Password). Render has the contents; the local file is a
   safety net.
7. **Build (28)** — the only remaining piece is the Live Activity
   *update* push (apns-push-type=liveactivity targeting the LA push
   token) so the server can update the activity content while it's
   running. Ship as a separate small endpoint
   `POST /api/admin/push/live-activity` if useful.

### Deferred (not for next session unless Andy says)

- Settings Notifications section visual sign-off (deferred until either
  device test or sim-keyboard harness lands).
- Editorial Georgia sign-in retry (still waiting on sim-keyboard
  harness).
- Source-recall improvements from the prior session's dynamic-suite
  findings.
- Per-tier permission filtering (Phase 2).
- Resend key rotation — Andy declined.
- App Store review submission — paused.

* * *

## 2026-05-06 — Dockerfile cache + iOS design-system trial + Today TL;DR + 7 home features

**AI / dev:** Claude Opus 4.7
**Duration:** ~14 hours across one long session
**ClickUp doc:** [2531q-98297](https://app.clickup.com/2264119/docs/2531q-98297/2531q-61237)
**Branches / repos touched:**
- `mds-ai-bot/main` — Dockerfile + `/api/today`
- `mds-ios-app/design-system-trial` — entire UI overhaul (still on the trial branch, NOT merged to main)

**Design system reference** (full token + component + screen + rules + lessons learned, written so a fresh session can implement against the system without re-reading the original handoff zip):
- Repo: `/Users/Born/mds-ios-app/DESIGN_SYSTEM.md`
- ClickUp: [Page 13 — Design System Reference](https://app.clickup.com/2264119/docs/2531q-98297/2531q-61597)

### What was done

Three loosely-connected pieces of work in one session:

#### A. Backend (`mds-ai-bot`)

- `Dockerfile` rework (`4b478fc`): split the embed step from the app-code COPY so backend-only commits stop triggering a 30-40 min re-embed. New layer order — `requirements` → `config.py` + `ingest.py` → `data/` → `RUN ingest_directory` → `RUN ingest_whatsapp` → `COPY *.py ./`. Verified: a follow-up backend deploy (`fbf70b1`) finished in ~2 min instead of 30+ because only `web.py` changed.
- `/api/today` endpoint (`fbf70b1`): GET, auth-required. Pulls today's digests from Airtable, calls Claude to synthesize a single 2-3 sentence cross-channel TL;DR, returns `{tldr, channels[], date, fallback_date}`. In-process cache 1h. Falls back to yesterday when today has no digests yet (early-morning state).
- `/api/today` filter fix (`efbb8e6`): plain `{date}='YYYY-MM-DD'` returned 0 records because the `date` column is an Airtable Date type, not text. Switched to `IS_SAME({date}, '2026-05-06', 'day')`. Verified locally — formula returns 10 records vs 0 with the broken version.

#### B. iOS app (`mds-ios-app`) — entire design-system trial

The trial branch (`design-system-trial`) now spans builds (13) → (26) on TestFlight. Builds (13)-(15) experimented with a blue Liquid Glass system; builds (16)+ replaced it with the warm-orange editorial system per Andy's `MDS AI (3).zip` design handoff. New tokens (`KBColor` warm-dark + cream + `#E76A2B` orange, Editorial Georgia ramp at 24/28/38pt, 4-pt spacing, KBRadius 12-24-pill), new glass primitive (LiquidGlass + GlassChip + GlassDropdown + GlassTabBar + AsteriskMark), new components (KBSectionLabel, KBListRow, KBSearchField, KBButton + KBIconButton). All 3 main screens fully refactored (Home, Digests, DigestDetail) plus ChatView shell, AnswerBubble, SourceCard, Settings, History, TypingIndicator. App icon swapped to the v3 set + MDSMark template image asset.

Sign-in screen ate 6 build cycles (21→26) — three different layout bugs: text clipped on the left under keyboard pressure, M logo pushed top-right under different padding combos, hero/subhead truncated to one line under vertical compression. Eventually reverted to main's proven ScrollView + VStack structure with KB tokens applied (commit `0aa148d`, build 25). Editorial Georgia hero deferred — comes back when there's a faster sim-validation loop and a way to test keyboard-up state in the sim before shipping.

Build (26) shipped seven home / detail features in one go (commit `cca77b8`):

1. **scenePhase auto-refresh** — every foregrounding refreshes DigestsStore + TodayStore.
2. **Today section on home** — synthesized cross-channel TL;DR card above "Suggested for you", per-channel quick links seed search queries.
3. **Read / unread digest dots** — small orange dot left of unread chat names in DigestsView, vanishes when DigestDetailView marks read.
4. **TTS Listen pill** — `AVSpeechSynthesizer` reads chat name + TL;DR + key insights, no backend.
5. **WhatsApp source badge → green** (`#25D366`) — explicit deviation from the single-accent design rule per Andy's request.
6. **Settings → Storage section** — sums UserDefaults bytes for app-owned keys, formats via ByteCountFormatter, Clear cache button preserves history + login.
7. **Pull-to-refresh** — already wired, verified no regression.

Three new persistence modules (`ReadStateStore`, `TodayStore`, `SpeechController`) + new `Today` model + new app-entry env-object injections.

#### C. Workflow change

After three rounds of pushing visually-unverified UI changes broke the same login screen in three different ways, switched to **build-for-sim → install → screenshot → verify → bump → commit** as the iron rule for any UI change. Burned ~3 hours of iteration on bugs the simulator would have caught in 30 seconds. Worth the discipline.

Also: **every commit on this branch must bump the build number**, no exceptions, even when paused mid-edit. Two duplicate `0.4.6 (21)` archives ended up in Andy's Organizer because I edited code without bumping while user was "checking something." Apple rejects duplicate identifiers — never again.

### Decisions made

- **Dockerfile rework first.** Without it, every backend iteration this session would have been a 30+ min wait. Paid back the first time `web.py` changed in isolation.
- **`/api/today` synthesis is server-side, not iOS-side.** Bills Claude once per hour per server cache, not once per app open per device. Keeps mobile Anthropic key out of the binary.
- **Server cache 1h, client cache 30 min.** Server cache absorbs spikes (multiple devices opening at the same time after the morning batch lands); client cache absorbs same-session re-renders without re-network.
- **Today section uses Editorial Georgia (`KBFont.editorial()` = Georgia 24).** Reads as a magazine pull-quote even on the home screen — exactly what the v3 design system intended for "reading-titled moments."
- **Read-state lives in UserDefaults, not the backend.** Per-device. If Andy wants cross-device sync later, Airtable + a `/api/read-state` endpoint would do it. Not worth the complexity for now.
- **TTS is iOS-native (`AVSpeechSynthesizer`), not a Polly/ElevenLabs backend.** Free, offline, ships today.
- **WhatsApp green is an explicit design exception.** Documented inline in `SourceCardView.whatsappBadge`. Single-accent rule still holds for everything else.
- **LoginView reverted to main's structure.** After 5 attempts at the editorial Georgia sign-in produced 5 different layout bugs, took the L. Build (25) ships main's ScrollView + VStack layout retinted with KB tokens — it works.

### Files / modules touched

`mds-ai-bot`:
- `Dockerfile` — split COPY layers (~10 lines)
- `web.py` — added `/api/today` endpoint + helpers (~140 lines)

`mds-ios-app` (design-system-trial branch only):
- All `DesignSystem/` — 17 files, total replacement
- All 3 main screens (Home/Empty/Digests/Detail) — full refactor
- `LoginView.swift` — 6 iterations, settled on retinted-main version
- `Models/Today.swift` (new), `Storage/TodayStore.swift` (new), `Storage/ReadStateStore.swift` (new), `Storage/SpeechController.swift` (new)
- `ContentView.swift` — manual tab switcher + GlassTabBar overlay + scenePhase
- `MDSKnowledgeBaseApp.swift` — env-object injections
- `SettingsView.swift` — Storage section + Clear cache
- `SourceCardView.swift` — green WhatsApp badge
- `Assets.xcassets/AppIcon.appiconset/` — v3 icon set
- `Assets.xcassets/MDSMark.imageset/` — template image
- `scripts/generate_app_icon.py` — Python PIL fallback generator
- `project.yml` — version bumps 0.2.7 (12) → 0.4.11 (26)

### QA / Verification

**Backend:**
- Dockerfile cache locality verified — `fbf70b1` deployed in ~2 min vs ~30 prior
- `/api/today` returns today's records, falls back to yesterday on empty
- Date filter `IS_SAME({date}, 'YYYY-MM-DD', 'day')` verified vs broken `=` formula
- Static prod suite was last run at 17/17 in commit `3c3978b` — no retest this session (no query/retrieval changes)

**iOS:**
- LoginView (build 25) sim-validated before shipping — hero, subhead, EMAIL field, button, helper, footer all in correct gutter
- Build (26) compiles clean — no sim-screenshot of authenticated views, those are layout-additive on already-verified screens
- All 11 trial builds (12 → 26) live on TestFlight; revert path = `git checkout main` + install build (12)

### Known issues / broken things

- **Editorial Georgia sign-in still missing.** Reverted to retinted-main layout in (25). The fancy hero version has been deferred indefinitely — needs a faster sim-validation loop AND a way to test keyboard-up state in the sim before another attempt.
- **Push notifications NOT YET shipped.** Promised for build (27). Needs Apple Push key from Andy's App Store Connect, device-token endpoint, hook into the WA-digest pipeline, APNs send code.
- **Dynamic Island Live Activity NOT YET shipped.** Promised for build (28). Depends on push being in place first.
- **Email TL;DR not started.** Andy clarified mid-session this isn't from emails — it's the existing per-channel digests synthesized into one cross-channel TL;DR, which is now `/api/today`. Email ingestion is no longer in scope.
- **WhatsApp green = single-accent rule violation.** Documented in `SourceCardView.whatsappBadge`. If we ever want strict design conformance back, drop the dedicated badge and use `KBColor.glassPill` like the Speaker badge.
- **No simulator way to test keyboard-up state.** Need an `osascript` helper or Xcode UI-test harness so future LoginView-style bugs (only visible with keyboard up) can be caught pre-ship.

### Test environment state

- **Render service:** `srv-d6kf5j56ubrc73ee8sag` — currently live on `efbb8e6`. Auto-deploy ON. Dockerfile cache means typical backend deploys are now ~1-2 min.
- **Reviewer creds:** `appstore-reviewer@mds.co` + fixed code `837363` (Render env vars `REVIEWER_EMAIL` / `REVIEWER_FIXED_CODE`).
- **Admin emails:** `andy.verdy1@gmail.com,tangowithw@gmail.com` (Render env var `ADMIN_EMAILS`).
- **iOS:** `mds-ios-app/design-system-trial` branch HEAD = `cca77b8` (build 26). Eleven trial builds (12 → 26) currently in TestFlight. Andy is about to upload (26) and test.
- **Working tree:** mds-ai-bot main is clean post-`efbb8e6`. mds-ios-app design-system-trial is clean post-`cca77b8`.

### Open questions for next session

- **Andy's reaction to (26).** Today section, read dots, Listen pill, green WhatsApp, Settings storage all need eyes-on validation. Likely 1-2 small follow-ups based on feedback.
- **Push notifications scoping.** Apple Push key creation is Andy-side (App Store Connect → Keys → +). Backend storage of device tokens — new Airtable table or just a Render KV/Redis? Likely Airtable for consistency. APNs send via `requests` to Apple's HTTP/2 endpoint — straightforward, but the JWT signing is fiddly.
- **Live Activity scope.** Just a "morning digests ready" payload? Or also "while reading: insight 3 of 5" progress?
- **Editorial Georgia sign-in retry.** Worth coming back to once we have a sim-keyboard test harness.

### Next steps (specific, actionable, in priority order)

1. **Andy archives + uploads (26) to TestFlight** — bump number is 26, version 0.4.11. Iterate on whatever feedback comes back.
2. **Build (27): Push notifications.** Andy creates Apple Push key in App Store Connect (~2 min). iOS adds `registerForRemoteNotifications` + posts device token to a new `/api/devices` endpoint. Backend stores tokens in a new Airtable table, hooks into the WA-digest pipeline so finishing the morning batch triggers a fan-out push to all subscribed devices.
3. **Build (28): Live Activity.** ActivityKit attributes + Live Activity views in iOS. Backend sends Live Activity APNs payload (separate flow from regular pushes) when batch finishes. Dynamic Island shows *"3 new digests · 2 chats you follow"*.
4. **Sim keyboard-test helper.** Small AppleScript / shell helper to focus the email field, type, and screenshot — so the next LoginView attempt doesn't re-burn 5 build cycles on bugs only visible with keyboard up.
5. **Decide:** merge `design-system-trial` to main once Andy's happy with (26)+. Currently main is build (12); the trial branch carries 14 build's worth of design-system + features.

### Deferred (not for next session unless Andy says)

- Editorial Georgia sign-in retry (after sim keyboard test harness exists)
- Source-recall improvements from the prior session's dynamic-suite findings (#4 Brandon Himmel API stack, #10 Kat's Meeting Spectrum Five) — neither is the no-info cap, both are retrieval-ranking
- Per-tier permission filtering (Phase 2)
- Resend key rotation — Andy declined again (still in git history)
- App Store review submission — paused, waiting for Andy's go-ahead

* * *

## 2026-05-05 (latest) — No-info cap refined → 17/17 static, 8/10 dynamic

**AI / dev:** Claude Opus 4.7
**Duration:** ~50 min (~40 of those waiting for one Render rebuild)
**ClickUp doc:** [2531q-98297](https://app.clickup.com/2264119/docs/2531q-98297/2531q-61237)
**Branch / PR:** `mds-ai-bot/main` only

### What was done

`query.py` (`3c3978b`): Refined the post-Claude no-info cap so it only fires for genuine declines, not for substantive answers that happen to open with a humble caveat.

Old behavior: any occurrence of "I don't have enough information" / "doesn't contain specific information" / etc. anywhere in Claude's answer → strip sources, cap confidence at 0.18. This was the dominant remaining failure mode for A4, E1 (static suite) and 4 of 10 dynamic-suite first-run cases (Ryan Hogan, Leslie Eisen, Michael Zenga, Brandon Himmel) — Claude found the right chunks but hedged on a specific detail, and the bot punished the whole answer.

New behavior:

*   `is_genuine_decline = hedge_in_first_200_chars AND len(answer) < 250` → strip sources, cap conf at 0.18 (true declines, e.g. E3 "capital of France" at 242 chars).
*   Hedge present but answer is longer / hedge buried → keep sources. Boost floor capped at **0.45** instead of 0.65 so the UI doesn't oversell a hedged answer.
*   No hedge → boost floor 0.65 (unchanged).

### Decisions made

*   **AND logic, not OR.** I sketched OR ("hedge at start OR answer short") to the user, but discovered while implementing that E1's hedge IS in the first 200 chars — OR would still strip E1. AND fixes E1 because E1's answer is 573 chars (well over 250). Stayed honest about the change.
*   **Two-tier boost (0.45 / 0.65), not single 0.65.** A long answer with "I don't have enough info" still shouldn't get the full "high relevance" badge. 0.45 ("moderate") is honest about the hedge.
*   **Kept the existing hedge-phrase list unchanged.** Adding more phrases would catch more hedges but also produce more false positives. The current 7 phrases are the ones Claude actually emits.

### Files / modules touched

*   `query.py` — added `hedge_at_start`, `is_short_answer`, `is_genuine_decline` variables. Replaced the bare `if has_no_info` strip with an `if is_genuine_decline` strip. Updated the substantive-answer boost to use a two-tier floor (0.45 hedged / 0.65 clean). ~22 lines net change.
*   `SESSION_LOG.md` — this entry.

### QA / Verification

Local sanity test (against local vectorstore before deploy) — all 5 trouble/regression cases as predicted:

| Query                    | Conf before | Conf after | Sources before | Sources after | Test result |
| ------------------------ | ----------- | ---------- | -------------- | ------------- | ----------- |
| E1 "How meny IG scrappers" | 0.18      | **0.45**   | 0              | **9**         | now passes (≥0.30) |
| A4 "tldr Ramon"          | 0.18        | **0.45**   | 0              | **9**         | now passes (≥0.30) |
| E3 "capital of France"   | 0.17        | 0.17       | 0              | 0             | still declines (242<250) ✓ |
| C1 "Josh Hadley TikTok"  | 0.65        | 0.65       | 5              | 5             | unchanged ✓ |
| D2 "IG strategies"       | 0.18        | **0.47**   | 0              | **7**         | flips back to pass |

Prod after `3c3978b`:

*   **Static 17-query suite: 17 / 17 passed.** Up from 15 / 17. First clean sweep of the suite. Categories: A 5/5, B 4/4, C 3/3, D 2/2, E 3/3.
*   **Dynamic suite (n=10, seed=42): 8 / 10 passed.** Up from 6 / 10 on the same seed. The 2 remaining fails are now both `source_match` failures (bot returned reasonable sources at conf 0.45 but didn't include the specific sampled chunk) — that's a recall problem, not the no-info cap. Different / harder failure mode.

### Known issues / broken things

*   **Recall ceiling — bot doesn't always return the most-specific chunk.** Dynamic suite #4 (Brandon Himmel API stack across vector DB) and #10 (Kat's Meeting "Spectrum Five" keyword) returned plausible sources at conf 0.45 but the specific sampled chunk wasn't among them. Possibilities: the source chunk's similarity score is being beaten by adjacent chunks in the same digest, or the WA chat name match is dominating. Worth investigating but not the same systemic issue as the cap.
*   **Dockerfile re-embeds 9879 chunks on every** **`.py`** **change.** Hit again this session (~40 min for `3c3978b`). Still on the next-steps list.
*   Resend key still in git history — Andy declined to rotate.

### Test environment state

*   **Render service:** `srv-d6kf5j56ubrc73ee8sag` — currently live on `3c3978b`.
*   Reviewer creds, admin emails, API keys: unchanged.
*   `/tmp` test scripts unchanged.
*   `tests/dynamic_search_quality.py` runs from project root.

### Open questions for next session

*   **Source-recall improvements?** The dynamic suite's source_match check is now the dominant failure mode. Possible levers: increase TOP_K for the side that has the answer, weight recent chunks higher, or change WA digest chunking to keep semantically related Q+A together.
*   **Dockerfile cache rework still pending.** Worth doing to get deploy time down to ~1 min; would unblock more iteration speed.

### Next steps (specific, actionable, in priority order)

1. **Restructure Dockerfile** so the embed step's cache key only depends on `data/` + `ingest.py`, not all `.py` files. Will cut deploy from ~40 min to ~1 min for any change in `query.py` / `auth.py` / `web.py`. Prior session's Next Step #3.
2. **Wire dynamic suite to a Render Cron Job** — nightly 20 chunks (10 WA + 10 TR), output to a Slack webhook or CU comment. Prior session's Next Step #2.
3. **Investigate source-recall failures.** Pick 3-5 specific dynamic-suite source-match failures, run them locally with `verbose=True` in `query.py` to see chunk rankings, identify whether the right chunk was in the top-K but ranked below others or wasn't retrieved at all.
4. **Mirror SESSION_LOG entries to BOTH this file and CU Page 12** when next session ends.

### Deferred (not for next session unless Andy says)

*   Per-tier permission filtering (Phase 2)
*   Push notifications (Phase 2)
*   iOS dark-mode visual verification
*   Resend key rotation — Andy declined
*   App Store review submission — paused

* * *

## 2026-05-05 (later) — Speaker pre-filter + dynamic search-quality suite shipped

**AI / dev:** Claude Opus 4.7
**Duration:** ~1.5 h (mostly waiting on Render rebuilds — 30+ min/each because the Dockerfile re-embeds 9879 chunks on any `.py` change)
**ClickUp doc:** [2531q-98297](https://app.clickup.com/2264119/docs/2531q-98297/2531q-61237)
**Branch / PR:** `mds-ai-bot/main` only (no iOS changes)

### What was done

Backend (`mds-ai-bot`):

- `config.py` (`195fc03`): Lowered `CONFIDENCE_THRESHOLD` 0.15 → 0.12 per the previous session's "Next steps" #1.
- `query.py` (`7008790` + `c2db5f7`): Speaker-name pre-filter for transcript retrieval. Builds (lazily, cached at module load) a name-phrase → raw-speaker-list index by walking transcript metadata once and running `format_display_name` + a name-extraction regex. When a query contains an indexed name, the transcript half of retrieval uses `filter={"speaker": {"$in": [...]}}` instead of the type-based filter. Multi-person sources like "Mogul Call with Hasan & Dave" index both individual names AND the joined tail.
- `tests/dynamic_search_quality.py` (`22e6189`): The dynamic search-quality suite designed in CU Page 09. Samples random WA + transcript chunks from the local vectorstore, generates one chunk-specific question per chunk via Claude, hits prod `/api/ask`, and checks (a) source match by `source_id` / `speaker`, (b) confidence ≥ 0.30, (c) ≥ 2 distinctive words shared between chunk and answer. "Tester-broken" cases (Claude can't generate a unique question) skipped from denominator.

Render config: no env-var changes this round.

Tests run:

- **Static 17-query suite against prod after `c2db5f7`: 15 / 17 passed.** Up from prior baseline 14 / 17. **C1 ("Josh Hadley TikTok") fixed** — went from 0 sources / conf 0.18 to 5 sources / conf 0.65 with substantive answer. Categories: A 4/5, B 4/4, C **3/3 ↑**, D 2/2, E 2/3.
- **Dynamic suite first run against new prod (10 chunks, seed 42): 6 / 10 passed.** All 4 failures are the same `has_no_info` cap pattern — Claude finds the right person/topic, includes a hedge phrase, gets confidence capped at 0.18, sources stripped. This is the systemic issue Page 09 named "the substantive-answer boost in `query.py`."
- E1 ("How meny IG scrappers does Ramon use?") still fails, despite the threshold lowering. **Root cause was misidentified in the prior session's Next steps:** the dominant constraint isn't pre-Claude `CONFIDENCE_THRESHOLD`, it's the post-Claude no-info cap at `query.py:386` (`avg_confidence = min(avg_confidence, 0.18)` when Claude's answer contains "I don't have enough information"). The threshold change was harmless (E3 still correctly declines at conf 0.17) but didn't deliver the predicted +1.

ClickUp doc updates:

- **Page 09 (Search-Quality Test Plan)** — to be appended with new prod result + dynamic-suite first-run baseline.
- **Page 11 (Known Issues)** — add post-Claude no-info cap as the dominant remaining failure mode for A4 / E1 / dynamic-suite "Ryan Hogan", "Leslie Eisen", "Michael Zenga", "Brandon Himmel" cases.
- **Page 12 (SESSION_LOG)** — entry mirrored to this file.

### Decisions made

- **Don't revert the `CONFIDENCE_THRESHOLD` 0.12 change.** It's conceptually correct — borderline-relevant queries should be allowed past the pre-Claude gate — and harmless (no test passing before now fails because of it). The remaining E1 failure is a separate issue (Claude's hedging + the post-Claude cap), not the threshold.
- **Speaker pre-filter scope: transcripts only.** WA chunks have empty `speaker`, so `$in` against a list of transcript raws naturally won't match WA chunks. The implicit `type != whatsapp` constraint is preserved. WA half of retrieval is unchanged.
- **Stop list for speaker-name extraction is conservative.** Words like "Mogul", "Call", "Channel", "Chapter", "Council", "Trading", "Logistics" are excluded so labels like "Rockies Chapter Monthly Call" or "AI Channel monthly Call" don't get indexed as fake names.
- **Single-word person names are NOT indexed.** "Brian" / "Ramon" / "Dave" alone would generate too many false positives. Multi-person tails like "Hasan & Dave" are indexed as a phrase but individual fragments aren't. The static suite (Brian's AI TikTok, Ramon's IG scrapes) doesn't regress because none of those queries name a single transcript speaker.
- **Dynamic suite calls prod, not local vectorstore** (open question from prior session). Prod is more realistic — exercises auth, deploy state, the real LLM call path. Cost is acceptable (~$0.005/chunk × 10 chunks = ~$0.05/run).
- **Tester-broken cases are skipped from denominator, not counted as fail** (other open question). Conflating "Claude couldn't write a verifiable question" with "bot returned wrong answer" would muddy the metric.
- **Multi-person speaker fix shipped same day** as the original speaker pre-filter (`c2db5f7` follow-up to `7008790`). Discovered during dynamic-suite smoke test when a question quoting "Mogul Call with Hasan & Dave" returned 0 sources.
- **Render build cache shortcut: cancel an in-progress redundant build** when a follow-up commit is pushed mid-build. Saved ~30 min by canceling `dep-d7spomfaqgkc73a7fhug` (commit `7008790`) once `c2db5f7` had queued — the new build hit cache from the partial earlier build.

### Files / modules touched

- `config.py` — `CONFIDENCE_THRESHOLD` 0.15 → 0.12 (1 line).
- `query.py` — added `_SPEAKER_STOP_WORDS`, `_SPEAKER_NAME_INDEX`, `_extract_name_candidates()`, `_get_speaker_name_index()`, `_detect_speakers_in_query()` helpers. Modified `ask()` retrieval block to use `$in` on speaker when matches found. ~115 lines added.
- `tests/dynamic_search_quality.py` — new file, ~420 lines.
- `SESSION_LOG.md` — this entry.

### QA / Verification

- ✓ Local prototype: speaker index built from 9879 transcript chunks → 152 unique name phrases (148 before multi-person fix). Josh Hadley resolves to 5 raw speakers totaling 231 chunks. Hasan & Dave resolves to 1 raw speaker.
- ✓ Local C1 test (before deploy): conf 0.65, 5 sources, all Josh Hadley.
- ✓ Local false-positive check: 16 of 17 static-suite queries return `_detect_speakers_in_query() == []`. Only C1 triggers the filter.
- ✓ Static prod suite after `c2db5f7`: 15/17 (was 14/17).
- ✓ Dynamic suite end-to-end: works on prod, produces machine-readable summary, surfaces real failure modes.
- ✓ E3 ("capital of France") still correctly returns "I don't have enough info" at conf 0.17 < 0.18 cap — threshold change didn't break the safety gate.

### Known issues / broken things

- **Dominant remaining failure mode: post-Claude no-info cap.** When Claude's answer contains any phrase from `_no_info_phrases` (e.g. "I don't have enough information", "doesn't contain specific information"), `query.py:380` strips sources and caps confidence at 0.18 — even when the answer is mostly substantive. Hits A4, E1 in static suite and 4/10 in the dynamic suite first run. **Possible fix:** only apply the cap when the no-info phrase appears in the FIRST 200 chars OR when the answer is short overall. Not implemented this session.
- **Dockerfile re-embeds 9879 chunks on every `.py` change.** Build time ~25-30 min per deploy, almost all spent in Step 1 / Step 2 ingest. The previous deploy (`2562631`, docs-only) hit cache and finished in 52 s. Worth restructuring Dockerfile so the embed step's cache key is more granular — but out of scope here.
- A4 (vague "tldr of what Ramon said") — still requires `/api/summarize-source` route per prior session's Page 09. Not in scope.
- Resend key still in git history — Andy declined to rotate.

### Test environment state

- **Render service:** `srv-d6kf5j56ubrc73ee8sag` — currently live on `c2db5f7` (Step 1 + Step 2 + multi-person fix). `22e6189` is on main but only adds `tests/dynamic_search_quality.py`, which the Dockerfile doesn't COPY, so no redeploy needed for the test file.
- **Reviewer creds, admin emails, API keys:** unchanged from prior session.
- **Build-cache discovery:** the queued `c2db5f7` build inherited cache layers from the canceled `7008790` build, finishing in ~7 min instead of ~30. Useful when the next session pushes a small follow-up.
- **`/tmp` test scripts** that survive between sessions: `/tmp/search_test_suite.py` (local), `/tmp/search_test_prod.py` (HTTP).
- **Repo test scripts:** `tests/dynamic_search_quality.py` runs from project root with venv activated. Default 5 + 5 chunks; pass `--n-wa N --n-tr N --recent-days N --json out.json --seed N` to customize.

### Open questions for next session

- **Should we patch the post-Claude no-info cap?** A small, scoped change — e.g. only apply when the phrase appears within the first 200 chars OR the answer is < 250 chars total — would unblock A4 / E1 / the dynamic-suite Brandon Himmel + Ryan Hogan + Leslie Eisen + Michael Zenga cases. Risk: too permissive, fewer "I don't know" responses. Worth a small experiment.
- **Schedule the dynamic suite as a Render Cron Job?** Or run from a local cron pointed at prod? Page 09 mentioned both. Cost ~$3/mo for nightly 20-chunk runs.
- **Should the dynamic suite log "borderline" cases** (passed source-match but failed conf, or vice versa) so we can see directional improvements between deploys, not just pass/fail?

### Next steps (specific, actionable, in priority order)

1. **Refine the post-Claude no-info cap in `query.py:380`.** Apply the cap only when the no-info phrase appears in the first 200 chars of `answer_text` OR when `len(answer_text) < 250`. Re-run static + dynamic suites; expect A4 / E1 + several dynamic-suite cases to flip.
2. **Wire dynamic suite to a Render Cron Job** — nightly 20 chunks (10 WA + 10 TR), output to a Slack webhook or CU comment. Page 09 already specifies this. ~30 lines of glue + a Render dashboard cron entry.
3. **Restructure `Dockerfile`** so the COPY layer above the embed RUN excludes only the `.py` files that don't affect ingestion (`auth.py`, `web.py`, `email_sender.py`, etc.). Cuts deploy time from ~30 min to ~1 min for backend-only changes.
4. **Mirror SESSION_LOG entries to BOTH this file and CU Page 12** when next session ends.

### Deferred (not for next session unless Andy says)

- A4 vague speaker-only queries — needs route through `/api/summarize-source`.
- Per-tier permission filtering (Phase 2).
- Push notifications (Phase 2).
- iOS dark-mode visual verification.
- Resend key rotation — Andy declined.
- App Store review submission — paused.

* * *

## 2026-05-05 (late) — Status gate + Apple-review bypass + dynamic-test design

**AI / dev:** Claude Opus 4.7
**Duration:** ~2 h on top of an already-long preceding session
**ClickUp doc:** [2531q-98297](https://app.clickup.com/2264119/docs/2531q-98297/2531q-61237)
**Branch / PR:** `mds-ai-bot/main` only (no iOS changes this round)

### What was done

Backend (`mds-ai-bot`):

- `auth.py` rewrite (`b835573`): `is_member_email()` now queries the source
  MDS member directory base (`appou5JVr0WIrioWS`, table `tblfwOSROSHfuYUxv`)
  by `Preferred Email`. Allowed only if `AT Database Status` is one of
  `Current Member`, `New Member`, `Pending Group Entrance`. Verified ~720
  valid members.
- `auth.py` reviewer bypass: env vars `REVIEWER_EMAIL` + `REVIEWER_FIXED_CODE`.
  `is_member_email()` returns True when email matches `REVIEWER_EMAIL`.
  `consume_code()` returns True when email + code both match. Used by Apple
  App Store reviewer.
- `web.py`: `/api/auth/request-code` skips Resend entirely for the reviewer
  email.

Render config:

- Set env vars via API: `REVIEWER_EMAIL=appstore-reviewer@mds.co` +
  `REVIEWER_FIXED_CODE=837363`.
- Triggered fresh deploy after env-var change (key gotcha — see Known Issues
  #13).

Tests run:

- 17-query suite against PROD (commit `b835573`): **14 / 17 passed.**
  Categories: A 4/5, B 4/4, C 2/3, D 2/2, E 2/3. **B3 fixed** vs prior
  baseline. The 3 remaining failures (A4, C1, E1) are known weaknesses, not
  regressions.
- Reviewer bypass smoke test: request-code → 200 (no Resend), verify with
  code 837363 → 30-day token, /api/ask with that token → conf 0.65 + WA
  citation.

ClickUp doc updates:

- **Page 09 (Search-Quality Test Plan)** — added prod 14/17 result + dynamic-
  suite design proposal.
- **Page 11 (Known Issues)** — added entries #13 (env-var refresh gotcha) +
  #12 (status gate) + new operational gotcha at top about always Manual
  Deploy after env-var change.
- **Page 12 (SESSION_LOG)** — entry mirrored to this file.

### Decisions made

- **Status gate uses 3 explicit values from `AT Database Status`** (Current
  Member, New Member, Pending Group Entrance). Records with blank status are
  blocked. Andy's record is blank but he's covered by `ADMIN_EMAILS` bypass.
  Real members with blank status need data backfill — Andy accepts that
  scope.
- **Apple reviewer pattern: dedicated email + fixed-code env vars.** Reviewer
  doesn't need to receive emails. Fixed code is 6 digits (matches iOS UI).
  30-day token issued like a normal session.
- **Acceptable to commit Resend key value to private repo (Andy's call).**
  Repo is private. Andy chose not to rotate this session.
- **Dynamic search-quality suite proposed but NOT yet built.** The static 17-
  query suite is a regression suite, not a discovery suite. Design documented
  in Page 09.

### Files / modules touched

- `auth.py` — added `SOURCE_BASE_ID`/`SOURCE_MEMBERS_TABLE`/
  `SOURCE_STATUS_FIELD`/`SOURCE_EMAIL_FIELD` constants + `ALLOWED_MEMBERSHIP_STATUSES`,
  rewrote `is_member_email()`, added reviewer bypass to `consume_code()`
- `web.py` — added reviewer bypass to `api_auth_request_code`
- `.env.example` — added the 4 missing env vars (AIRTABLE_PAT, RESEND_API_KEY,
  EMAIL_FROM, ADMIN_EMAILS, REVIEWER_EMAIL, REVIEWER_FIXED_CODE)
- `SESSION_LOG.md` — created (this file)

### QA / Verification

**Backend smoke test run this session:**

- ✓ /api/auth/request-code with non-member email → 403
- ✓ /api/auth/request-code with reviewer email → 200, no Resend call
- ✓ /api/auth/verify with reviewer email + fixed code 837363 → token issued
- ✓ /api/ask with reviewer token → answer + WA citation (conf 0.65)
- ✓ Full 17-query suite against prod → 14/17 pass

**Regression checks needed next session:**

- Confirm A4/C1/E1 fixes (when implemented) don't regress the 14 currently-
  passing queries.

### Known issues / broken things

- **3 search-quality failures still open**, all known weaknesses with
  documented fix paths in Page 09:
  - A4 "tldr of what Ramon said" — needs speaker-only-query routing through
    summarize-source
  - C1 "Josh Hadley TikTok" — needs speaker-name pre-filter for transcript
    chunks
  - E1 typo "How meny IG scrappers" — borderline, fix by lowering
    `CONFIDENCE_THRESHOLD` 0.15→0.12
- **Resend key in git history** — Andy chose not to rotate this session.
- **Today's digests not in Airtable until end-of-day** — sister-project
  pipeline timing, not a bot bug.

### Test environment state

- **Render service:** `srv-d6kf5j56ubrc73ee8sag` — currently live on
  `b835573`. Auto-deploy ON (commit trigger from main).
- **Reviewer creds:** `REVIEWER_EMAIL=appstore-reviewer@mds.co`,
  `REVIEWER_FIXED_CODE=837363` (set in Render dashboard env vars).
- **Test admin emails:** `andy.verdy1@gmail.com`, `tangowithw@gmail.com` (in
  `ADMIN_EMAILS`).
- **Render API key:** in `/Users/Born/mds-ai-bot/.env` as `RENDER_API_KEY`.
- **Airtable PAT:** in `/Users/Born/mds-ai-bot/.env` as `AIRTABLE_PAT` —
  works on both `appT9TVZWhv7io4CN` (auth/digests) AND `appou5JVr0WIrioWS`
  (membership directory).
- **iOS:** TestFlight build `0.2.7 (12)` uploaded. Not yet submitted for App
  Store review (Andy paused).
- **`/tmp` test scripts** that survive between sessions:
  `/tmp/search_test_suite.py` (local), `/tmp/search_test_prod.py` (HTTP),
  `/tmp/auth_and_reingest.py` (admin reingest helper).

### Open questions for next session

- Should the dynamic suite call the prod `/api/ask` or query the local
  vectorstore directly? (Prod is more realistic; local is faster + cheaper.)
- Should "tester-broken" cases (Claude couldn't generate a verifiable
  question) be silently skipped or counted as fail?

### Next steps (specific, actionable, in priority order)

1. **Lower `CONFIDENCE_THRESHOLD` from 0.15 → 0.12 in `config.py:28`.** One-
   line change. Re-run prod 17-query suite; expect E1 to flip to pass
   (15/17). Fastest win.
2. **Add speaker-name pre-filter to `query.py:262`** for queries that mention
   a known speaker. When query contains a name that matches a `speaker`
   metadata value, run the transcript half of retrieval with
   `filter={"speaker": {"$contains": name}}` instead of just
   `{"type": {"$ne": "whatsapp"}}`. ~30 lines. Re-run suite; expect C1 to
   flip to pass.
3. **Build dynamic search-quality suite** as designed in Page 09. ~150 lines
   Python. Save as `mds-ai-bot/tests/dynamic_search_quality.py`. Run once
   locally to validate, then schedule via Render Cron Job (or local cron
   pointed at prod).
4. **Mirror SESSION_LOG entries to BOTH this file and CU Page 12** when next
   session ends.

### Deferred (not for next session unless Andy says)

- A4 vague speaker-only queries — needs route through `/api/summarize-source`
- Per-tier permission filtering (Phase 2)
- Push notifications (Phase 2)
- iOS dark-mode visual verification — Andy needs to eyeball build (12) and
  report
- Resend key rotation — Andy declined

* * *

## 2026-05-05 (earlier) — UI batch + clickable WA + Digests filter + per-source-type retrieval + doc restructure

(Earlier this session — see git log `014f494 → 33179f8 → 4573aca → 11a6f96 →
b835573` and iOS `fafdaeb → 11a6f96`. Doc restructured into 12 pages.)

* * *

## 2026-05-04 — OTP fix + WA index fix + iOS Digests-tab fix + Render config

(See commit history `071bab7 → ad62cac → 096ecd3` and iOS
`292ee5c → 95f9cad`.)

* * *

## 2026-05-03 — Initial spec + audit + iOS shipped

(Earliest session, before this SESSION_LOG existed. Repo `mds-ai-bot` was
on `3f3d508` hotfix and iOS at build `0.2.4 (9)`.)
