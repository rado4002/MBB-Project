# MBB frontend

## Purpose

This application is the browser-session-based interface foundation for the first MBB read-only Inbox. It is separate from the Streamlit dashboard and communicates only with the same-origin browser-authentication API.

## Current F2 scope

F2 includes browser session initialization, sign-in, mandatory password change, protected routing, a minimal authenticated application shell, and read-only account and session information. The `/inbox` route is deliberately an empty foundation and does not complete the Inbox.

## Explicit exclusions

Conversation APIs begin in F3. Message history and limited customer or conversation context begin in F4. F2 includes no business writes, replies, assignment, escalation actions, AI behavior, direct service or database access, or runtime HTTPS proof. Real isolated HTTPS browser validation remains F6.

## Security rules

- API calls use relative same-origin URLs, `credentials: "same-origin"`, and `cache: "no-store"`.
- CSRF values exist only in the in-memory authentication provider and are replaced after session rotation.
- The secure HttpOnly cookie remains authoritative and is never read, written, or deleted by JavaScript.
- Authentication state and credentials are never stored in local storage, session storage, IndexedDB, service workers, or URLs.
- Session expiration removes protected frontend state. Authentication service failure is shown as unavailable, never as proof that the user is anonymous.
- The frontend has no direct access to PostgreSQL, Redis, Celery, Baileys, messaging providers, or other external capabilities.
- Response-header `X-Request-ID` takes precedence over a body request ID; only the safe normalized reference is presented.

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
- `src/api/` is the single typed `fetch` boundary for browser authentication. It applies request security defaults, normalizes E1 and lowercase authentication errors, supports cancellation and throttling metadata, and centrally removes protected state on session expiration.
- `src/components/` and `src/features/` contain only the small shell and screens required by F2. Administrator and Operator share the same shell and the only primary navigation item is Inbox.
- `src/styles/` defines light-mode design tokens and responsive component styling with visible focus and reduced-motion support. It uses system fonts and local inline SVG only.
- Tests use Vitest, jsdom, Testing Library, `user-event`, MSW contract handlers, keyboard-oriented assertions, and axe-core automated accessibility checks. They do not start backend or infrastructure services.
