enum PushRegistrationResult { registered, retry, terminal }

PushRegistrationResult pushRegistrationResult(Object? payload) {
  if (payload is! Map) return PushRegistrationResult.retry;
  final message = payload['message'];
  if (message is! Map) return PushRegistrationResult.retry;
  if (message['success'] == true) return PushRegistrationResult.registered;
  if (message['error'] == 'Invalid order ticket' ||
      message['error'] == 'Order is not available') {
    return PushRegistrationResult.terminal;
  }
  return PushRegistrationResult.retry;
}

// Keep proofs after a successful registration so a rotated FCM delivery token
// can still receive updates for the same order. Avoid repeating registrations
// for an unchanged token when the WebView reports its saved proofs again.
class GuestPushRegistrations {
  final Map<String, String> _tickets = {};
  final Map<String, String> _registeredTokens = {};

  bool get isEmpty => _tickets.isEmpty;

  void remember(String orderName, String ticket) {
    if (_tickets[orderName] != ticket) _registeredTokens.remove(orderName);
    _tickets[orderName] = ticket;
  }

  Map<String, String> pendingFor(String? deviceToken) => {
    for (final entry in _tickets.entries)
      if (deviceToken == null || _registeredTokens[entry.key] != deviceToken)
        entry.key: entry.value,
  };

  bool complete(
    String orderName,
    String ticket,
    String deviceToken,
    PushRegistrationResult result,
  ) {
    // A delayed response must not overwrite a newer proof from the WebView.
    if (_tickets[orderName] != ticket) return false;
    if (result == PushRegistrationResult.registered) {
      _registeredTokens[orderName] = deviceToken;
    } else if (result == PushRegistrationResult.terminal) {
      _tickets.remove(orderName);
      _registeredTokens.remove(orderName);
      return true;
    }
    return false;
  }
}
