// Runs without Flutter/plugins: dart test/guest_push_registration_check.dart
import '../lib/guest_push_registration.dart';

void check(bool condition, String description) {
  if (!condition) throw StateError(description);
}

void main() {
  final orders = GuestPushRegistrations();
  orders.remember('ORDER-1', 'proof-1');
  check(orders.pendingFor('token-1')['ORDER-1'] == 'proof-1', 'New order');
  check(
    !orders.complete(
      'ORDER-1',
      'proof-1',
      'token-1',
      PushRegistrationResult.registered,
    ),
    'Success must retain the persisted proof',
  );
  orders.remember('ORDER-1', 'proof-1');
  check(orders.pendingFor('token-1').isEmpty, 'Repeated bridge message');
  check(
    orders.pendingFor('token-2')['ORDER-1'] == 'proof-1',
    'Token rotation must re-register an existing order',
  );

  orders.complete('ORDER-1', 'proof-1', 'token-2', PushRegistrationResult.retry);
  check(orders.pendingFor('token-2').isNotEmpty, 'Transient failure retries');
  orders.remember('ORDER-1', 'proof-2');
  check(
    !orders.complete(
      'ORDER-1',
      'proof-1',
      'token-1',
      PushRegistrationResult.terminal,
    ),
    'Delayed rejection must not erase a replacement proof',
  );
  check(orders.pendingFor('token-2')['ORDER-1'] == 'proof-2', 'Updated proof');
  check(
    orders.complete(
      'ORDER-1',
      'proof-2',
      'token-2',
      PushRegistrationResult.terminal,
    ),
    'Terminal rejection removes its persisted proof',
  );
  check(orders.isEmpty, 'Terminal rejection must stop retrying');

  check(
    pushRegistrationResult({
          'message': {'success': true},
        }) ==
        PushRegistrationResult.registered,
    'Successful backend response',
  );
  for (final error in ['Invalid order ticket', 'Order is not available']) {
    check(
      pushRegistrationResult({
            'message': {'success': false, 'error': error},
          }) ==
          PushRegistrationResult.terminal,
      'Terminal backend response: $error',
    );
  }
  for (final payload in [
    null,
    <String, Object>{},
    {'message': 'Unavailable'},
    {
      'message': {'success': false, 'error': 'Push notifications need bench migrate'},
    },
  ]) {
    check(
      pushRegistrationResult(payload) == PushRegistrationResult.retry,
      'Malformed and temporary backend failures keep their proof',
    );
  }
  print('Guest push checks passed: duplicate bridge reports, token rotation, '
      'transient retry, replacement proof, terminal cleanup and response parsing.');
}
