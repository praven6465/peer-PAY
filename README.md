# ChargeNear v2 — Bengaluru two-wheeler charging marketplace

A Flask MVP for a neighbourhood marketplace that connects two-wheeler EV riders with approved hosts who offer outdoor home charging.

## Product decisions implemented

- **Pilot:** Nagarbhavi, Bengaluru (configurable with `PILOT_LOCALITY`).
- **Vehicle segment:** electric two-wheelers only; riders enter the exact vehicle model.
- **Use cases:** emergency and everyday/planned charging.
- **Host location:** residential property with designated outdoor parking. A host's full address and phone are hidden until the host accepts the request.
- **Discovery:** browser location + straight-line Haversine radius search, default 1 km.
- **Pricing:** per kWh with separate electricity reimbursement and host service rate.
- **Verification:** newly submitted or edited host listings are not discoverable until an admin approves them.
- **Energy accounting:** host enters dedicated meter start/end readings; the final bill is calculated from the difference.
- **Booking flow:** requested → accepted → arrived → charging → completed (or rejected).

## Price model in v2

The host configures two rates:

1. Electricity reimbursement rate (₹/kWh).
2. Host service rate (₹/kWh).

The platform commission is currently set to **15% of the host service component only**. The electricity reimbursement is passed through to the host and is not commissionable in the prototype.

For delivered energy `E` kWh:

```text
Customer total = E × (electricity rate + host service rate)
Platform revenue = E × host service rate × 15%
Host payout = E × electricity rate + E × host service rate × 85%
```

Example only: at 2 kWh, an electricity rate of ₹8/kWh and service rate of ₹4/kWh:

- Customer total: ₹24.00
- Host payout: ₹22.80
- ChargeNear gross commission: ₹1.20

This is a transparent prototype formula, not a statement of the correct permitted retail tariff or tax treatment. The Ministry of Power's 2024 guidelines specify an AC slow-charging service-charge ceiling of ₹3/kWh during solar hours and ₹4/kWh during non-solar hours for Public and Community EV Charging Stations through 31 March 2028; whether a paid, booking-based residential host falls into that category must be determined for this exact operating model. The same guidelines say domestic electricity rates apply to EV charging at home, but that clause does not by itself resolve a paid third-party marketplace arrangement. Get written advice/clarification from BESCOM/KERC and qualified legal/electrical advisers on classification, tariff, service-charge ceilings, metering, invoicing, GST and payment-flow requirements before charging real customers. The form starts the host service rate at ₹0 for this reason.

## Run locally

Python 3.10+ is recommended.

```bash
python -m venv venv
# Windows: venv\\Scripts\\activate
# macOS/Linux: source venv/bin/activate
pip install -r requirements.txt

# Copy .env.example to .env, then replace BOTH placeholder values with unique random secrets.
# Windows PowerShell:
Copy-Item .env.example .env
# macOS/Linux:
# cp .env.example .env

# Generate two random values (run this command and copy its output into .env):
python -c "import secrets; print('SECRET_KEY=' + secrets.token_hex(32)); print('ADMIN_KEY=' + secrets.token_urlsafe(32))"

python app.py
```

Open `http://127.0.0.1:5000`. This version automatically loads `.env` from the same folder as `app.py`. If you change a key, stop Flask with Ctrl+C and start it again. The `.env` file is ignored by Git and must never be shared or uploaded publicly.

## First test flow

1. Register one account as a **Host** and another as an **EV rider**. Use different phone numbers.
2. Log in as the host and submit a listing. Test coordinates can be obtained from the map or browser geolocation.
3. Make sure the host enters the exact models to support, a fixed setup type, the measurement method, kWh rates, and checks all safety/permission declarations.
4. Log out. Open `/admin/login` and enter the `ADMIN_KEY` environment value. Review and approve the host. The admin route is disabled (404) if `ADMIN_KEY` is not configured.
5. Log in as the rider, click **Use my location**, and search. For testing, the rider and host need to be within 1 km of one another; alternatively use manual coordinates.
6. Request emergency or everyday charging. The host accepts. The rider marks arrival. The host starts charging and completes the session with start/end meter readings.
7. The app calculates the final total, host payout and platform commission. **No money is collected or transferred.**

