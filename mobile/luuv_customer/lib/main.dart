import 'dart:async';
import 'dart:convert';

import 'package:flutter/foundation.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:url_launcher/url_launcher.dart';
import 'package:webview_flutter/webview_flutter.dart';

const _brandColor = Color(0xFFF15207);
const _defaultPortalUrl = 'https://luuvgrand.com/cus?r=Kurunegala';
const _portalUrl = String.fromEnvironment(
  'CUSTOMER_PORTAL_URL',
  defaultValue: _defaultPortalUrl,
);
const _testNotificationOnStart = bool.fromEnvironment(
  'TEST_ORDER_READY_NOTIFICATION',
);

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await OrderReadyNotifications.initialize();
  SystemChrome.setSystemUIOverlayStyle(
    const SystemUiOverlayStyle(
      statusBarColor: Colors.white,
      statusBarIconBrightness: Brightness.dark,
      statusBarBrightness: Brightness.light,
      systemNavigationBarColor: Colors.white,
      systemNavigationBarIconBrightness: Brightness.dark,
    ),
  );
  runApp(const LuuvCustomerApp());
}

class LuuvCustomerApp extends StatelessWidget {
  const LuuvCustomerApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'Luuv Kurunegala',
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
  bool _mainFrameFailed = false;
  bool _hasOrderSnapshot = false;
  Map<String, String> _orderStatuses = {};

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

    WidgetsBinding.instance.addPostFrameCallback((_) async {
      await OrderReadyNotifications.requestPermission();
      if (_testNotificationOnStart) {
        await Future<void>.delayed(const Duration(seconds: 2));
        await OrderReadyNotifications.show(orderName: 'TEST-ORDER');
      }
    });
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
        reportLuuvOrderStatuses();
        window.__luuvNativeOrderTimer = setInterval(reportLuuvOrderStatuses, 8000);
      })();
    ''');
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
          unawaited(OrderReadyNotifications.show(orderName: orderName));
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
    });
    _controller.loadRequest(_portalUri);
  }

  @override
  Widget build(BuildContext context) {
    return PopScope<Object?>(
      canPop: false,
      onPopInvokedWithResult: (didPop, _) {
        if (!didPop) unawaited(_handleBack());
      },
      child: Scaffold(
        body: SafeArea(
          bottom: true,
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
              if (_mainFrameFailed) _OfflineView(onRetry: _retry),
            ],
          ),
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

class OrderReadyNotifications {
  static final FlutterLocalNotificationsPlugin _plugin =
      FlutterLocalNotificationsPlugin();

  static const _channel = AndroidNotificationChannel(
    'order_ready',
    'Order ready alerts',
    description: 'Plays an alert when a customer order is ready.',
    importance: Importance.max,
    playSound: true,
    enableVibration: true,
  );

  static Future<void> initialize() async {
    const settings = InitializationSettings(
      android: AndroidInitializationSettings('@mipmap/ic_launcher'),
      iOS: DarwinInitializationSettings(),
    );
    await _plugin.initialize(settings: settings);
    await _plugin
        .resolvePlatformSpecificImplementation<
          AndroidFlutterLocalNotificationsPlugin
        >()
        ?.createNotificationChannel(_channel);
  }

  static Future<void> requestPermission() async {
    await _plugin
        .resolvePlatformSpecificImplementation<
          AndroidFlutterLocalNotificationsPlugin
        >()
        ?.requestNotificationsPermission();
  }

  static Future<void> show({required String orderName}) async {
    const details = NotificationDetails(
      android: AndroidNotificationDetails(
        'order_ready',
        'Order ready alerts',
        channelDescription: 'Plays an alert when a customer order is ready.',
        importance: Importance.max,
        priority: Priority.high,
        playSound: true,
        enableVibration: true,
        category: AndroidNotificationCategory.status,
      ),
      iOS: DarwinNotificationDetails(presentSound: true),
    );
    await _plugin.show(
      id: orderName.hashCode & 0x7fffffff,
      title: 'Your order is ready! 🍽️',
      body: '#$orderName is ready for pickup.',
      notificationDetails: details,
      payload: orderName,
    );
  }
}
