import 'package:flutter_test/flutter_test.dart';
import 'package:luuv_customer/main.dart';

void main() {
  final portal = Uri.parse('https://luuvgrand.com/cus?r=Kurunegala');

  test('keeps portal links inside the WebView', () {
    expect(
      isPortalNavigation(
        Uri.parse('https://luuvgrand.com/cus?tab=orders'),
        portal,
      ),
      isTrue,
    );
    expect(
      isPortalNavigation(
        Uri.parse('https://luuvgrand.com/api/method/luuvcrm.api.ping'),
        portal,
      ),
      isTrue,
    );
  });

  test('sends external and non-HTTPS links to the device', () {
    expect(
      isPortalNavigation(Uri.parse('https://wa.me/94770000000'), portal),
      isFalse,
    );
    expect(isPortalNavigation(Uri.parse('tel:+94770000000'), portal), isFalse);
    expect(
      isPortalNavigation(Uri.parse('http://luuvgrand.com/cus'), portal),
      isFalse,
    );
  });

  test('detects only transitions into Ready', () {
    final previous = <String, String>{
      'ORD-1': 'Processing',
      'ORD-2': 'Ready',
      'ORD-3': 'Pending',
    };
    final current = <String, String>{
      'ORD-1': 'Ready',
      'ORD-2': 'Ready',
      'ORD-3': 'Served',
    };

    expect(newlyReadyOrders(previous, current), ['ORD-1']);
  });
}
