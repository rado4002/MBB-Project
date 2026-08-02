# MBB frontend

## Purpose

This application is the browser-session-based interface foundation for the first MBB read-only Inbox. It is separate from the Streamlit dashboard and communicates only with the same-origin browser-authentication API.

## Current F3 scope

F3 retains browser session initialization, sign-in, mandatory password change, protected routing, the authenticated shell, and read-only account/session information. It connects `/inbox` to `GET /api/v1/operator/conversations` for a read-only conversation queue with the E1-supported status, escalation-state, and language filters, background refresh, and memory-only cursor pagination.

## Explicit exclusions

Conversation selection, detail routing, message history, and limited customer or conversation context begin in F4. F3 includes no business writes, replies, assignment, escalation details or actions, search, user-selected sorting, AI behavior, direct service or database access, or runtime HTTPS proof. Real isolated HTTPS browser validation remains F6.

## Security rules

- API calls use relative same-origin URLs, `credentials: "same-origin"`, and `cache: "no-store"`.
- CSRF values exist only in the in-memory authentication provider and are replaced after session rotation.
- The secure HttpOnly cookie remains authoritative and is never read, written, or deleted by JavaScript.
- Authentication state and credentials are never stored in local storage, session storage, IndexedDB, service workers, or URLs.
- Session expiration removes protected frontend state. Authentication service failure is shown as unavailable, never as proof that the user is anonymous.
- The frontend has no direct access to PostgreSQL, Redis, Celery, Baileys, messaging providers, or other external capabilities.
- Response-header `X-Request-ID` takes precedence over a body request ID; only the safe normalized reference is presented.
- Only valid supported filters are serialized into the URL. Opaque cursors, phone values, previews, and other conversation data remain outside URLs and browser persistence.
- The queue renders the backend-masked phone as received, treats customer text as plain text, and replaces non-text previews with safe local media labels.

## Development commands

Use Node 20 or a newer compatible Node release and npm:

```text
npm ci
npm run dev
npm run typecheck
npm run lint
npm run test:run
npm run build
```

`npm run test` starts Vitest in watch mode. All commands run from `frontend/`.

## Architecture

- `src/app/` defines the browser router and the five supported routes: `/login`, `/password-change`, `/inbox`, `/account`, and `/session`.
- `src/auth/` owns an explicit context-and-reducer state machine. Startup always reconstructs identity through `GET /api/v1/auth/session`; protected navigation is not rendered while that request is unresolved.
- `src/api/` is the single typed `fetch` boundary for browser authentication and the minimized E1 queue. It applies request security defaults, normalizes E1 and lowercase authentication errors, supports cancellation and throttling metadata, and centrally removes protected state on session expiration.
- `src/components/` and `src/features/` contain the shared shell, authentication screens, and read-only queue. Administrator and Operator share the same shell and the only primary navigation item is Inbox.
- `src/features/inbox/` normalizes supported URL filters, aborts or ignores stale list requests, preserves rows during manual refresh, keeps cursors in memory, and deduplicates appended pages by `conversation_id`.
- `src/styles/` defines light-mode design tokens and responsive component styling with visible focus and reduced-motion support. It uses system fonts and local inline SVG only.
- Tests use Vitest, jsdom, Testing Library, `user-event`, MSW contract handlers, keyboard-oriented assertions, and axe-core automated accessibility checks. They do not start backend or infrastructure services.

## Dependency audit boundary

The production audit currently flags `GHSA-qwww-vcr4-c8h2` through `react-router@7.18.2`. The reviewed advisory states that it applies only to unstable React Server Component APIs. This frontend is a client-only Vite application using declarative `BrowserRouter` routes; it has no React Server Components, SSR, loaders, actions, or server-action endpoints, so the affected execution path is absent. Reassess this exception before introducing any server-rendering or RSC routing mode, and upgrade when a compatible patched release is available.
