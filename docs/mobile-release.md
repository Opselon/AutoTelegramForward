# Mobile releases

## APK / AAB install

Every GitHub release ships per-ABI APKs plus a Play Store bundle (built by
the `mobile-android` release job; integrity data in `release/manifest.json`
under `android.artifacts`):

| Asset                                    | ABI / use              |
|------------------------------------------|------------------------|
| `AutoTelegramForward-<ver>-android-arm64.apk`  | arm64-v8a (most phones) |
| `AutoTelegramForward-<ver>-android-armv7.apk`  | armeabi-v7a (old devices) |
| `AutoTelegramForward-<ver>-android-x86_64.apk` | x86_64 (emulators)     |
| `AutoTelegramForward-<ver>-android-bundle.aab` | Play Store upload      |

Production release builds require four GitHub Actions secrets: `ANDROID_KEYSTORE_BASE64`
(base64-encoded release keystore), `ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`,
and `ANDROID_KEY_PASSWORD`. The workflow fails closed when any is absent; it never
silently signs a published release with the ephemeral runner debug key.

Install: download the APK matching your device, enable install-from-unknown-
sources if prompted, and open the file. Verify first if you like:

```bash
sha256sum -c SHA256SUMS.txt   # from the release assets
```

## Simulator build (no Apple signing needed)

Maintainers and forks can validate the iOS target without any certificates —
this is exactly what the `ios-simulator` release job does on `macos-latest`:

```bash
cd mobile
flutter test
flutter build ios --simulator --no-codesign
```

The output `build/ios/iphonesimulator/Runner.app` runs in the Xcode
simulator (`xcrun simctl install booted <path>/Runner.app`). It cannot be
installed on physical devices — that requires the signed IPA below.

## Fork default endpoint (`mobile/assets/cfg/env.json`)

The app's compiled-in default server URL ships as an asset so a fork can
repoint it without touching Dart code:

```json
{
  "apiBaseUrl": "https://atf.opselon.com",
  "apiBaseUrlDev": "http://10.0.2.2:8080",
  "minSupportedApiVersion": "1.0.0",
  "appVersion": "1.5.0",
  ...
}
```

Fork steps:

1. Edit `mobile/assets/cfg/env.json` → set `apiBaseUrl` to your server
   (public, non-loopback hosts must use `https` — enforced by
   `ServerUrl.tryParse` in `lib/core/env.dart`).
2. Keep `apiBaseUrlDev` as the emulator loopback (`10.0.2.2`) for local
   docker-compose development.
3. Rebuild (`flutter build apk …` / `flutter build ipa …`).

No tokens, API keys, or master secrets belong in this file or anywhere else
in the client — every request uses the caller's own JWT from
`/api/auth/login`.

## Restore-default (point back at your own server)

End users never need to rebuild: open **Settings → Server** (or the server
dialog at login) and enter the server URL, e.g. `https://panel.example.com/`
or `https://192.168.1.10/` with a trusted certificate for LAN testing. Switching servers clears the
current session token (a token minted by one server is meaningless to
another). To return to the fork's compiled-in default, re-enter the URL from
`assets/cfg/env.json` — the app always starts from that value when no
override is stored (`AppState` falls back to `env.defaultBaseUrl`).
