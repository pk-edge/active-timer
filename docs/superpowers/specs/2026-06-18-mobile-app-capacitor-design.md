# TimeTracker Mobile App — Capacitor (Android + iOS) Design

**Date:** 2026-06-18
**Status:** Approved design → ready for implementation planning
**Author:** Engineering (brainstormed with stakeholder)

---

## 1. Goal & Constraints

Turn the existing Next.js admin dashboard into a native cross-platform mobile app (Android + iOS) using Capacitor, with **full feature parity** to the web dashboard, delivered in phases.

**Hard constraints (non-negotiable):**

1. **Do not change existing functionality.** The AWS web deployment must remain byte-for-byte unchanged at runtime. All dashboard/backend edits must be *additive* or *web-transparent*.
2. **Zero runtime environment variables on the mobile app.** The deployment does not inject envs into the running app (see §3). The mobile build must resolve everything from committed, code-reviewed config — mirroring the backend's existing `_ENVIRONMENTS` pattern.
3. **Same architecture, optimized.** Follow the existing repo conventions (Next.js App Router structure, CamelCase file names, the existing API-client + SWR + hooks layering). No forked business logic — the mobile app reuses the *exact* `lib/api.ts`, types, and data hooks.
4. **UX matters.** This is a phone app for admins/managers on the go: bottom-tab navigation, card layouts, pull-to-refresh, native feel, safe areas.

**Primary user:** Admins & managers (monitoring/oversight). Tracked employees ("talent") remain portal-restricted as already implemented.

---

## 2. Architecture Overview

**Strategy:** One codebase. The existing `dashboard/` Next.js app gains (a) a mobile build flavor and (b) a responsive mobile layer. A **new top-level `mobile/` folder** holds Capacitor config and the generated native projects. Web and mobile are produced as **two distinct build artifacts** (required — see §3).

```
active-time/
├── dashboard/                       # existing Next.js app — additive edits only
│   ├── next.config.mjs              # target-aware: standalone (web) | export (mobile)
│   ├── package.json                 # + build:mobile:{prod,staging,dev} scripts
│   └── src/
│       ├── lib/
│       │   ├── api.ts               # BASE becomes target-aware; web branch UNCHANGED
│       │   └── mobileConfig.ts      # NEW — committed API endpoints (mirrors _ENVIRONMENTS)
│       ├── lib/capacitor/           # NEW — native bridges (auth, haptics, push, storage)
│       └── components/mobile/       # NEW — bottom nav, mobile shell, card primitives
└── mobile/                          # NEW — Capacitor project
    ├── capacitor.config.ts          # appId: co.onedge.timetracker, webDir: ../dashboard/out
    ├── package.json                 # capacitor deps + sync/open/run scripts
    ├── android/                     # generated native project
    └── ios/                         # generated native project
```

**Why "wrap the existing UI" (chosen) over a separate rebuilt app:** full parity comes from one codebase with no double-maintenance; every existing page is reachable immediately. The cost — touching the dashboard build config and converting dynamic routes — is bounded and made web-transparent (see §4, §5).

---

## 3. Environment-Free API Resolution (the core of the deployment constraint)

### 3.1 What "envs won't work there" means (confirmed)

- [`dashboard/src/lib/api.ts:13`](../../../dashboard/src/lib/api.ts) — `export const BASE = process.env.NEXT_PUBLIC_API_URL ?? "";`
- `NEXT_PUBLIC_*` values are **statically inlined at `next build` time**, not read at runtime.
- Deployment is **AWS ECS via Bitbucket Pipelines**. The web dashboard runs the Next.js **`output: "standalone"`** Node server. The pipeline bakes the URL with `docker build --build-arg NEXT_PUBLIC_API_URL=https://api.tracker.onedge.co` per branch (staging/master).
- **Therefore:** setting `NEXT_PUBLIC_API_URL` on an ECS task/service/compose `environment:` has **zero effect** — the value is already frozen into the bundle. The dashboard's API base is effectively a compile-time constant.

### 3.2 The mobile strategy — mirror the backend's `_ENVIRONMENTS`

