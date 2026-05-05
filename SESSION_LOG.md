# Session Log

Newest entry at top. Each entry uses the context-handoff template: what was
done, decisions, files touched, QA, known issues, test environment, open
questions, next steps.

The canonical version of this log is mirrored to ClickUp Page 12:
https://app.clickup.com/2264119/docs/2531q-98297/2531q-61477 — keep both in
sync per the context-handoff skill protocol.

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
