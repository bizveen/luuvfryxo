# Luuv Customer

Flutter shell for the Luuv Kurunegala customer portal. The app keeps the
existing `/cus` ordering, account, order history, and loyalty experience in a
persistent native WebView.

## Run

```bash
flutter pub get
flutter run
```

The default portal is:

```text
https://luuvgrand.com/cus?r=Kurunegala
```

Use another deployment without editing source code:

```bash
flutter run \
  --dart-define=CUSTOMER_PORTAL_URL=https://example.com/cus?r=Kurunegala
```

Release Android App Bundle:

```bash
flutter build appbundle --release \
  --dart-define=CUSTOMER_PORTAL_URL=https://luuvgrand.com/cus?r=Kurunegala
```

## Native behavior

- Cookies and browser local storage persist customer sessions and cart state.
- Links on `luuvgrand.com` remain inside the app.
- WhatsApp, phone, email, maps, and other external links open in the matching
  device app.
- Android back navigates WebView history before closing the app.
- A native retry screen appears when the main page cannot load.
- While the app is running, the native bridge checks signed-in customer order
  statuses every eight seconds and plays a high-priority notification when an
  order changes to `Ready`.

For local Android development, run the Frappe server and reverse its port to a
USB-connected phone:

```bash
adb reverse tcp:8000 tcp:8000
flutter run \
  --dart-define=CUSTOMER_PORTAL_URL=http://127.0.0.1:8000/cus?r=Kurunegala \
  --dart-define=TEST_ORDER_READY_NOTIFICATION=true
```

## Push notification follow-up

The status bridge above is intended for foreground/local testing. Reliable
alerts while the app is backgrounded or closed require a Firebase project. Add the Android
`google-services.json` and iOS `GoogleService-Info.plist`, then connect the FCM
device token to the signed-in `/cus` customer token. The Frappe server should
send a high-priority notification when `POS Order.kitchen_status` changes to
`Ready`. Firebase credentials must stay outside git.