The backend already solves the multi-environment problem without per-env runtime config in [`googleAuth.py:74-81`](../../../backend/app/api/v1/endpoints/googleAuth.py) via a committed `_ENVIRONMENTS` table. The mobile app mirrors this exactly.

**New committed file `dashboard/src/lib/mobileConfig.ts`:**
```ts
// Committed, env-free. Mirrors backend _ENVIRONMENTS (googleAuth.py:74-81).
// The chosen flavor is a COMPILE-TIME constant — never a runtime env.
export const MOBILE_ENVIRONMENTS = {
  production:  { api: "https://api.tracker.onedge.co",          deepLink: "co.onedge.timetracker://auth" },
  staging:     { api: "https://api.tracker.staging.onedge.co",  deepLink: "co.onedge.timetracker://auth" },
  development: { api: "http://localhost:8000",                  deepLink: "co.onedge.timetracker://auth" },
} as const;

export type MobileFlavor = keyof typeof MOBILE_ENVIRONMENTS;
```

**`api.ts` becomes target-aware — the web branch is byte-for-byte identical to today:**
```ts
import { MOBILE_ENVIRONMENTS, type MobileFlavor } from "@/lib/mobileConfig";

const MOBILE_FLAVOR = process.env.NEXT_PUBLIC_MOBILE_FLAVOR as MobileFlavor | undefined;

export const BASE = MOBILE_FLAVOR
  ? MOBILE_ENVIRONMENTS[MOBILE_FLAVOR].api        // mobile: committed table, no runtime env
  : (process.env.NEXT_PUBLIC_API_URL ?? "");      // web: IDENTICAL to today
```

**Flavor selection (no runtime env, no free-form URLs):** one npm script per flavor in `package.json`. The discriminator carries only an enum key, never a URL.
```json
"build:mobile:prod":    "NEXT_PUBLIC_MOBILE_FLAVOR=production  next build",
"build:mobile:staging": "NEXT_PUBLIC_MOBILE_FLAVOR=staging     next build",
"build:mobile:dev":     "NEXT_PUBLIC_MOBILE_FLAVOR=development next build"
```
A wrong/missing flavor can only ever select a **known-good committed endpoint** — never an empty/relative `BASE`. The *absence* of `NEXT_PUBLIC_MOBILE_FLAVOR` is the explicit signal "this is the web build — behave exactly as today," which is what keeps the AWS web build provably unchanged.

### 3.3 Build-time safety assertion (new — also hardens existing web risk)

Today nothing asserts `BASE` is non-empty; a misconfigured build silently degrades to same-origin relative URLs. Add a build-time guard:
- If `NEXT_PUBLIC_MOBILE_FLAVOR` is **set**, `BASE` must resolve to a non-empty absolute URL from the table, else **fail the build**.
- If **unset** (web), preserve today's behavior exactly (no new failure path introduced for web).

### 3.4 Two distinct build artifacts (non-negotiable)

Because `BASE` is build-time-frozen, web and mobile **cannot share a bundle**. Mobile builds with `NEXT_PUBLIC_MOBILE_FLAVOR` + `output: "export"`; web continues `output: "standalone"`. No cached bundle reuse across targets.

### 3.5 `next.config.mjs` becomes target-aware (web object unchanged)

```js
const isMobile = !!process.env.NEXT_PUBLIC_MOBILE_FLAVOR;
const nextConfig = isMobile
  ? { output: "export", images: { unoptimized: true }, trailingSlash: true }
  : { output: "standalone", images: { remotePatterns: [/* unchanged */] } };
```
Screenshots from `*.amazonaws.com` render as unoptimized `<img>` on mobile (the `next/image` optimizer needs the server that a static export does not have).

### 3.6 Cleanup

Delete the dead `NEXT_PUBLIC_GOOGLE_OAUTH_BASE` ARG/ENV from [`dashboard/Dockerfile`](../../../dashboard/Dockerfile) (confirmed unused; misleading for mobile setup).

---

## 4. Dynamic Route Conversion (the only structural edit to existing pages)

Static export cannot pre-render `[id]` routes for arbitrary backend IDs. Convert the three detail routes to query-param routes. Behavior is **identical on web** — same component, same data fetch, only the URL shape changes.

