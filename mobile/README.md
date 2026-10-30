# AutoTelegramForward — Mobile (Flutter)

The production mobile client for the AutoTelegramForward forwarding engine,
for Android and iOS. This app is a **pure client** of the existing Golang API:
it owns presentation, local state and platform integration only — never
business logic, storage, or a second backend.

## Architecture

```
lib/
├── main.dart            composition root (env → storage → AppState → app)
├── app.dart             MaterialApp: themes, locale, session routing
├── core/
│   ├── api_client.dart  the one HTTP client: every endpoint, typed models in/out
│   ├── models.dart      DTOs mirroring the Go JSON contract (snake_case)
│   ├── app_state.dart   session + preferences (ChangeNotifier via provider)
│   ├── design_tokens.dart  spacing/radius/type scales + AppPalette (ThemeExtension)
│   ├── theme.dart       Material 3 light & dark themes
│   ├── env.dart         assets/cfg/env.json + server-URL validation
│   └── storage.dart     SecureTokenStore / PreferenceStore abstractions
├── widgets/             shared primitives (buttons, fields, cards, states)
├── features/<area>/     one directory per product area (dashboard, rules,
│                        delivery, filters, logs, auto_reply, …)
└── l10n/                ten locale catalogs with machine-checked 1:1 parity
```

Key seams (everything is injectable, nothing is a singleton):

* `HttpTransport` — the raw HTTP seam. Tests inject canned transports; no test
  ever opens a socket.
* `SecureTokenStore` / `PreferenceStore` — JWT lives in flutter_secure_storage
  (Keystore / Keychain); non-secret prefs in shared_preferences.
* `AppState.buildApiClient({transport})` — the factory every screen uses.

## Backend contract

The Go API under `../api` is the single source of truth. `api_client.dart`
documents its routes against `api/main.go` and `api/internal/handlers/*.go`.
Wire format is **snake_case**; numbers may arrive as strings, so every numeric
read goes through `core/json_cast.dart`. The app pins two-way SemVer
compatibility against `GET /api/version` at connect time and fails closed.

Anything the API does not support is **not** in the UI. No mock data, no fake
success states — a failed read renders as a failure.

## Development

```bash
flutter pub get
flutter test          # 40+ suites, incl. l10n parity and UI-copy gates
flutter analyze
dart format lib test  # CI enforces --set-exit-if-changed
flutter build apk --debug
```

From the repo root, the Python gates that cover this app also run in CI:

```bash
python scripts/audit_mobile_ui_copy.py     # no hardcoded user-facing copy
python scripts/audit_cross_language.py     # 10-locale catalog parity
```

## Conventions (enforced by tests, not by hope)

1. **All user-facing strings** go through `AppMessages`; a missing key fails
   loudly. New keys are added to **all ten** catalogs in the same change
   (`en, fa, ru, zh, pt, uz, id, es, fr, it`; fallback is `en`, never `fa`).
2. **Colors by role** (`AppColors.of(context).danger`), never hex literals; a
   new hex inside a screen is a review failure. WCAG contrast is pinned by
   `test/core/palette_contrast_test.dart`.
3. **Every screen** implements loading / empty / error (+retry) / content, and
   a failed read never renders as zeroes.
4. **Adaptive layout**: compact (<600 dp) bottom bar, medium rail, expanded
   two-pane master-detail; 48 dp minimum touch targets; RTL verified for `fa`.
5. **Mutations are single-shot** (no duplicate submits); reads retry with
   backoff. 401 clears the session and routes to login.

See `../docs/ui-ux-system.md` and `../docs/agent-rules.md` for the full
contracts, and `../docs/mobile-release.md` for signing and release steps.
