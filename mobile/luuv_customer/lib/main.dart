import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:webview_flutter/webview_flutter.dart';

const _brandColor = Color(0xFFFB6C03);
const _systemUiStyle = SystemUiOverlayStyle(
  statusBarColor: Colors.white,
  statusBarIconBrightness: Brightness.dark,
  statusBarBrightness: Brightness.light,
  systemNavigationBarColor: Colors.black,
  systemNavigationBarIconBrightness: Brightness.light,
  systemNavigationBarDividerColor: Colors.black,
  systemNavigationBarContrastEnforced: false,
);
const _defaultPortalUrl = 'https://luuvgrand.com/cus?r=Kurunegala';
const _portalUrl = String.fromEnvironment(
  'CUSTOMER_PORTAL_URL',
  defaultValue: _defaultPortalUrl,
);
const _testNotificationOnStart = bool.fromEnvironment(
  'TEST_ORDER_READY_NOTIFICATION',
);
const _appVersion = String.fromEnvironment(
  'APP_VERSION',
  defaultValue: '1.0.0',
);

@pragma('vm:entry-point')
Future<void> firebaseMessagingBackgroundHandler(RemoteMessage message) async {
  try {
    await Firebase.initializeApp();
  } catch (_) {
    // A local build without Firebase config still has the foreground polling
    // fallback. Configured production builds initialize normally here.
  }
}

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  FirebaseMessaging.onBackgroundMessage(firebaseMessagingBackgroundHandler);
  await AppNotifications.initialize();
  await SystemChrome.setEnabledSystemUIMode(
    SystemUiMode.manual,
    overlays: SystemUiOverlay.values,
  );
  SystemChrome.setSystemUIOverlayStyle(_systemUiStyle);
  runApp(const LuuvCustomerApp());
}

class LuuvCustomerApp extends StatelessWidget {
  const LuuvCustomerApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Luuv Fryxo',
      debugShowCheckedModeBanner: false,
      theme: ThemeData(
        colorScheme: ColorScheme.fromSeed(seedColor: _brandColor),
        scaffoldBackgroundColor: Colors.white,
        useMaterial3: true,
      ),
      home: const CustomerPortalScreen(),
    );
  }
}

class CustomerPortalScreen extends StatefulWidget {
  const CustomerPortalScreen({super.key});

  @override
  State<CustomerPortalScreen> createState() => _CustomerPortalScreenState();
}

class _CustomerPortalScreenState extends State<CustomerPortalScreen> {
  late final Uri _portalUri;
  late final WebViewController _controller;

  int _progress = 0;
  bool _showLaunchScreen = true;
  DateTime _launchStartedAt = DateTime.now();
  bool _mainFrameFailed = false;
  bool _hasOrderSnapshot = false;
  Map<String, String> _orderStatuses = {};
  StreamSubscription<String>? _notificationRouteSubscription;
  StreamSubscription<String>? _pushTokenSubscription;
  String? _customerToken;
  String? _fcmToken;
  String _restaurant = '';
  String _subscribedRestaurantTopic = '';