## PythonAnywhere deployment

1. Create a PythonAnywhere account and open **Web** → **Add a new web app**. Choose Flask or Manual configuration with a supported Python version.
2. Upload this project folder to `/home/YOUR_USERNAME/chargenear_v2` (or clone it from your private Git repository).
3. Open a Bash console and install dependencies in the virtual environment associated with your web app:

   ```bash
   pip install -r /home/YOUR_USERNAME/chargenear_v2/requirements.txt
   ```

4. In the **Web** tab, open the WSGI configuration file and set the project path/import. Add your secrets there without committing them to a public repository:

   ```python
   import os
   import sys

   path = '/home/YOUR_USERNAME/chargenear_v2'
   if path not in sys.path:
       sys.path.insert(0, path)

   os.environ['SECRET_KEY'] = 'YOUR_LONG_RANDOM_SECRET'
   os.environ['ADMIN_KEY'] = 'A_DIFFERENT_LONG_ADMIN_SECRET'
   os.environ['PILOT_LOCALITY'] = 'Nagarbhavi, Bengaluru'
   os.environ['COOKIE_SECURE'] = '1'

   from app import app as application
   ```

5. Ensure the app's `instance` directory is writable by your account. The SQLite database is stored there by default. You can set `DATABASE_PATH` to a persistent writable path if needed.
6. Set the same Python version in your web app and virtual environment, then press **Reload** in the Web tab.
7. Visit `https://YOUR_USERNAME.pythonanywhere.com`.

PythonAnywhere uses the WSGI import; do not run Flask's development server in the WSGI file. The `app.run()` block is guarded and only runs when `app.py` is executed directly.

## Prototype boundaries — do not skip before a real pilot

- **No payment integration:** no UPI/QR checkout, card collection, refunds, GST invoicing, escrow, split transfers or host payout automation.
- **No actual meter integration:** the host manually enters start/end readings. The app trusts those entries; it does not talk to a certified sub-meter or smart charger.
- **No KYC or site inspection upload:** host approval is an admin workflow based on the submitted data and declarations. It does not replace an on-site inspection, electrical test, identity verification or property permission.
- **No dynamic map directions:** the map uses Leaflet + OpenStreetMap tiles and plots approximate host pins. Distance is straight-line, not road distance.
- **No SMS/OTP, push alerts, ratings, dispute handling, no-show fees, refunds or cancellation rules yet.**
- **Not production hardened:** add formal privacy/terms, independent security review, rate limiting, production logging, backups, monitoring, data retention/deletion, operational support and appropriate hosting before public launch.
- **Commercial/legal check:** determine whether and how a paid third-party home charging marketplace is treated under the applicable Ministry of Power guidelines, Karnataka tariff orders, BESCOM connection conditions, building/society rules, tax and consumer law. Do not assume that rules for personal domestic charging automatically settle the rules for a paid public-facing service.

## Electrical safety baseline

The Central Electricity Authority's 2023 safety regulations contain additional EV charging-station requirements, including protective devices, socket placement and restrictions on extension sets, adaptors and portable socket outlets. Re-check the latest consolidated rules and amendments before launch. The exact setup should be reviewed by a qualified electrical professional and checked against the latest applicable standards and local requirements. ChargeNear should not encourage hosts to offer a normal household extension board as an EV charging station.

## Suggested next upgrades

1. Photo/document upload plus verified host ID and a physical electrical/site inspection checklist.
2. Automated smart-meter/EVSE reading instead of manual readings.
3. Razorpay Route or another approved marketplace payment/payout product after legal and provider onboarding review.
4. SMS/WhatsApp alerts, cancellation/no-show policies, refunds, ratings and incident reporting.
5. Android apps only after the one-locality pilot proves host participation and repeat usage.
