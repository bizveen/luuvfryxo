# LuuvCRM — Restaurant Management Platform
## Proposal

**Prepared:** September 2026
**Version:** 1.0

> **Note to whoever sends this:** every figure in square brackets — prices, timelines, contact details — is a placeholder. Fill them in before this goes to a client. Everything else describes the software as it actually exists today.

---

## 1. The problem

Most restaurants run on four disconnected systems: a till that only rings up sales, a paper KOT book, a WhatsApp group for orders, and a notebook for supplier bills. Nobody knows the real food cost until the accountant says so, three months late. The owner cannot see today's takings without being in the building.

LuuvCRM replaces all four with one system that the whole restaurant works inside — counter, floor, kitchen, office and the customer's own phone.

---

## 2. What it does

### Point of sale
A tablet or PC at the counter. Dine-in, takeaway and delivery, with the service charge applied only where it should be. Cash, card, or a split of the two, with change calculated. Shift open and close with a counted drawer, so the day reconciles instead of being guessed at. Thermal receipt and kitchen-ticket printing on 58 mm or 80 mm rolls.

### Waiter app
Runs on the waiter's own phone. Orders go from the table straight to the kitchen — no walk to the counter, no handwritten slip to lose. Per-item notes ("no chilli", "extra spicy") print on the ticket. More items can be added to a running table, and the kitchen only ever sees the new lines.

### Kitchen display
One screen, colour-coded by station, refreshing itself. Start → Ready → Served, so the floor knows the state of every plate without asking. Takeaway lines are flagged for packing.

### Self-order kiosk
A tablet the customer orders from directly. Straight to the kitchen; payment at the counter.

### Customer app
The customer's phone. Menu, ordering for pickup, delivery or a scheduled time, live order tracking, order history, and a loyalty scheme with points and referrals. Reached from a QR code on the table or a WhatsApp link — nothing to install.

### Manager app
The owner's phone. Today's revenue, sale count, average bill, payment mix and revenue by hour, live. The cash drawer with every movement in and out. Daily and monthly sales. Every bill, searchable, drillable to the line. Approvals for large discounts and cancellations, so nothing unusual happens without the owner knowing — wherever they are.

### Purchasing and cost control
Supplier bills entered against a price book, so a rising ingredient price is visible the week it moves. Line-by-line approval — and the person who entered a bill cannot approve it. Outstanding-payments tracking. Expenses by category. Wastage recording. All of it rolls into a **food-cost report**: purchases plus wastage against revenue, the single number that says whether the kitchen is profitable.

### WhatsApp, promotions and partners
Ordering and order confirmation over WhatsApp. QR promotion sticker campaigns with issue, registration and redemption tracked. A tour-guide commission scheme with its own portal, balances and payout records.

### Accountability
Every staff action is recorded — who changed a price, who cancelled a bill, who approved what, from which device. Unusual actions are flagged for review. The owner has an answer to "what happened on Tuesday" without asking anyone.

---

## 3. Built on ERPNext

LuuvCRM is not a closed box. It sits on **ERPNext**, the open-source ERP used by tens of thousands of businesses worldwide.

That means the restaurant also gets, in the same system, at no extra licence cost: full double-entry accounting, inventory and stock, purchasing and suppliers, payroll and HR, and CRM. And it means:

- **No per-user licence fees.** Add staff without adding cost.
- **No vendor lock-in.** The data is in a standard database, exportable at any time.
- **No dead end.** The platform is maintained by a global community, independently of us.

The restaurant starts with what it needs today and grows into the rest when it is ready.

---

## 4. What is delivered

| | Included |
|---|---|
| **Software** | Every module in §2, installed and configured for the restaurant |
| **Setup** | Menu, prices, tables, staff accounts, roles, printers |
| **Data** | Existing menu and customer data imported |
| **Training** | On-site training for cashiers, waiters, kitchen and management |
| **Documentation** | Full user guide, in the staff's language if required |
| **Support** | [30/60/90] days included after go-live |
| **Hosting** | On the restaurant's own server, or hosted by us — the client's choice |

### Hardware — not included, specified here