  @override
  void initState() {
    super.initState();
    _portalUri = _validatedPortalUri(_portalUrl);
    _controller = WebViewController()
      ..setJavaScriptMode(JavaScriptMode.unrestricted)
      ..setBackgroundColor(Colors.white)
      ..addJavaScriptChannel(
        'LuuvNavigation',
        onMessageReceived: (message) {
          final uri = Uri.tryParse(message.message);
          if (uri != null) unawaited(_openExternal(uri));
        },
      )
      ..addJavaScriptChannel(
        'LuuvOrderStatus',
        onMessageReceived: (message) => _handleOrderStatuses(message.message),
      )
      ..addJavaScriptChannel(
        'LuuvSession',
        onMessageReceived: (message) => _handleCustomerSession(message.message),
      )
      ..setNavigationDelegate(
        NavigationDelegate(
          onProgress: (progress) {
            if (mounted) setState(() => _progress = progress);
          },
          onPageStarted: (_) {
            if (mounted) {
              setState(() {
                _progress = 0;
                _mainFrameFailed = false;
              });
            }
          },
          onPageFinished: (_) async {
            if (mounted) setState(() => _progress = 100);
            await _installWindowOpenBridge();
            await _revealPortal();
          },
          onWebResourceError: (error) {
            if (error.isForMainFrame ?? false) {
              if (mounted) setState(() => _mainFrameFailed = true);
            }
          },
          onNavigationRequest: (request) {
            final uri = Uri.tryParse(request.url);
            if (uri == null) return NavigationDecision.prevent;
            if (isPortalNavigation(uri, _portalUri)) {
              return NavigationDecision.navigate;
            }
            unawaited(_openExternal(uri));
            return NavigationDecision.prevent;
          },
        ),
      )
      ..loadRequest(_portalUri);

    _notificationRouteSubscription = AppNotifications.routes.listen(
      _openNotificationRoute,
    );
    _pushTokenSubscription = AppNotifications.tokens.listen((token) {
      _fcmToken = token;
      unawaited(_registerPushToken());
    });
    final initialRoute = AppNotifications.takePendingRoute();
    if (initialRoute != null) {
      WidgetsBinding.instance.addPostFrameCallback(
        (_) => _openNotificationRoute(initialRoute),
      );
    }

    WidgetsBinding.instance.addPostFrameCallback((_) async {
      await AppNotifications.requestPermission();
      _fcmToken = await AppNotifications.currentToken();
      await _registerPushToken();
      if (_testNotificationOnStart) {
        await Future<void>.delayed(const Duration(seconds: 2));
        await AppNotifications.showOrderReady(orderName: 'TEST-ORDER');
      }
    });
  }

  @override
  void dispose() {
    _notificationRouteSubscription?.cancel();
    _pushTokenSubscription?.cancel();
    super.dispose();
  }

  Future<void> _installWindowOpenBridge() async {
    await _controller.runJavaScript('''
      (function () {
        window.open = function (url) {
          if (url) LuuvNavigation.postMessage(String(url));
          return null;
        };

        if (window.__luuvNativeOrderTimer) {
          clearInterval(window.__luuvNativeOrderTimer);
        }
        if (window.__luuvNativeSessionTimer) {
          clearInterval(window.__luuvNativeSessionTimer);
        }
        let lastLuuvSession = null;
        function reportLuuvSession() {
          try {
            const token = localStorage.getItem('lf_token') || '';
            const params = new URLSearchParams(location.search);
            const restaurant = params.get('r') || (typeof RESTAURANT !== 'undefined' ? RESTAURANT : '');
            const value = JSON.stringify({token: token, restaurant: restaurant});
            if (value !== lastLuuvSession) {
              lastLuuvSession = value;
              LuuvSession.postMessage(value);
            }
          } catch (_) {}
        }
        async function reportLuuvOrderStatuses() {
          try {
            const token = localStorage.getItem('lf_token');
            if (!token) return;
            const url = '/api/method/luuvcrm.api.customer_get_orders?token=' +
              encodeURIComponent(token);
            const response = await fetch(url, {credentials: 'include'});
            const payload = await response.json();
            const orders = payload && payload.message && payload.message.orders;
            if (!Array.isArray(orders)) return;
            LuuvOrderStatus.postMessage(JSON.stringify(orders.map(function (o) {
              return {name: String(o.name || ''), status: String(o.status || '')};
            })));
          } catch (_) {}
        }
        reportLuuvSession();
        reportLuuvOrderStatuses();
        window.__luuvNativeSessionTimer = setInterval(reportLuuvSession, 2000);
        window.__luuvNativeOrderTimer = setInterval(reportLuuvOrderStatuses, 8000);
      })();
    ''');
  }