| Before | After |
|---|---|
| `devices/[id]/page.tsx` → `/devices/abc` | `devices/detail/page.tsx` → `/devices/detail?id=abc` |
| `users/[id]/page.tsx` → `/users/abc` | `users/detail/page.tsx` → `/users/detail?id=abc` |
| `groups/[id]/page.tsx` → `/groups/abc` | `groups/detail/page.tsx` → `/groups/detail?id=abc` |

Detail pages read the id via `useSearchParams().get("id")`. All internal links (`/devices/${id}` → `/devices/detail?id=${id}`) updated repo-wide. The `[id]` co-located `_components/` move with their page.

---

## 5. Responsive Mobile Layer

Detect native context via `Capacitor.isNativePlatform()` (cheap, cached).

- **Bottom tab bar** (mobile only) replaces the sidebar: **Home · Devices · Activity · Reports · More**. The "More" sheet holds the long tail (Users, Groups, Roles, Policies, Categories, Audit, Logs, Tech, Team, Overview, Settings, Agents).
- **Tables → card lists** on small screens via a shared `components/mobile/` card primitive; reuse existing data hooks unchanged.
- Safe-area insets (notch/home indicator), ≥44px touch targets, splash screen, status-bar theming, app icon.
- The sidebar still renders at tablet/desktop widths → the web app is visually unchanged.
- Permission gating (`useUser().can(...)`) is reused as-is, so RBAC visibility is identical to web.

---

## 6. Authentication

- **Email/password:** works as-is against `/v1/auth/admin/login`. No change.
- **Google OAuth (mobile, deep-link):**
  - App opens the system browser (`@capacitor/browser`) to `${BASE}/v1/auth/google?origin=co.onedge.timetracker://auth`.
  - Backend carries the origin in its signed state (already does), and on callback redirects to the custom scheme. The app captures it via `@capacitor/app` `appUrlOpen`, extracts the credential, stores it, and enters.
- JWT stored in secure device storage; **biometric (Face/Touch ID / fingerprint)** gate on app re-open.

### 6.1 Backend OAuth changes (additive, web-safe)

In [`googleAuth.py`](../../../backend/app/api/v1/endpoints/googleAuth.py):
1. Append a mobile row to `_ENVIRONMENTS`: `{"dashboard": "co.onedge.timetracker://auth", "api": "https://api.tracker.onedge.co"}` (+ staging). `_allowed_origins()` and `_match_env()` pick it up automatically.
2. Fix `_safe_origin()` (line ~98): its `if not p.scheme or not p.netloc` guard silently drops custom schemes (`co.onedge.timetracker://auth` may parse with empty `netloc`). Relax/normalize for the registered mobile scheme so it survives the allowlist check (existing https origins parse exactly as before).
3. **Separate Google OAuth client (PKCE):** the current confidential **Web** client cannot register a custom-scheme redirect URI. A dedicated **iOS/Android (PKCE, no client secret)** client is required. ⚠️ **Stakeholder action (when prompted):** create this client + register redirect URIs in Google Cloud Console. The implementer will provide the exact URIs.
4. **Token delivery hardening (mobile only):** return the JWT via a one-time code (POST exchange) or URL fragment instead of `?token=` — deep-link query strings leak into OS logs / app-switcher. The web query-string flow is unchanged.

---

## 7. CORS (additive)

[`main.py`](../../../backend/app/main.py): the `allow_origin_regex = r"https://[a-zA-Z0-9\-]+\.onedge\.co"` is https-only and will not match mobile origins. The SSE streams ([`home/page.tsx:37`](../../../dashboard/src/app/(dashboard)/home/page.tsx), [`useDevicesPush.ts:26`](../../../dashboard/src/hooks/useDevicesPush.ts)) run through WebView `fetch`/`EventSource`, so:
- Add `capacitor://localhost`, `https://localhost`, `http://localhost`, `ionic://localhost` to `CORS_ORIGINS`; mirror in `_cors_allow_origin()` so 500 responses keep CORS headers.
- Keep `allow_credentials=True` ⇒ **never** use `"*"`.
- Existing origins/regex untouched.

