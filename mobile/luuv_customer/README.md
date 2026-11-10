# Luuv Customer

Flutter shell for the Luuv Kurunegala customer portal. The app keeps the
existing `/cus` ordering, account, order history, and loyalty experience in a
persistent native WebView.

## Run

```bash
flutter pub get
flutter run
```

The release default portal is:

```text
https://luuvgrand.com/cus?r=Kurunegala
```

Debug builds default to the development PC on the local network:

```text
http://192.168.1.11:8000/cus?r=Kurunegala
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
- The signed-in customer session is securely paired with the native FCM app
  installation. The kitchen's `Ready` action can therefore alert the customer
  while the app is open, backgrounded, or closed.
- Foreground alerts use native Android/iOS notification UI. Tapping an order
  alert opens Orders; tapping an offer or promotion opens Offers.
- A local eight-second order-status check remains as a foreground fallback and
  duplicate Ready alerts are suppressed.

For local Android development on the same Wi-Fi network, run the Frappe server
on port 8000 and start the debug app. Override the IP if the development PC's
DHCP address changes:

```bash
flutter run \
  --dart-define=CUSTOMER_PORTAL_URL=http://192.168.1.11:8000/cus?r=Kurunegala \
  --dart-define=TEST_ORDER_READY_NOTIFICATION=true
```

USB port reversal remains available when the phone is not on the same network:

```bash
adb reverse tcp:8000 tcp:8000
flutter run \
  --dart-define=CUSTOMER_PORTAL_URL=http://127.0.0.1:8000/cus?r=Kurunegala \
  --dart-define=TEST_ORDER_READY_NOTIFICATION=true
```

## Firebase setup

The customer app is registered in the dedicated Firebase project:

```text
Project name: Luuv
Project ID: gen-lang-client-0437285569
Project number: 944300833041
```

The Android and iOS native app configuration files are included in this app.
Run `flutterfire configure` only when adding another platform or changing the
Firebase project.

Firebase app config is intentionally environment-specific and ignored by git.
Log in, choose/create the Firebase project, and run FlutterFire from this
directory:

```bash
firebase login
~/.pub-cache/bin/flutterfire configure \
  --project=gen-lang-client-0437285569 \
  --platforms=android,ios
```

Use these registered app identifiers:

- Android: `com.luuvgrand.luuv_customer`
- iOS: `com.luuvgrand.luuvCustomer`

The generated native files must end up at:

```text
android/app/google-services.json
ios/Runner/GoogleService-Info.plist
```

For iOS, enable Push Notifications and Background Modes > Remote notifications
for the same App ID in Apple Developer/Xcode, and upload an APNs authentication
key in Firebase Console > Project settings > Cloud Messaging.

### Frappe sender credentials

Download a Firebase Admin service-account JSON and keep it in the site's
private directory (never in this repository), then configure the site:

```bash
cp /secure/path/firebase-service-account.json \
  /home/pc/frappe-bench/sites/test.localhost/private/firebase-service-account.json

bench --site test.localhost set-config \
  firebase_service_account_file private/firebase-service-account.json
bench --site test.localhost set-config \
  firebase_project_id gen-lang-client-0437285569
bench restart
```

Kitchen `Ready` now queues an HTTP v1 notification automatically. Signed-in
customer devices are stored in the `Customer Push Token` DocType; expired FCM
installations are disabled after Firebase reports them as unregistered.

### Offers and promotions

Managers can queue an offer/promotion through the authenticated endpoint
`luuvcrm.api.queue_customer_push_broadcast` with `kind`, `title`, `body`, and an
optional `restaurant`. The notification opens the Offers tab.

Firebase Console campaigns can target either of these subscribed topics:

- `luuv_customers` — every configured customer app
- `luuv_<restaurant>` — one branch, normalized to lowercase; for example,
  `luuv_kurunegala`

For a Console campaign, add custom data `type=offer` (or `promotion`) and
`route=/cus?r=Kurunegala&tab=offers` so a tap opens the correct tab.
