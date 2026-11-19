# Temple Seva Booking & Donation App

QR scan → pick sevas (single amount, multiple amount tiers, or bundled combo
packages) → name/phone/remarks → payment gateway or manual UPI confirmation
→ receipt with Print → admin: manage sevas, pricing, combo packages, staff
roles, editable receipt wording, date-range reports.

## Run locally

**One-time system requirement (Windows only):** PDF receipts are generated
with WeasyPrint, which needs the GTK3 runtime (Pango/Cairo/GDK-Pixbuf) for
correct Marathi text rendering. This is separate from `pip install`:

1. Download the latest `gtk3-runtime-...-installer.exe` from:
   https://github.com/tschoonj/GTK-for-Windows-Runtime-Environment-Installer/releases
2. Run it (default options are fine), then restart your terminal

(On Linux, install via `apt-get install libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0`.)

```bash
pip install -r requirements.txt
python app.py
```

Open `http://127.0.0.1:5000`, register a temple, log in.

## Sevas: single price, multiple tiers, or combo packages

- **Manage Sevas** → add a seva with one default amount (simple case)
- **Add Amount Option** → give a seva several selectable price tiers
  (e.g. ₹20 / ₹51 / ₹151 / ₹201 / ₹1001) — devotees then pick from a dropdown
  instead of a single fixed price
- **Create Combo Package** → bundle several sevas into one selectable
  package with a combined total (e.g. "Ganeshotsav Combo" = Archana +
  Satyanarayan Pooja). Selecting a package on the booking page adds all its
  bundled sevas to the cart, and the receipt itemizes each one individually.
- The booking page always shows a live breakdown of exactly which sevas will
  be performed for whatever's currently selected, before the devotee pays.
- Server-side validation rejects any submitted amount that isn't one of a
  seva's actual configured prices, so the price can't be tampered with from
  the browser.

## User roles

- **Admin** (temple owner login, always) — full access
- **User** — dashboard, reports, counter (no settings/sevas/user management)
- **Printing User** — counter/print screen only, nothing else reachable
Manage staff accounts under **Users** in the dashboard.

## Payment confirmation: what's actually possible

- **With Razorpay configured** (Temple Settings → Razorpay Gateway): payment
  success/failure is verified automatically via cryptographic signature
  check before a receipt is ever created — this is the only way this app
  (or any web app) can *automatically* confirm a payment succeeded.
- **Without a gateway** (manual/UPI-at-counter mode): there is no technical
  way for any website to automatically detect a UPI transfer completing,
  since UPI apps don't notify arbitrary third-party websites. This isn't a
  limitation of this app specifically — it's how UPI works. In this mode,
  staff must manually verify the payment (check the bank/UPI app for the
  credit matching the amount/phone) before treating the booking as paid.

## Receipt wording

Fully editable from **Receipt Text** in the dashboard — use `{NAME}` as a
placeholder for the devotee's name. A live preview and a "Generate Sample
PDF" button let you check exact wording before it goes live. Once a receipt
is created, its wording is locked in permanently (matching a real paper
receipt) — editing the template only affects future bookings.

## Receipt format

6x4 inch PDF in Marathi (तारीख / पावती नंबर / श्री-श्रीमती / seva table /
amount in words), rendered via WeasyPrint (not simpler PDF libraries)
specifically because it correctly shapes Devanagari conjuncts (e.g. "श्री")
the same way a browser does.

Requires the bundled fonts at `static/fonts/NotoSansDevanagari-Regular.ttf`
and `-Bold.ttf` — don't delete them.

## QR code

By default uses whatever URL you're viewing the admin panel from. If that's
ever wrong (e.g. shows "localhost" when testing via GitHub Codespaces or
similar), set **Public Base URL** in Temple Settings to override it.

## Deploying

Includes a `Procfile` (`gunicorn app:app`). Set `SECRET_KEY` env var in
production. If deploying to a Linux host and the build fails mentioning
`libpango`/`libgobject`, add a build step:
`apt-get install -y libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0`
before `pip install -r requirements.txt`.