  void _handleCustomerSession(String raw) {
    try {
      final data = jsonDecode(raw);
      if (data is! Map<String, dynamic>) return;
      final nextToken = (data['token'] ?? '').toString().trim();
      final nextRestaurant = (data['restaurant'] ?? '').toString().trim();
      final previousToken = _customerToken;
      final restaurantChanged = nextRestaurant != _restaurant;
      _restaurant = nextRestaurant;

      if (nextToken == previousToken && !restaurantChanged) return;
      if ((previousToken ?? '').isNotEmpty && nextToken != previousToken) {
        unawaited(_unregisterPushToken(previousToken!));
      }
      _customerToken = nextToken.isEmpty ? null : nextToken;
      unawaited(_syncRestaurantTopic());
      unawaited(_registerPushToken());
    } on FormatException {
      // Wait for the next session bridge update.
    }
  }

  Future<void> _registerPushToken() async {
    final customerToken = _customerToken;
    final deviceToken = _fcmToken;
    if (customerToken == null || deviceToken == null || deviceToken.isEmpty) {
      return;
    }
    await _postPushEndpoint('customer_register_push_token', {
      'customer_token': customerToken,
      'device_token': deviceToken,
      'platform': defaultTargetPlatform == TargetPlatform.iOS
          ? 'ios'
          : 'android',
      'restaurant': _restaurant,
      'app_version': _appVersion,
    });
  }

  Future<void> _unregisterPushToken(String customerToken) async {
    final deviceToken = _fcmToken;
    if (deviceToken == null || deviceToken.isEmpty) return;
    await _postPushEndpoint('customer_unregister_push_token', {
      'customer_token': customerToken,
      'device_token': deviceToken,
    });
  }

  Future<void> _postPushEndpoint(
    String method,
    Map<String, String> fields,
  ) async {
    final client = HttpClient();
    try {
      final uri = _portalUri.resolve('/api/method/luuvcrm.api.$method');
      final request = await client.postUrl(uri);
      request.headers.contentType = ContentType(
        'application',
        'x-www-form-urlencoded',
        charset: 'utf-8',
      );
      request.write(Uri(queryParameters: fields).query);
      final response = await request.close();
      await response.drain<void>();
    } catch (_) {
      // Registration is retried on the next page/session or FCM token update.
    } finally {
      client.close(force: true);
    }
  }

  Future<void> _syncRestaurantTopic() async {
    final nextTopic = restaurantTopic(_restaurant);
    if (_subscribedRestaurantTopic == nextTopic) return;
    if (_subscribedRestaurantTopic.isNotEmpty) {
      await AppNotifications.unsubscribeFromTopic(_subscribedRestaurantTopic);
    }
    _subscribedRestaurantTopic = nextTopic;
    if (nextTopic.isNotEmpty) {
      await AppNotifications.subscribeToTopic(nextTopic);
    }
  }

  void _openNotificationRoute(String route) {
    final target = notificationRoute(route, _portalUri);
    if (target != null) unawaited(_controller.loadRequest(target));
  }

  void _handleOrderStatuses(String raw) {
    try {
      final rows = jsonDecode(raw) as List<dynamic>;
      final current = <String, String>{};
      for (final row in rows) {
        if (row is! Map<String, dynamic>) continue;
        final name = (row['name'] ?? '').toString();
        final status = (row['status'] ?? '').toString();
        if (name.isNotEmpty) current[name] = status;
      }

      if (_hasOrderSnapshot) {
        for (final orderName in newlyReadyOrders(_orderStatuses, current)) {
          unawaited(AppNotifications.showOrderReady(orderName: orderName));
        }
      }
      _orderStatuses = current;
      _hasOrderSnapshot = true;
    } on FormatException {
      // Ignore a malformed bridge message and wait for the next poll.
    } on TypeError {
      // Ignore an unexpected payload shape and wait for the next poll.
    }
  }

