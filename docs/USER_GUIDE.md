# LuuvCRM — User Guide

Everything the restaurant's staff need, screen by screen. Written for people who have never used a POS before.

---

## Who uses what

| Role | Screen | Device |
|---|---|---|
| Cashier | `/pos` | Counter tablet or PC |
| Waiter | `/waiter` | Phone |
| Kitchen | `/kitchen` | Kitchen screen or tablet |
| Customer at the table | `/kiosk` | Shared tablet |
| Customer at home | `/cus` | Their own phone |
| Manager / owner | `/manager` | Phone |
| Purchaser, or a manager | `/purchases` | Phone |
| Everything else | `/admin` | Any |

Everyone signs in at `/pos-login` with the email and password the manager creates. **Never share one login between two people** — the activity trail and the sales reports are keyed to who was signed in.

Add `/pos`, `/waiter`, `/manager` and `/purchases` to the phone's home screen (Chrome → ⋮ → *Add to Home screen*). They then open full-screen like ordinary apps.

---

## 1. Cashier — `/pos`

Three tabs along the bottom: **Sale**, **Tables**, **Orders**.

### Opening the shift
The first thing every day. The POS asks for the opening cash float — count the drawer and type the real number. Nothing can be sold until the shift is open, and the closing report is measured against this figure.

### Taking a sale
1. **Sale** tab. Tap items to add them; tap again to increase the quantity.
2. Choose **Dine In**, **Takeaway** or **Delivery** at the top. This matters — the service charge applies to dine-in only, and takeaway lines are marked for the kitchen.
3. For dine-in, pick the table.
4. Customer name and phone are optional, but the phone is what links the sale to loyalty and lets the customer track the order.
5. **Send to Kitchen** prints the KOT and starts the cooking. **Pay** closes the bill.

### Payment
- **Cash** — type what the customer handed over; the change is calculated for you.
- **Card**
- **Cash + Card** — enter the cash part; the rest goes on card.

The receipt prints automatically. **Print Receipt** in the success box reprints it.

### Tables
A live floor plan. Green is free, orange has an open bill. Tap a table to see its bill, add items, or take payment. If a party moves, use **Move table** rather than starting a new bill.

### Orders
Everything still open. Reprint a receipt, reprint a KOT, or cancel — cancelling asks for a reason and the manager sees it.

### Closing the shift
End of day: count the drawer, enter the amount, and the POS shows expected vs counted with the difference. Submit it. **Do this every single day** — the manager's cash reports are built from closing entries.

### If something needs the manager
Discounts above the allowed limit, and cancelling a paid bill, raise an **approval request**. The manager's phone buzzes; the POS waits and continues as soon as they tap approve. The manager can add a note, which stays on the record.

---

## 2. Waiter — `/waiter`

Two tabs: **New Order** and **My Orders**.

### Taking an order
Pick the table, tap through the menu, tap **Place Order**. It goes straight to the kitchen — no trip to the counter.

**Item notes** are the important feature: tap a line and type "no chilli", "extra spicy", "no onion". It prints on the KOT.

### Adding to a running table
Open the table in **My Orders** and add the new items. Only the new lines print on the kitchen ticket — the kitchen never sees a duplicate of what it has already cooked.

### My Orders
Today's orders that you took, with live status: Pending → Processing → Ready → Served. When you carry a plate out, tap **Served**.

---

## 3. Kitchen — `/kitchen`

One screen, refreshing by itself every 10 seconds. Each card is an order: table, time waiting, and the items with their notes.

Three taps as the food moves:
- **Start** → the waiter sees "Processing"
- **Ready** → the waiter is told to collect
- **Served** → done, the card clears

Cards are colour-coded by kitchen group (Kottu, Rice & Noodles, Curries, Biryani, Pizza, Beverages, Coffee & Tea, Desserts, Sides & Starters), so the right station finds its work at a glance. **KOT** on any card reprints the ticket.

Takeaway lines are marked — pack, don't plate.

---

## 4. Self-order kiosk — `/kiosk`

A tablet at the table or the entrance. The customer browses, adds to a cart, enters name and phone, and confirms. The order lands in the kitchen and on the cashier's Orders tab like any other. Payment happens at the counter.

---

## 5. Customer app — `/cus`

The customer's own phone.

- Browse the menu and order for pickup, delivery, or a scheduled time
- Register with a phone number and a 4-digit PIN
- **My Orders** — live status and full history
- **Track** (`/track`) — follow one order without signing in
- Loyalty points, referral codes and rewards where those are switched on

Customers reach it from the QR code on the table, from `/menu`, or from a WhatsApp link.

---

## 6. Manager — `/manager`

The owner's phone app. Four tabs.

### Today
Live, all day: revenue, number of sales, average bill, the payment mix (how much cash vs card), and revenue by hour so you can see your rush.

### Cash
The drawer. Opening float, cash sales, cash in and cash out, expected balance. Record a cash movement whenever money leaves or enters the drawer outside a sale — paying a supplier, a staff advance, a bank drop — with a note. This is what makes the end-of-day count meaningful.

### Approvals
Requests waiting on you: big discounts, cancellations, purchase bills. Each shows who asked, for what, and how much. Approve or reject, with an optional comment. **Check this often** — the cashier is standing still until you answer.