| Item | Spec | Qty | Approx |
|---|---|---|---|
| Counter tablet or PC | Android 10+ / any modern browser | 1 per till | [—] |
| Thermal printer | 58 mm or 80 mm, Bluetooth or USB | 1–2 | [—] |
| Kitchen display | Any tablet or monitor | 1 per station | [—] |
| Waiter phones | Staff's own phones work | — | Rs. 0 |
| Server | 4 GB RAM, 2 vCPU, 40 GB — on-site or cloud | 1 | [—] |
| Network | Reliable WiFi covering floor and kitchen | — | [—] |

Waiters using their own phones is deliberate: it is the single largest hardware saving in the system.

---

## 5. Delivery plan

| Phase | Work | Duration |
|---|---|---|
| **1 · Discovery** | Walk the restaurant, map the current flow, agree the menu structure | [1 week] |
| **2 · Setup** | Install, configure, import the menu, create accounts, connect printers | [1–2 weeks] |
| **3 · Training** | Train each role on their own screen, on their own device | [1 week] |
| **4 · Parallel run** | Old and new side by side for a few days — nothing is switched off until the staff are comfortable | [1 week] |
| **5 · Go-live** | Full cutover, on-site support for the first days | [1 week] |
| **6 · Support** | Remote support, fixes, adjustments | [30–90 days] |

**Total: [5–6 weeks] from signature to go-live.**

---

## 6. Commercials

*Placeholders — set before sending.*

| Option | What it covers | Price |
|---|---|---|
| **A · One-time licence** | Software, setup, training, [90] days support. Self-hosted. Source code escrow available. | **[Rs. ______]** |
| **B · Monthly** | Everything in A, hosted by us, with ongoing support and updates. | **[Rs. ______ / month]** |
| **C · Enterprise** | Multiple branches, consolidated reporting, custom modules. | **[Quoted]** |

**After the included support period:** [Rs. ______ / month] for support and updates, or [Rs. ______ / hour] ad-hoc.
**Custom development:** [Rs. ______ / hour].

---

## 7. Being straight about the current state

We would rather say this now than have it found later.

LuuvCRM has been running a live restaurant daily and is genuinely proven in service. It is not yet packaged as shrink-wrapped software: a full engineering audit (available on request) identified work needed before it can be installed by a third party unassisted — chiefly a proper automated installer, a set of security hardening fixes, and an automated test suite.

**What this means in practice:**

- **For a client we install and support ourselves:** none of it is blocking. We handle the installation; the security fixes ship before go-live either way.
- **For a client who wants the source and their own developers:** budget [6–10 weeks] of engineering to complete the packaging first. We will quote this separately and transparently.

The remediation plan is written, scoped and costed. We are not discovering this at handover.

---

## 8. Why this rather than the alternatives

| | LuuvCRM | Typical local POS | International cloud POS |
|---|---|---|---|
| Per-user licence | None | Per till | Per user, per month |
| Waiter app | Included | Extra | Extra |
| Kitchen display | Included | Extra | Extra |
| Customer ordering app | Included | Rarely | Extra |
| Purchasing + food cost | Included | Rarely | Extra |
| WhatsApp ordering | Included | No | No |
| Full accounting | Included (ERPNext) | No | No |
| Data ownership | Client's | Vendor's | Vendor's |
| Local support | Yes | Yes | No |
| Runs offline | Partially | Varies | No |
| Source code | Available | No | No |

The honest summary: the international products are more polished; the local ones are cheaper per screen. LuuvCRM is the one that covers the whole restaurant — floor, kitchen, purchasing and accounts — for one price, on hardware the restaurant mostly already owns, with the data staying with the client.

---

## 9. Next step

A [60]-minute demonstration at the restaurant, on the restaurant's own menu, so the staff can judge it against how they actually work.

**[Company]**
[Contact] · [Phone] · [Email]

---

### Appendix — technical summary

| | |
|---|---|
| Platform | Frappe v15 / ERPNext v15 |
| Language | Python 3.10+, JavaScript |
| Database | MariaDB |
| Front end | Progressive web apps — no installation, works on any modern phone |
| Server | 4 GB RAM, 2 vCPU, 40 GB minimum |
| Deployment | On-premise or cloud |
| Backups | Automated daily, off-site copies |
| Security | HTTPS, role-based access, encrypted credentials, full audit trail |
| Licence | MIT (application), GPL v3 (ERPNext) |
| Scale | Designed for a single restaurant; multi-branch on request |