  Future<void> _openExternal(Uri uri) async {
    final launched = await launchUrl(uri, mode: LaunchMode.externalApplication);
    if (!launched && mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('Could not open this link.')),
      );
    }
  }

  Future<void> _handleBack() async {
    if (await _controller.canGoBack()) {
      await _controller.goBack();
      return;
    }
    await SystemNavigator.pop();
  }

  void _retry() {
    setState(() {
      _mainFrameFailed = false;
      _progress = 0;
      _showLaunchScreen = true;
      _launchStartedAt = DateTime.now();
    });
    _controller.loadRequest(_portalUri);
  }

  Future<void> _revealPortal() async {
    const minimumBrandTime = Duration(milliseconds: 850);
    final elapsed = DateTime.now().difference(_launchStartedAt);
    if (elapsed < minimumBrandTime) {
      await Future<void>.delayed(minimumBrandTime - elapsed);
    }
    if (mounted && !_mainFrameFailed) {
      setState(() => _showLaunchScreen = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    final systemNavigationInset = MediaQuery.viewPaddingOf(context).bottom;
    return AnnotatedRegion<SystemUiOverlayStyle>(
      value: _systemUiStyle,
      child: PopScope<Object?>(
        canPop: false,
        onPopInvokedWithResult: (didPop, _) {
          if (!didPop) unawaited(_handleBack());
        },
        child: Scaffold(
          // Edge-to-edge Android renders the status area over this surface.
          // Keep it light; the bottom system-navigation inset is painted black
          // separately below.
          backgroundColor: Colors.white,
          body: SafeArea(
            // The page owns its app navigation. A black overlay below keeps the
            // Android system-navigation area visually separate and predictable.
            bottom: false,
            child: Stack(
              children: [
                Positioned.fill(child: WebViewWidget(controller: _controller)),
                if (_progress < 100 && !_mainFrameFailed)
                  Align(
                    alignment: Alignment.topCenter,
                    child: LinearProgressIndicator(
                      value: _progress == 0 ? null : _progress / 100,
                      minHeight: 3,
                      color: _brandColor,
                      backgroundColor: const Color(0xFFFFE2D2),
                    ),
                  ),
                Positioned.fill(
                  child: IgnorePointer(
                    ignoring: !_showLaunchScreen,
                    child: AnimatedOpacity(
                      opacity: _showLaunchScreen ? 1 : 0,
                      duration: const Duration(milliseconds: 360),
                      curve: Curves.easeOutCubic,
                      child: _PortalLoadingView(progress: _progress),
                    ),
                  ),
                ),
                if (_mainFrameFailed) _OfflineView(onRetry: _retry),
                if (systemNavigationInset > 0)
                  Positioned(
                    left: 0,
                    right: 0,
                    bottom: 0,
                    height: systemNavigationInset,
                    child: const ColoredBox(color: Colors.black),
                  ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}

class _PortalLoadingView extends StatelessWidget {
  const _PortalLoadingView({required this.progress});

  final int progress;

  @override
  Widget build(BuildContext context) {
    final progressValue = progress <= 3 || progress >= 100
        ? null
        : progress / 100;
    return DecoratedBox(
      decoration: const BoxDecoration(
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [Color(0xFFFFFAF6), Color(0xFFFFE8D8)],
        ),
      ),
      child: Stack(
        children: [
          const Positioned(
            right: -72,
            top: -58,
            child: _LoadingGlow(size: 210, opacity: .075),
          ),
          const Positioned(
            left: -88,
            bottom: -76,
            child: _LoadingGlow(size: 235, opacity: .055),
          ),
          Center(
            child: Padding(
              padding: const EdgeInsets.symmetric(horizontal: 34),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: [
                  TweenAnimationBuilder<double>(
                    tween: Tween(begin: .9, end: 1),
                    duration: const Duration(milliseconds: 650),
                    curve: Curves.easeOutBack,
                    builder: (context, scale, child) =>
                        Transform.scale(scale: scale, child: child),
                    child: Container(
                      width: 112,
                      height: 112,
                      padding: const EdgeInsets.all(9),
                      decoration: BoxDecoration(
                        color: Colors.white,
                        borderRadius: BorderRadius.circular(34),
                        boxShadow: const [
                          BoxShadow(
                            color: Color(0x2E9D3E08),
                            blurRadius: 30,
                            offset: Offset(0, 15),
                          ),
                        ],
                      ),
                      child: ClipRRect(
                        borderRadius: BorderRadius.circular(27),
                        child: Image.asset(
                          'assets/icon.png',
                          fit: BoxFit.cover,
                        ),
                      ),
                    ),
                  ),
                  const SizedBox(height: 24),
                  const Text(
                    'Luuv Fryxo',
                    style: TextStyle(
                      color: Color(0xFF1F1915),
                      fontSize: 29,
                      height: 1,
                      fontWeight: FontWeight.w900,
                      letterSpacing: -1.1,
                    ),
                  ),
                  const SizedBox(height: 9),
                  const Text(
                    'Freshly made. Ready when you are.',
                    textAlign: TextAlign.center,
                    style: TextStyle(
                      color: Color(0xFF8D7D73),
                      fontSize: 13,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                  const SizedBox(height: 28),
                  SizedBox(
                    width: 156,
                    child: ClipRRect(
                      borderRadius: BorderRadius.circular(99),
                      child: LinearProgressIndicator(
                        value: progressValue,
                        minHeight: 5,
                        color: _brandColor,
                        backgroundColor: const Color(0xFFFFD3B8),
                      ),
                    ),
                  ),
                  const SizedBox(height: 12),
                  Text(
                    progress >= 75 ? 'Almost ready…' : 'Preparing your menu…',
                    style: const TextStyle(
                      color: Color(0xFFB05A24),
                      fontSize: 10.5,
                      fontWeight: FontWeight.w800,
                      letterSpacing: .25,
                    ),
                  ),
                ],
              ),
            ),
          ),
          const Positioned(
            left: 0,
            right: 0,
            bottom: 34,
            child: Text(
              'ORDER  •  TRACK  •  REWARD',
              textAlign: TextAlign.center,
              style: TextStyle(
                color: Color(0xFFBB7250),
                fontSize: 9,
                fontWeight: FontWeight.w900,
                letterSpacing: 1.25,
              ),
            ),
          ),
        ],
      ),
    );
  }
}

class _LoadingGlow extends StatelessWidget {
  const _LoadingGlow({required this.size, required this.opacity});

  final double size;
  final double opacity;

  @override
  Widget build(BuildContext context) {
    return Container(
      width: size,
      height: size,
      decoration: BoxDecoration(
        shape: BoxShape.circle,
        color: _brandColor.withValues(alpha: opacity),
        border: Border.all(
          color: _brandColor.withValues(alpha: opacity * .9),
          width: 28,
        ),
      ),
    );
  }
}

class _OfflineView extends StatelessWidget {
  const _OfflineView({required this.onRetry});

  final VoidCallback onRetry;

  @override
  Widget build(BuildContext context) {
    return ColoredBox(
      color: const Color(0xFFFAF8F6),
      child: Center(
        child: Padding(
          padding: const EdgeInsets.all(28),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Icon(Icons.wifi_off_rounded, size: 52, color: _brandColor),
              const SizedBox(height: 18),
              Text(
                'No internet connection',
                style: Theme.of(context).textTheme.titleLarge
                    ?.copyWith(fontWeight: FontWeight.w800),
              ),
              const SizedBox(height: 8),
              const Text(
                'Connect to the internet and try loading the customer portal again.',
                textAlign: TextAlign.center,
              ),
              const SizedBox(height: 22),
              FilledButton.icon(
                onPressed: onRetry,
                icon: const Icon(Icons.refresh_rounded),
                label: const Text('Try again'),
                style: FilledButton.styleFrom(backgroundColor: _brandColor),
              ),
            ],
          ),
        ),
      ),
    );
  }
}

Uri _validatedPortalUri(String value) {
  final uri = Uri.tryParse(value);
  final secure = uri?.scheme == 'https';
  final localDebug =
      kDebugMode &&
      uri?.scheme == 'http' &&
      const {'127.0.0.1', 'localhost'}.contains(uri?.host);
  if (uri == null || uri.host.isEmpty || (!secure && !localDebug)) {
    throw ArgumentError.value(
      value,
      'CUSTOMER_PORTAL_URL',
      'must be HTTPS, or localhost HTTP in a debug build',
    );
  }
  return uri;
}

bool isPortalNavigation(Uri candidate, Uri portal) {
  return candidate.scheme == portal.scheme &&
      candidate.host == portal.host &&
      candidate.port == portal.port;
}

List<String> newlyReadyOrders(
  Map<String, String> previous,
  Map<String, String> current,
) {
  return current.entries
      .where(
        (entry) => entry.value == 'Ready' && previous[entry.key] != entry.value,
      )
      .map((entry) => entry.key)
      .toList(growable: false);
}

String restaurantTopic(String restaurant) {
  final slug = restaurant
      .toLowerCase()
      .replaceAll(RegExp('[^a-z0-9_-]+'), '_')
      .replaceAll(RegExp(r'^_+|_+$'), '');
  return slug.isEmpty ? '' : 'luuv_$slug';
}

Uri? notificationRoute(String route, Uri portal) {
  final value = route.trim();
  if (value.isEmpty) return null;
  final parsed = Uri.tryParse(value);
  if (parsed == null) return null;
  final target = parsed.hasScheme ? parsed : portal.resolveUri(parsed);
  return isPortalNavigation(target, portal) ? target : null;
}

class AppNotifications {
  static final FlutterLocalNotificationsPlugin _plugin =
      FlutterLocalNotificationsPlugin();
  static final StreamController<String> _routes =
      StreamController<String>.broadcast();
  static final StreamController<String> _tokens =
      StreamController<String>.broadcast();
  static final Map<String, DateTime> _recentReadyOrders = {};
  static final Completer<String?> _firstRegistration = Completer<String?>();
  static bool _firebaseAvailable = false;
  static String? _currentToken;
  static String? _pendingRoute;

  static const _orderChannel = AndroidNotificationChannel(
    'order_updates',
    'Order updates',
    description: 'Plays an alert when a customer order is ready.',
    importance: Importance.max,
    playSound: true,
    enableVibration: true,
  );
  static const _offersChannel = AndroidNotificationChannel(
    'offers_promotions',
    'Offers and promotions',
    description: 'New restaurant offers and promotions.',
    importance: Importance.high,
    playSound: true,
  );

  static Stream<String> get routes => _routes.stream;
  static Stream<String> get tokens => _tokens.stream;

  static Future<void> initialize() async {
    const settings = InitializationSettings(
      android: AndroidInitializationSettings('@mipmap/ic_launcher'),
      iOS: DarwinInitializationSettings(
        requestAlertPermission: false,
        requestBadgePermission: false,
        requestSoundPermission: false,
      ),
    );
    await _plugin.initialize(
      settings: settings,
      onDidReceiveNotificationResponse: (response) {
        final route = response.payload;
        if (route != null && route.isNotEmpty) _emitRoute(route);
      },
    );
    final android = _plugin
        .resolvePlatformSpecificImplementation<
          AndroidFlutterLocalNotificationsPlugin
        >();
    await android?.createNotificationChannel(_orderChannel);
    await android?.createNotificationChannel(_offersChannel);

    try {
      await Firebase.initializeApp();
      _firebaseAvailable = true;
      await FirebaseMessaging.instance
          .setForegroundNotificationPresentationOptions(
            alert: false,
            badge: false,
            sound: false,
          );
      FirebaseMessaging.onMessage.listen(_showRemoteMessage);
      FirebaseMessaging.onMessageOpenedApp.listen(_routeRemoteMessage);
      FirebaseMessaging.instance.onRegistered.listen((token) {
        _currentToken = token;
        if (!_firstRegistration.isCompleted) {
          _firstRegistration.complete(token);
        }
        _tokens.add(token);
      });
      await FirebaseMessaging.instance.register();
      final initial = await FirebaseMessaging.instance.getInitialMessage();
      if (initial != null) _routeRemoteMessage(initial);
      await FirebaseMessaging.instance.subscribeToTopic('luuv_customers');
    } catch (_) {
      _firebaseAvailable = false;
    }
  }

  static Future<void> requestPermission() async {
    await _plugin
        .resolvePlatformSpecificImplementation<
          AndroidFlutterLocalNotificationsPlugin
        >()
        ?.requestNotificationsPermission();
    if (_firebaseAvailable) {
      await FirebaseMessaging.instance.requestPermission(
        alert: true,
        badge: true,
        sound: true,
      );
    }
  }

  static Future<String?> currentToken() async {
    if (!_firebaseAvailable) return null;
    try {
      if (_currentToken != null) return _currentToken;
      await FirebaseMessaging.instance.register();
      return await _firstRegistration.future.timeout(
        const Duration(seconds: 10),
        onTimeout: () => null,
      );
    } catch (_) {
      return null;
    }
  }

  static Future<void> subscribeToTopic(String topic) async {
    if (!_firebaseAvailable || topic.isEmpty) return;
    try {
      await FirebaseMessaging.instance.subscribeToTopic(topic);
    } catch (_) {}
  }

  static Future<void> unsubscribeFromTopic(String topic) async {
    if (!_firebaseAvailable || topic.isEmpty) return;
    try {
      await FirebaseMessaging.instance.unsubscribeFromTopic(topic);
    } catch (_) {}
  }

  static String? takePendingRoute() {
    final route = _pendingRoute;
    _pendingRoute = null;
    return route;
  }

  static void _emitRoute(String route) {
    _pendingRoute = route;
    if (_routes.hasListener) {
      _pendingRoute = null;
      _routes.add(route);
    }
  }

  static String _routeFor(RemoteMessage message) {
    final explicit = (message.data['route'] ?? '').toString().trim();
    if (explicit.isNotEmpty) return explicit;
    final kind = (message.data['type'] ?? '').toString();
    return kind == 'order_ready' ? '/cus?tab=orders' : '/cus?tab=offers';
  }

  static void _routeRemoteMessage(RemoteMessage message) {
    _emitRoute(_routeFor(message));
  }

  static Future<void> _showRemoteMessage(RemoteMessage message) async {
    final kind = (message.data['type'] ?? 'promotion').toString();
    final title =
        message.notification?.title ??
        (message.data['title'] ?? 'Luuv Grand').toString();
    final body =
        message.notification?.body ??
        (message.data['body'] ?? 'A new update is waiting for you.').toString();
    final route = _routeFor(message);
    final orderName = (message.data['order_name'] ?? '').toString();
    if (kind == 'order_ready' && orderName.isNotEmpty) {
      await showOrderReady(
        orderName: orderName,
        title: title,
        body: body,
        route: route,
      );
      return;
    }
    await showMessage(
      id: message.messageId?.hashCode ?? DateTime.now().millisecondsSinceEpoch,
      title: title,
      body: body,
      kind: kind,
      route: route,
    );
  }

  static Future<void> showOrderReady({
    required String orderName,
    String title = 'Your order is ready! 🍽️',
    String? body,
    String route = '/cus?tab=orders',
  }) async {
    final now = DateTime.now();
    _recentReadyOrders.removeWhere(
      (_, shownAt) => now.difference(shownAt) > const Duration(minutes: 10),
    );
    if (_recentReadyOrders.containsKey(orderName)) return;
    _recentReadyOrders[orderName] = now;
    await showMessage(
      id: orderName.hashCode,
      title: title,
      body: body ?? '#$orderName is prepared and ready for pickup.',
      kind: 'order_ready',
      route: route,
    );
  }

  static Future<void> showMessage({
    required int id,
    required String title,
    required String body,
    required String kind,
    required String route,
  }) async {
    final order = kind == 'order_ready';
    final details = NotificationDetails(
      android: AndroidNotificationDetails(
        order ? 'order_updates' : 'offers_promotions',
        order ? 'Order updates' : 'Offers and promotions',
        channelDescription: order
            ? 'Plays an alert when a customer order is ready.'
            : 'New restaurant offers and promotions.',
        importance: order ? Importance.max : Importance.high,
        priority: Priority.high,
        playSound: true,
        enableVibration: order,
        category: order
            ? AndroidNotificationCategory.status
            : AndroidNotificationCategory.promo,
      ),
      iOS: const DarwinNotificationDetails(
        presentAlert: true,
        presentBadge: true,
        presentSound: true,
      ),
    );
    await _plugin.show(
      id: id & 0x7fffffff,
      title: title,
      body: body,
      notificationDetails: details,
      payload: route,
    );
  }
}