### More
- **Daily sales** — day by day, with your best day marked
- **Monthly sales** — month over month
- **Bills** — every bill, searchable, drill into any one to see its lines
- **Purchases** — what was bought, from whom, for how much
- **Food cost** — purchases plus wastage against revenue, the number that tells you whether you are actually making money
- **Menu** — change a price or add an item without opening the Desk
- **Guides** — tour-guide commissions, balances and payouts
- **Activity** (`/activity`) — every staff action, filterable, with anything unusual flagged
- **Errors** (`/errors`) — technical faults, for the developer

There is a PIN on the manager app, but the real protection is your login — **do not leave a manager session signed in on a shared phone**.

---

## 7. Purchases — `/purchases`

Five tabs.

### List
Every supplier bill. Search across all of them by supplier, item or bill number.

### Due
Bills not yet paid. Tap **Mark paid** when you settle one, so the total owed stays honest.

### Approve
Bills waiting for approval, **one line at a time** — you see each item, its quantity and its price before it counts. Add a comment if you want to record why. The person who entered a bill cannot approve their own.

### Expenses
Non-stock costs: electricity, water, gas, rent, internet, salaries, repairs, transport, cleaning, licences. Pick the category, enter the amount, add a note. These feed the food-cost report.

### Prices
The price book — what each ingredient last cost and from whom, so you can see when a supplier's price moves. When an ingredient is marked **Stock item — counted**, approving its bill also receives that quantity into the branch warehouse.

### Wastage
Record spoiled or dropped stock with a reason. Uncomfortable, and worth it: wastage is invisible money, and food cost is wrong without it.

### Ingredient stock and menu recipes

This needs one setup pass before the first stock-controlled sale:

1. In `/admin` → **Branches**, make sure the restaurant has a leaf Warehouse.
2. In `/admin` → **Items**, open each ingredient, choose **Stock item — counted**, and set its stock unit (Kg, Litre, Nos, and so on).
3. Enter the real opening quantity with ERPNext **Stock Reconciliation**. Old LuuvCRM purchase bills are history; enabling stock does not invent an opening balance from them.
4. In `/admin` → **Menu**, open a dish. Under **Recipe (BOM)** add what one serving consumes and save the recipe.

After that, approving a new purchase creates a native **Material Receipt**. Sending a menu item to the kitchen creates a native **Material Issue** from its default BOM. Adding more items consumes only the added quantity; printing or paying twice does not deduct twice. A made-to-order item with no recipe continues to sell without moving stock, which lets recipes be introduced gradually. A stock item sold directly, such as a bottled drink, deducts itself without a BOM.

Food made ahead—naan, cooked chicken, sauces, dough, and similar prep—is also a stock item. Create it under `/admin` → **Items** as **Stock item — counted**, reopen it, and add its **Preparation recipe**. Enter the quantity the kitchen made and press **Prepare batch**: the recipe ingredients leave stock and the prepared item enters stock. A final menu BOM can mix prepared stock and raw ingredients; for example, naan roti biriyani can consume one prepared naan, prepared chicken, raw onion, and sauce. At sale time the prepared items themselves are deducted, not their raw materials a second time.

The same add/update controls are under `/purchases` → **BOM** for the staff who maintain buying items. Search for either a prepared item or a final dish, open it, edit the ingredients, and save. Stock items with a saved BOM also show **Prepare stock batch** there.

If ERPNext reports insufficient stock, correct the physical count or purchase receipt; do not enter a fake purchase just to clear the warning. Cancelling food after it has reached the kitchen does not put ingredients back—the food has already been consumed and should be handled as wastage.

---

## 8. WhatsApp, promos and guides

Optional features the manager switches on.

- **WhatsApp ordering** — the customer messages the restaurant, confirms an order in the chat, and it appears in the POS.
- **QR promo stickers** (`/qrpromo`, `/claim`) — printed sticker batches. The customer scans, sends a WhatsApp message with the code, and claims a free meal. The manager sees how many were issued, registered and redeemed.
- **Tour guides** (`/guide`) — a guide brings customers and earns a percentage back. They sign in with their WhatsApp number and a password, and see their earnings and payouts. The manager records payouts from the manager app.
- **Loyalty** — points on spend, referral codes, redemption at the till.

---

## 9. Daily rhythm

**Opening**
1. Cashier opens the shift with a counted float.
2. Kitchen screen on.
3. Waiters signed in on their own phones.

**During service**
- Waiters take orders on their phones; the kitchen sees them instantly.
- The cashier takes walk-ins and handles all payment.
- The manager keeps the Approvals tab in reach.

**Closing**
1. All open bills settled or cancelled with a reason.
2. Cashier counts the drawer and submits the closing entry.
3. Manager checks Today, Cash and the day's approvals.
4. Enter the day's supplier bills and expenses in `/purchases`.

---

## 10. When something goes wrong

| Problem | Do this |
|---|---|
| Prices show as 0 | The item has no price in the price list — manager fixes it in **More → Menu** |
| Order not reaching the kitchen | Check the kitchen screen's internet; it retries by itself. Reprint the KOT from the POS. |
| Printer silent | Check paper and power, then the print app on the tablet. Reprint from **Orders**. |
| Cashier stuck waiting on approval | The manager has not tapped yet — call them |
| "Order is on another table" | Someone moved the table underneath you. Refresh and try again. |
| Wrong change given | Do not edit a paid bill. Record it as a cash movement with a note, and tell the manager. |
| Screen frozen | Pull down to refresh. If it persists, close and reopen from the home screen. |

**Golden rules**
- One login per person, always.
- Never edit a paid bill — request a cancellation instead.
- Close the shift every day.
- Enter wastage honestly; the food-cost number is worthless otherwise.