(If a later decision uses the native Capacitor HTTP plugin — which bypasses CORS — for non-stream calls, CORS is still required for the SSE streams, so we add it regardless.)

---

## 8. Native Enhancements

| Feature | Implementation | Backend? |
|---|---|---|
| Pull-to-refresh + haptics | Touch handler → SWR `mutate`; `@capacitor/haptics` | No |
| Biometric quick-unlock | `capacitor-native-biometric` + secure JWT storage (`@capacitor/preferences` or secure-storage plugin) | No |
| Offline read cache | SWR `provider` persisted to device storage; revalidate on reconnect | No |
| Push notifications | `@capacitor/push-notifications` + FCM (routes to APNs for iOS) | **Yes** (§8.1) |

### 8.1 Push-notification backend (new, isolated)

None exists today (only in-process SSE via [`broadcaster.py`](../../../backend/app/core/broadcaster.py)). Add:
- New model `PushToken(id, employee_id FK, token, platform[ios|android], created_at, last_used, unique(token))` in [`models.py`](../../../backend/app/db/models.py) — the desktop-oriented `Device` model is unsuitable.
- A versioned migration (run via Docker, per project convention).
- Authenticated endpoints `POST /v1/devices/push-token` (upsert) and `DELETE` (unregister on logout), guarded by `get_current_user`, registered in [`router.py`](../../../backend/app/api/v1/router.py).
- An FCM/APNs sender service. Tokens are employee/tenant-scoped, purged on logout and on provider "unregistered" responses.
- Triggers (phase 3): device-offline, pending-user-approval, etc.

---

## 9. Delivery Phases (full parity, shipped incrementally)

Each phase gets its own implementation plan and is independently verifiable.

- **Phase 0 — Foundation:** `mobileConfig.ts` + target-aware `api.ts`/`next.config.mjs` + build-time assertion + mobile npm scripts; dynamic-route conversion; Capacitor scaffold + Android/iOS projects; responsive shell (bottom nav, mobile chrome, safe areas, splash, status bar); email/password auth. **Exit:** installable app on both platforms, all pages reachable, web build provably unchanged.
- **Phase 1 — Auth + core native UX:** Google deep-link OAuth (PKCE client, `_ENVIRONMENTS` row, `_safe_origin` fix, token hardening); biometric unlock + secure storage; pull-to-refresh + haptics; offline read cache.
- **Phase 2 — Mobile polish:** Home, Devices, Activity, Reports, detail pages → card-based, touch-optimized.
- **Phase 3 — Push notifications:** backend `PushToken` + endpoints + FCM/APNs sender + triggers; in-app handling + permission prompt.
- **Phase 4 — Store prep:** icons/splash assets, signing, store listings, release (Play Console + App Store Connect).

---

## 10. Risks & Findings

- 🔴 **Secrets committed to git:** [`.env`](../../../.env) contains a real `GOOGLE_CLIENT_SECRET`, `SETUP_SECRET`, `SEED_PASSWORD`, tracked in the repo. Recommend rotating + git-ignoring (out of scope for this build, but flagged).
- **Silent empty `BASE`:** pre-existing web risk; mitigated by the §3.3 build-time assertion.
- **PKCE mobile OAuth client** is a hard dependency for Google sign-in on mobile (stakeholder Cloud Console action — provided at implementation time).
- **No IaC in repo:** ECS task defs / ALB / Route53 live outside the repo; backend runtime secrets are wired there. Push-notification env (FCM/APNs keys) must be added to the backend ECS task def at runtime (backend *is* runtime-configurable, unlike the frontend).
- **No shared artifact:** web and mobile are two builds; documented to prevent accidental reuse.

---

## 11. What is NOT changing

- The AWS web deployment (Bitbucket pipeline, `output: "standalone"`, `--build-arg NEXT_PUBLIC_API_URL`) — untouched.
- The desktop agent (PyInstaller `.dmg`/`.msi`) — untouched.
- Existing backend endpoints, RBAC, talent portal restriction — untouched (only additive rows/endpoints/origins).
- Business logic — reused, not forked.
