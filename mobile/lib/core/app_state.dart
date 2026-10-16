/// App-wide state: authentication session, server selection, theme, language.
///
/// Uses `provider`'s `ChangeNotifier`. Deliberately no bloc/rx dependency —
/// the state transitions here are simple enough that plain Dart is clearer
/// and needs no extra packages or generated code.
library;

import 'package:flutter/material.dart';
import 'package:http/http.dart' as http;

import 'api_client.dart';
import 'env.dart';
import 'models.dart';
import 'storage.dart';

/// Lifecycle status of the authenticated session.
enum SessionStatus { unknown, unauthenticated, authenticated, expired }

/// Reason the login screen is being shown.
enum LoginReason { fresh, expired, loggedOut, serverChanged }

class AppState extends ChangeNotifier {
  AppState({
    required this.env,
    required this.tokenStore,
    required this.preferenceStore,
    this.transportOverride,
  }) : _serverUrl = env.defaultBaseUrl,
       _status = SessionStatus.unknown,
       _themeMode = ThemeMode.system,
       _locale = const Locale('en');

  final AppEnv env;
  final SecureTokenStore tokenStore;
  final PreferenceStore preferenceStore;

  /// Test seam: a screen that builds its own client ([buildApiClient] with no
  /// explicit transport) still needs to be drivable from a widget test with a
  /// canned server. Null in production, where `ioTransport` is used.
  final HttpTransport? transportOverride;

  String _serverUrl;
  String? _token;
  int _userId = 0;
  String _username = '';
  String _savedUsername = '';
  String _savedPassword = '';
  bool _isAdmin = false;
  SessionStatus _status;
  ThemeMode _themeMode;
  Locale _locale;
  String? _lastError;

  String get serverUrl => _serverUrl;
  String? get authToken => _token;
  int get userId => _userId;
  String get username => _username;
  String get savedUsername => _savedUsername;
  String get savedPassword => _savedPassword;
  bool get isAdmin => _isAdmin;
  SessionStatus get status => _status;
  ThemeMode get themeMode => _themeMode;
  Locale get locale => _locale;
  String? get lastError => _lastError;

  bool get isAuthenticated => _status == SessionStatus.authenticated;

  /// Restores the persisted session at startup. Sets [status] to either
  /// [authenticated] or [unauthenticated]; never throws.
  Future<void> restore() async {
    final creds = await tokenStore.read();
    _savedUsername = creds.username;
    _savedPassword = creds.password;

    final storedLanguage = await preferenceStore.getString(PrefKeys.language);
    final storedTheme = await preferenceStore.getString(PrefKeys.themeMode);
    final storedServer = await preferenceStore.getString(
      PrefKeys.lastServerUrl,
    );

    if (storedLanguage.isNotEmpty) {
      _locale = Locale(storedLanguage);
    }
    if (storedTheme == 'dark') {
      _themeMode = ThemeMode.dark;
    } else if (storedTheme == 'light') {
      _themeMode = ThemeMode.light;
    }
    if (storedServer.isNotEmpty) {
      _serverUrl = storedServer;
    }

    if (creds.isValid) {
      _token = creds.token;
      _userId = creds.userId;
      _username = creds.username;
      _isAdmin = creds.isAdmin;
      _serverUrl = creds.serverUrl;
      _status = SessionStatus.authenticated;
    } else if (creds.username.isNotEmpty &&
        creds.password.isNotEmpty &&
        _serverUrl.isNotEmpty) {
      // Re-login seamlessly with saved credentials so user is not prompted again when app is closed and reopened
      try {
        final client = buildApiClient();
        final session = await client.login(
          username: creds.username,
          password: creds.password,
        );
        _token = session.token;
        _userId = session.userId;
        _username = session.username;
        _isAdmin = session.isAdmin;
        _serverUrl = session.serverUrl;
        _status = SessionStatus.authenticated;
      } catch (_) {
        _token = null;
        _userId = 0;
        _username = creds.username;
        _isAdmin = false;
        _status = creds.token.isNotEmpty
            ? SessionStatus.expired
            : SessionStatus.unauthenticated;
      }
    } else {
      _token = null;
      _userId = 0;
      _username = '';
      _isAdmin = false;
      _status = SessionStatus.unauthenticated;
      if (creds.token.isNotEmpty) {
        await tokenStore.clear();
      }
    }
    notifyListeners();
  }

  void setLocale(Locale locale) {
    _locale = locale;
    preferenceStore.setString(PrefKeys.language, locale.languageCode);
    notifyListeners();
  }

  void setThemeMode(ThemeMode mode) {
    _themeMode = mode;
    preferenceStore.setString(
      PrefKeys.themeMode,
      mode == ThemeMode.dark
          ? 'dark'
          : (mode == ThemeMode.light ? 'light' : 'system'),
    );
    notifyListeners();
  }

  /// Switches the server. Clears the current session: a token minted by one
  /// server is meaningless to another, so the user must authenticate again.
  Future<void> setServerUrl(String rawUrl) async {
    final parsed = ServerUrl.tryParse(rawUrl);
    if (parsed == null) {
      _lastError = 'server_url_invalid';
      notifyListeners();
      return;
    }
    _serverUrl = parsed.normalized;
    await preferenceStore.setString(PrefKeys.lastServerUrl, parsed.normalized);
    await tokenStore.clear();
    _token = null;
    _userId = 0;
    _username = '';
    _isAdmin = false;
    _status = SessionStatus.unauthenticated;
    _lastError = null;
    notifyListeners();
  }

  void _applySession(AuthSession session) {
    _token = session.token;
    _userId = session.userId;
    _username = session.username;
    _isAdmin = session.isAdmin;
    _serverUrl = session.serverUrl;
    _status = SessionStatus.authenticated;
    _lastError = null;
    notifyListeners();
  }

  Future<void> onLoginSuccess(
    AuthSession session, {
    String password = '',
  }) async {
    await preferenceStore.setString(PrefKeys.lastServerUrl, session.serverUrl);
    _savedUsername = session.username;
    if (password.isNotEmpty) {
      _savedPassword = password;
    }
    _applySession(session);
  }

  Future<void> logout() async {
    await tokenStore.clear();
    _token = null;
    _userId = 0;
    _username = '';
    _savedPassword = '';
    _isAdmin = false;
    _status = SessionStatus.unauthenticated;
    notifyListeners();
  }

  /// Called when the API rejects a token. The store is already cleared by the
  /// client; this updates the UI so the user lands on the login screen with a
  /// clear reason instead of an unexplained blank view.
  void markExpired() {
    _token = null;
    _status = _status == SessionStatus.authenticated
        ? SessionStatus.expired
        : SessionStatus.unauthenticated;
    notifyListeners();
  }

  void clearLastError() {
    _lastError = null;
    notifyListeners();
  }

  ApiClient buildApiClient({HttpTransport? transport}) {
    final client = ApiClient(
      baseUrl: _serverUrl,
      tokenStore: tokenStore,
      transport: transport ?? transportOverride ?? ioTransport(kHttpClient),
    );
    final currentToken = _token;
    if (currentToken != null && currentToken.isNotEmpty) {
      client.setBearer(currentToken);
    }
    return client;
  }
}

/// The [http.Client] used by production. Kept here so tests can replace it
/// via [AppState.buildApiClient] without importing `http` in feature code.
final kHttpClient = http.Client();
