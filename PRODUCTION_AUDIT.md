# GROWCAD — COMPLETE PRODUCTION AUDIT REPORT
# Generated: 2026-05-11
# ─────────────────────────────────────────────────────────────────────────────
# FORMAT: CATEGORY → FINDING → PRIORITY → FILE/LOCATION → FIX
# ─────────────────────────────────────────────────────────────────────────────

════════════════════════════════════════════════════════
SECTION 1: LAUNCH BLOCKERS (fix before going live)
════════════════════════════════════════════════════════

LB-01  PAYMENT TRUST MODEL IS BACKWARDS
       ─────────────────────────────────
       Priority: CRITICAL | File: server.py → select_plan()

       PROBLEM:
         Frontend calls /institute/select-plan AFTER Razorpay checkout.handler()
         fires. The handler only receives client-side confirmation — nothing
         stops an attacker from calling /institute/select-plan with a fake
         razorpay_payment_id and getting activated for free.

       FIX:
         1. Frontend calls /payments/create-order → gets order_id
         2. Razorpay checkout fires
         3. On success, frontend calls /institute/select-plan with payment_id + order_id
         4. Backend fetches payment from Razorpay API server-side and verifies amount
         5. Razorpay WEBHOOK is the authoritative activation (even if step 3-4 fails)
         
         Code: See backend_fixes.py → /payments/webhook + /institute/select-plan

LB-02  NO RAZORPAY WEBHOOK VERIFICATION
       ────────────────────────────────
       Priority: CRITICAL | File: server.py (missing entirely)

       PROBLEM:
         Without webhook verification, payment state depends entirely on frontend
         callbacks which can be intercepted, replayed, or fabricated.

       FIX:
         POST /payments/webhook with HMAC-SHA256 signature verification.
         Code: See backend_fixes.py → razorpay_webhook()
         
         Configure in Razorpay Dashboard:
           URL: https://api.growcad.in/api/payments/webhook
           Secret: store in RAZORPAY_WEBHOOK_SECRET env var
           Events: payment.captured, payment.failed, order.paid

LB-03  OTP USES random.randint (NOT CRYPTOGRAPHICALLY SECURE)
       ────────────────────────────────────────────────────────
       Priority: CRITICAL | File: server.py → _generate_otp()

       PROBLEM:
         random.randint uses Mersenne Twister — predictable after observing
         enough outputs. An attacker who receives multiple OTPs can predict
         future ones.

       FIX (2 lines):
         import secrets
         def _generate_otp(length=6):
             return "".join(str(secrets.randbelow(10)) for _ in range(length))

LB-04  OTP HASH IS PLAIN SHA256 — TIMING ATTACK VULNERABLE
       ──────────────────────────────────────────────────────
       Priority: CRITICAL | File: server.py → _otp_hash() + _consume_otp()

       PROBLEM:
         - hashlib.sha256(otp.encode()) without a secret key can be brute-forced
           offline if the otp_verifications collection leaks
         - rec["otp_hash"] != _otp_hash(otp) is NOT timing-safe

       FIX:
         def _otp_hash(otp):
             return hmac.new(JWT_SECRET.encode(), otp.encode(), hashlib.sha256).hexdigest()
         
         def _otp_compare(otp, stored_hash):
             return hmac.compare_digest(stored_hash, _otp_hash(otp))

LB-05  OTP RATE LIMITER IS IN-MEMORY (otp_rate_limit = {})
       ─────────────────────────────────────────────────────
       Priority: CRITICAL | File: server.py → check_otp_rate()

       PROBLEM:
         - Resets to empty dict on every deploy → rate limits bypass on redeploy
         - Useless with 2+ backend instances (each has its own empty dict)
         - Attacker can hammer OTP endpoint from different IPs

       FIX:
         Replace with MongoDB TTL collection (db.otp_rate_limits).
         Code: See backend_fixes.py → _check_otp_rate()

LB-06  bcrypt ROUNDS TOO LOW
       ──────────────────────
       Priority: CRITICAL | File: server.py → hash_pw()

       PROBLEM:
         bcrypt.gensalt() defaults to rounds=12 on some versions but 10 on others.
         Explicit rounds=10 is 4× faster to crack than rounds=12.

       FIX:
         def hash_pw(pw): return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=12)).decode()

LB-07  NO SUBSCRIPTION EXPIRY JOB
       ────────────────────────────
       Priority: CRITICAL | File: server.py (missing)

       PROBLEM:
         Subscriptions never expire automatically. An institute that stops paying
         continues to have full access forever.

       FIX:
         Daily background job marks subscriptions past renewsAt as "expired".
         Code: See backend_fixes.py → _subscription_expiry_loop()

LB-08  auth() DEPENDENCY DOES NOT ENFORCE TENANT ISOLATION
       ─────────────────────────────────────────────────────
       Priority: CRITICAL | File: server.py → auth()

       PROBLEM:
         Original: db.users.find_one({"id": p["uid"], "instituteId": p.get("iid")})
         If iid claim is None (malformed token), query becomes {"id": x, "instituteId": None}
         which matches NO document — but the bigger issue is there's no iid claim
         validation. A token with iid=A can in theory call institute B's endpoints
         if route-level checks are missing.

       FIX:
         Always require both uid and iid. Validate iid claim is non-empty.
         Add TOKEN_VERSION claim for mass invalidation.
         Code: See backend_fixes.py → auth()

LB-09  DUPLICATE PAYMENT POSSIBLE (no idempotency)
       ──────────────────────────────────────────────
       Priority: CRITICAL | File: server.py → select_plan()

       PROBLEM:
         If user double-clicks "Pay" or network retry fires, two subscriptions
         get inserted for the same institute. Also two Razorpay orders get created.

       FIX:
         1. Check for existing active subscription before create-order
         2. Unique index on subscriptions.razorpayPaymentId
         3. DuplicateKeyError catch returns idempotent success
         Code: See backend_fixes.py → create_razorpay_order() + select_plan()

LB-10  WEBHOOK REPLAY ATTACK — NO IDEMPOTENCY KEY
       ─────────────────────────────────────────────
       Priority: CRITICAL | File: missing webhook handler

       PROBLEM:
         Razorpay retries webhooks on non-200 responses. Without deduplication,
         each retry re-activates/re-processes the payment.

       FIX:
         webhook_events collection with unique index on razorpayEventId.
         TTL index auto-cleans after 30 days.
         Code: See backend_fixes.py → razorpay_webhook()


════════════════════════════════════════════════════════
SECTION 2: SECURITY ISSUES
════════════════════════════════════════════════════════

S-01   TenantMiddleware HITS DB FOR EVERY REQUEST
       ─────────────────────────────────────────────
       Priority: CRITICAL | File: server.py → TenantMiddleware

       PROBLEM:
         /auth/send-otp, /auth/admin-signup, /institute/create, /institute/check-slug
         all carry the X-Institute-Slug header (the frontend sends it for all requests
         via the Axios interceptor). The middleware hits MongoDB for EVERY one,
         and returns 404 for auth/institute routes because the institute doesn't
         exist yet during onboarding. This also means 1 extra DB round-trip on
         every authenticated API call.

       FIX:
         Exempt /api/auth/, /api/institute/, /api/seed, /api/payments/webhook.
         Code: See backend_fixes.py → TenantMiddleware

S-02   JWT NO EXPIRY ENFORCEMENT ON SENSITIVE WRITES
       ─────────────────────────────────────────────
       Priority: IMPORTANT | File: server.py

       PROBLEM:
         48-hour JWT is fine for reads. But payment operations, institute creation,
         and plan changes should require a recent token (short re-auth window).

       FIX (optional but recommended):
         Check iat claim on payment routes — if token is > 2 hours old, return
         401 with "SESSION_STALE" error code, redirect to re-login.
         
         async def require_fresh_token(user=Depends(auth), cred=Depends(security)):
             p = jwt.decode(cred.credentials, JWT_SECRET, algorithms=[JWT_ALGO])
             if time.time() - p.get("iat", 0) > 7200:  # 2 hours
                 raise HTTPException(401, "Session expired for sensitive operation.")
             return user

S-03   /auth/subdomain-login USES UNESCAPED REGEX
       ──────────────────────────────────────────────
       Priority: CRITICAL | File: server.py → subdomain_login()

       PROBLEM:
         {"$regex": mobile[-10:]} — if mobile contains regex chars like . or *
         it becomes a MongoDB regex injection.

       FIX:
         import re
         safe_mobile = re.escape(mobile[-10:])
         {"$regex": safe_mobile}

S-04   /institute/create RACE CONDITION ON SLUG
       ─────────────────────────────────────────
       Priority: CRITICAL | File: server.py → create_institute()

       PROBLEM:
         check → insert is not atomic. Two requests with same slug can both
         pass the check and both succeed (one will fail on the MongoDB unique
         index, but it throws an unhandled exception).

       FIX:
         Remove pre-check. Wrap insert_one in try/except DuplicateKeyError:
         
         try:
             await db.institutes.insert_one(institute)
         except DuplicateKeyError:
             raise HTTPException(409, "This slug is already taken.")
         
         Requires: unique index on institutes.slug (see backend_fixes.py)

S-05   /auth/admin-signup LEAKS USER EXISTENCE VIA TIMING
       ─────────────────────────────────────────────────────
       Priority: IMPORTANT | File: server.py → admin_signup()

       PROBLEM:
         Returning "An account with this email already exists" reveals PII.
         Attacker can enumerate which emails are registered.

       FIX:
         Return generic "If this email is not registered, you will receive an OTP."
         Only reveal duplicate error to the already-authenticated user.

S-06   ENV VARS NOT VALIDATED AT STARTUP
       ────────────────────────────────────
       Priority: CRITICAL | File: server.py → top

       FIX: See backend_fixes.py → Environment Validation section.

S-07   CORS_ORIGINS DEFAULTS TO "*"
       ──────────────────────────────
       Priority: CRITICAL | File: server.py → app.add_middleware(CORSMiddleware)

       PROBLEM:
         allow_origins="*" with allow_credentials=True is rejected by browsers
         and is also a security risk in production.

       FIX:
         CORS_ORIGINS = "https://growcad.in,https://app.growcad.in,https://*.growcad.in"
         # Store in .env. Wildcard subdomain needs NGINX-level matching,
         # not FastAPI CORS (FastAPI doesn't support *.domain.com in origins).
         # Use explicit list in production.

S-08   SEED ROUTE IS OPEN IN PRODUCTION
       ────────────────────────────────────
       Priority: CRITICAL | File: server.py → @api.post("/seed")

       FIX:
         @api.post("/seed")
         async def seed_endpoint():
             if os.environ.get("ENV") == "production":
                 raise HTTPException(404)
             return await do_seed()

S-09   PAYMENTS/CREATE-ORDER EXPOSES ADMIN EMAIL IN RAZORPAY NOTES
       ────────────────────────────────────────────────────────────────
       Priority: LOW — acceptable for internal notes, not displayed publicly.


════════════════════════════════════════════════════════
SECTION 3: ARCHITECTURE ISSUES
════════════════════════════════════════════════════════

A-01   TWO SEPARATE STARTUP EVENT HANDLERS
       ─────────────────────────────────────
       Priority: IMPORTANT | File: server.py

       PROBLEM:
         @app.on_event("startup") create_indexes() and @app.on_event("startup") startup()
         both registered. FastAPI runs both but this is confusing and order-dependent.
         do_seed() is called in startup but NOT in create_indexes — if indexes don't
         exist yet when seed runs, seed fails.

       FIX:
         One _startup() function that: 1) ensures indexes, 2) seeds, 3) starts jobs.
         Code: See backend_fixes.py → _startup()

A-02   select_plan IMMEDIATELY ACTIVATES WITHOUT PAYMENT VERIFICATION
       ─────────────────────────────────────────────────────────────────
       Priority: CRITICAL (see LB-01 above)

A-03   NO SUBSCRIPTION LIFECYCLE STATE MACHINE
       ─────────────────────────────────────────
       Priority: IMPORTANT

       MISSING STATES:
         pending → pending_payment → active → expired → cancelled
         pending → pending_approval (cash) → active | rejected

       FIX:
         Add renewsAt to subscriptions. Run expiry job daily.
         Add invoice collection on each renewal.
         See backend_fixes.py → _subscription_expiry_loop()

A-04   _teacher_dashboard USES asyncio.coroutine() (REMOVED IN 3.11)
       ─────────────────────────────────────────────────────────────────
       Priority: CRITICAL | File: server.py → _student_dashboard()

       FIX:
         async def _none(): return None
         Replace asyncio.coroutine(lambda: None)() with _none()

A-05   PERFORMANCE REPORT DOES N+1 QUERIES
       ─────────────────────────────────────
       Priority: IMPORTANT | File: server.py → performance_report()

       Lines 2083-2116: for each test → await db.marks.find + await db.batches.find_one
       This is O(n) DB calls inside a loop.

       FIX:
         Fetch all marks for all test IDs in one query. Build in-memory maps.
         Already done for other reports — apply same pattern here.

A-06   pending_reminders HAS N+1 QUERY LOOP
       ──────────────────────────────────────
       Priority: IMPORTANT | File: server.py → pending_reminders()

       Line 2225: for f in fees: student = await db.students.find_one(...)
       This is O(fees) DB calls.

       FIX:
         Bulk fetch all student IDs first, then use _get_students_map().

A-07   do_seed() RUNS ON EVERY STARTUP
       ────────────────────────────────
       Priority: IMPORTANT | File: server.py → startup()

       PROBLEM:
         await do_seed() in every startup call is harmless (it checks existing)
         but adds 5-10 DB round trips to every cold start.

       FIX:
         Move seed to a one-shot migration script (seed.py).
         Keep the endpoint for dev: POST /seed (guarded by ENV != production).

A-08   NO AUDIT LOGGING
       ──────────────────
       Priority: IMPORTANT

       MISSING: payment events, plan changes, admin approvals, login attempts,
       OTP failures, student/teacher creation.

       FIX: See backend_fixes.py → _audit() helper.
       Index on (instituteId, timestamp) and (action, timestamp).

A-09   MARKS UPLOAD USES N+1 FIND+UPDATE LOOP
       ──────────────────────────────────────────
       Priority: IMPORTANT | File: server.py → upload_marks()

       FIX (already fixed in previous session, verify it landed):
         ops = [UpdateOne({"testId": tid, "studentId": m["studentId"]},
                          {"$set": {"marksObtained": m["marksObtained"]},
                           "$setOnInsert": {"id": uid(), "instituteId": iid}},
                          upsert=True)
                for m in data.marks]
         await db.marks.bulk_write(ops)


════════════════════════════════════════════════════════
SECTION 4: MONGODB SCHEMA & INDEX IMPROVEMENTS
════════════════════════════════════════════════════════

INDEX-01  MISSING UNIQUE INDEX ON institutes.slug
          ──────────────────────────────────────────
          Priority: CRITICAL
          await db.institutes.create_index("slug", unique=True)

INDEX-02  MISSING UNIQUE INDEX ON subscriptions.razorpayPaymentId
          ──────────────────────────────────────────────────────────
          Priority: CRITICAL (enables duplicate payment detection)
          await db.subscriptions.create_index("razorpayPaymentId", unique=True, sparse=True)

INDEX-03  MISSING INDEX ON subscriptions.(status, renewsAt)
          ──────────────────────────────────────────────────────────
          Priority: IMPORTANT (for expiry job query)
          await db.subscriptions.create_index([("status", 1), ("renewsAt", 1)])

INDEX-04  MISSING UNIQUE INDEX ON webhook_events.razorpayEventId
          ──────────────────────────────────────────────────────────
          Priority: CRITICAL (idempotency)
          await db.webhook_events.create_index("razorpayEventId", unique=True)
          await db.webhook_events.create_index("processedAt", expireAfterSeconds=2592000)  # 30 days

INDEX-05  otp_verifications MISSING TTL INDEX
          ─────────────────────────────────────
          Priority: IMPORTANT (docs auto-clean)
          await db.otp_verifications.create_index("expiresAt", expireAfterSeconds=0)
          # Store expiresAt as a real datetime object, not ISO string

INDEX-06  marks MISSING UNIQUE COMPOUND INDEX
          ──────────────────────────────────────
          Priority: IMPORTANT (prevents duplicate mark entries)
          await db.marks.create_index([("testId", 1), ("studentId", 1)], unique=True)

INDEX-07  users MISSING (instituteId, role) INDEX
          ─────────────────────────────────────────
          Priority: IMPORTANT (subdomain login + role checks)
          await db.users.create_index([("instituteId", 1), ("role", 1)])

INDEX-08  COLLECTIONS MISSING _id → id PROJECTION DEFAULT
          ──────────────────────────────────────────────────
          Priority: LOW
          All find() calls already use {"_id": 0} — this is correct.

SCHEMA-01  STORE expiresAt AS datetime OBJECT, NOT ISO STRING
           ────────────────────────────────────────────────────
           Priority: IMPORTANT
           TTL indexes only work on BSON Date fields.
           Storing as ISO string breaks TTL auto-deletion.
           
           FIX in _store_otp():
             "expiresAt": datetime.now(timezone.utc) + timedelta(minutes=OTP_EXPIRY_MINS)
             # No .isoformat() — store native datetime

SCHEMA-02  SUBSCRIPTIONS COLLECTION DESIGN
           ──────────────────────────────────
           Current schema is flat and ok. Add:
             invoiceId:      str  (link to invoices collection)
             nextInvoiceAt:  datetime
             cancelledAt:    datetime | None
             cancellationReason: str | None

SCHEMA-03  ADD invoices COLLECTION
           ──────────────────────────
           Priority: IMPORTANT
           {
             id, instituteId, subscriptionId,
             amount, currency, period_start, period_end,
             razorpayPaymentId, status (paid/void),
             pdfUrl, createdAt
           }


════════════════════════════════════════════════════════
SECTION 5: RAZORPAY-SPECIFIC BEST PRACTICES
════════════════════════════════════════════════════════

RZP-01  VERIFY PAYMENT SERVER-SIDE BEFORE ACTIVATION
        See LB-01. Never trust frontend handler callback alone.

RZP-02  USE RAZORPAY ORDERS (not direct payment links)
        Already doing this — correct.

RZP-03  STORE ORDER ID BEFORE OPENING MODAL
        Create order → persist order_id → open modal.
        If user closes modal, order_id is in DB for retry.
        FIX: /payments/create-order upserts order_id into existing pending sub.

RZP-04  ENABLE WEBHOOK FOR ALL PAYMENT EVENTS
        Events to subscribe: payment.captured, payment.failed, order.paid
        Set RAZORPAY_WEBHOOK_SECRET and verify HMAC-SHA256.

RZP-05  STORE razorpay_payment_id AND razorpay_order_id SEPARATELY
        Already doing this — correct.

RZP-06  NEVER LOG FULL WEBHOOK PAYLOAD IN PRODUCTION
        Webhook contains PII (customer email, phone). Log only event type + IDs.
        FIX: logger.info(f"Webhook: {event_type} event_id={event_id}")

RZP-07  RETRY HANDLING — RAZORPAY RETRIES WEBHOOKS 3× ON NON-200
        Always return 200 even on duplicate events (idempotency guard handles it).

RZP-08  AMOUNT MISMATCH CHECK
        Always verify payment.amount_paid >= expected amount in paise.
        Attacker can modify amount in frontend before checkout.
        Code: See backend_fixes.py → _handle_payment_captured()

RZP-09  FOR SUBSCRIPTIONS/RENEWALS: USE RAZORPAY SUBSCRIPTIONS API
        Current implementation is one-time orders. For recurring billing,
        use Razorpay Subscriptions with plan_id. Webhook: subscription.charged.
        This eliminates manual renewal tracking entirely.

RZP-10  TEST WITH rzp_test_ KEY IN STAGING
        Never test with live key. Add ENV check to reject live key in non-prod.


════════════════════════════════════════════════════════
SECTION 6: FRONTEND ISSUES
════════════════════════════════════════════════════════

F-01   RAZORPAY SCRIPT LOADED DYNAMICALLY — NO FALLBACK UI
       ───────────────────────────────────────────────────────
       Priority: IMPORTANT | File: PlanSelectionPage.js → loadRazorpay()

       PROBLEM:
         If CDN is down or script blocked by ad-blocker, user sees a broken
         button with no explanation.

       FIX:
         if (!ok) {
           setError("Payment gateway could not load. Please disable ad-blockers or try a different browser.");
           setLoading(false); return;
         }

F-02   AuthContext: /institute/create CALLED BEFORE OTPs VERIFIED
       ─────────────────────────────────────────────────────────────
       Priority: IMPORTANT | File: OnboardingPage.js

       PROBLEM:
         Current flow: /auth/verify-otp → /onboarding → /institute/create.
         But /institute/create does NOT check that both OTPs were verified.
         An attacker can skip /verify-otp and call /institute/create directly.

       FIX (backend):
         In create_institute(), before insert:
         mob_rec = await db.otp_verifications.find_one({"target": mobile, "channel": "mobile"})
         eml_rec = await db.otp_verifications.find_one({"target": email,  "channel": "email"})
         if not mob_rec or not mob_rec.get("verified"):
             raise HTTPException(400, "Mobile OTP not verified.")
         if not eml_rec or not eml_rec.get("verified"):
             raise HTTPException(400, "Email OTP not verified.")

F-03   PendingApprovalPage POLLS WITH auth-dependent API CALL
       ──────────────────────────────────────────────────────────
       Priority: IMPORTANT | File: PendingApprovalPage.js

       PROBLEM:
         GET /institute/by-id/:id requires auth. If token expires during the
         (potentially hours-long) pending approval wait, polling fails silently.

       FIX:
         Catch 401 in polling and show "Session expired — please log in again"
         with a link to /login. Don't loop on 401.
         
         const checkStatus = async () => {
           try {
             const { data } = await API.get(`/institute/by-id/${user.instituteId}`);
             ...
           } catch (err) {
             if (err.response?.status === 401) {
               clearInterval(intervalRef.current);
               setStatus("session_expired");
             }
           }
         };

F-04   APP.JS: /payment-success AND /pending-approval ARE UNPROTECTED
       ────────────────────────────────────────────────────────────────
       Priority: IMPORTANT | File: App.js

       PROBLEM:
         Anyone can navigate to /payment-success with arbitrary state and
         see the "activated" screen without actually paying.

       FIX:
         Add a one-time token to the navigation state that the backend generates
         as part of the activation response. The success page verifies it:
         
         // In select_plan response:
         "successToken": secrets.token_urlsafe(16)
         
         // Stored in DB with TTL. PaymentSuccessPage verifies it.
         // If missing → redirect to /login.

F-05   REACT ROUTER: NO SCROLL RESET ON NAVIGATION
       ────────────────────────────────────────────────
       Priority: LOW | File: App.js

       FIX:
         import { useEffect } from 'react';
         import { useLocation } from 'react-router-dom';
         function ScrollToTop() {
           const { pathname } = useLocation();
           useEffect(() => window.scrollTo(0, 0), [pathname]);
           return null;
         }
         // Add <ScrollToTop /> inside <BrowserRouter>

F-06   AuthContext: TOKEN STORED IN localStorage (XSS VULNERABLE)
       ─────────────────────────────────────────────────────────────
       Priority: IMPORTANT

       PROBLEM:
         localStorage is accessible to any JS on the page. An XSS attack
         (via third-party scripts, user-generated content) can steal the token.

       RECOMMENDATION:
         For a multi-tenant SaaS, localStorage is a pragmatic tradeoff.
         Mitigate by:
         1. Setting Content-Security-Policy headers (NGINX)
         2. Short JWT expiry (24-48h max) — already done
         3. Token version for remote revocation — see backend_fixes.py
         4. httpOnly cookie alternative requires CORS + credential changes

F-07   SIGNUPPAGE: NO RATE LIMIT FEEDBACK TO USER
       ─────────────────────────────────────────────
       Priority: IMPORTANT | File: SignupPage.js

       FIX:
         catch (err) {
           if (err.response?.status === 429) {
             setError("Too many requests. Please wait 30 seconds.");
           } else {
             setError(err.message || "Signup failed.");
           }
         }

F-08   PLAN SELECTION: AMOUNT IS CALCULATED IN FRONTEND ONLY
       ───────────────────────────────────────────────────────
       Priority: CRITICAL | File: PlanSelectionPage.js

       PROBLEM:
         Frontend calculates totalPrice from hardcoded BASE_PLANS array.
         Backend trusts this amount for Razorpay order creation.
         Attacker can modify the JS bundle or API call to send amount=1.

       FIX (backend):
         In /payments/create-order, calculate the expected amount server-side
         from plan_type + billing_cycle + addons. Reject if frontend amount
         deviates by more than 1 rupee.
         
         PLAN_PRICES = {
             "0-150": 3000, "150-250": 5000, "250-500": 10000,
             "500-750": 15000, "750-1000": 20000,
         }
         expected = PLAN_PRICES.get(strength) * (1 if monthly else 12 * 0.84 * 0.90)
         if abs(data.amount - expected) > 1:
             raise HTTPException(400, "Amount mismatch.")


════════════════════════════════════════════════════════
SECTION 7: RATE LIMITING STRATEGY
════════════════════════════════════════════════════════

Without Redis, use a layered approach:

LAYER 1 — NGINX (before FastAPI sees the request)
  limit_req_zone $binary_remote_addr zone=api_general:10m rate=30r/m;
  limit_req_zone $binary_remote_addr zone=api_auth:10m    rate=5r/m;
  
  location /api/auth/send-otp    { limit_req zone=api_auth    burst=3; proxy_pass ...; }
  location /api/auth/verify-otp  { limit_req zone=api_auth    burst=5; proxy_pass ...; }
  location /api/auth/admin-signup{ limit_req zone=api_auth    burst=3; proxy_pass ...; }
  location /api/                 { limit_req zone=api_general burst=20; proxy_pass ...; }

LAYER 2 — MongoDB rate limit collection (already implemented for OTP)
  Use same pattern for login attempts:
  5 failed logins → 15-minute lockout stored in db.login_lockouts

LAYER 3 — slowapi (Python)
  pip install slowapi
  
  from slowapi import Limiter
  from slowapi.util import get_remote_address
  limiter = Limiter(key_func=get_remote_address)
  app.state.limiter = limiter
  
  @api.post("/auth/login")
  @limiter.limit("10/minute")
  async def login(request: Request, data: UserLogin): ...


════════════════════════════════════════════════════════
SECTION 8: DEPLOYMENT ARCHITECTURE
════════════════════════════════════════════════════════

NGINX CONFIGURATION (key blocks):

  # Main domain
  server {
    server_name growcad.in app.growcad.in;
    
    # Security headers
    add_header X-Frame-Options DENY;
    add_header X-Content-Type-Options nosniff;
    add_header Referrer-Policy strict-origin-when-cross-origin;
    add_header Content-Security-Policy "default-src 'self' https:; script-src 'self' 'unsafe-inline' https://checkout.razorpay.com; frame-src https://api.razorpay.com;";
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains";
    
    # API rate limiting
    limit_req_zone $binary_remote_addr zone=api:10m rate=60r/m;
    limit_req_zone $binary_remote_addr zone=auth:10m rate=5r/m;
    
    location /api/auth/ { limit_req zone=auth burst=3 nodelay; proxy_pass http://backend; }
    location /api/      { limit_req zone=api  burst=20;        proxy_pass http://backend; }
    location /          { root /var/www/frontend/build; try_files $uri /index.html; }
  }
  
  # Wildcard subdomain routing
  server {
    server_name ~^(?<slug>[a-z0-9\-]+)\.growcad\.in$;
    location /api/ { proxy_pass http://backend; proxy_set_header X-Institute-Slug $slug; }
    location /      { root /var/www/frontend/build; try_files $uri /index.html; }
  }
  
  upstream backend { server 127.0.0.1:8000; keepalive 32; }

DOCKER COMPOSE (recommended):
  services:
    backend:  gunicorn -k uvicorn.workers.UvicornWorker -w 4 server:app
    mongo:    mongo:7 with replica set (required for transactions)
    nginx:    reverse proxy + SSL (Certbot wildcard cert for *.growcad.in)
    redis:    optional, for advanced rate limiting and session caching

SSL:
  certbot certonly --dns-cloudflare -d growcad.in -d '*.growcad.in'
  # Wildcard cert required for slug.growcad.in subdomains


════════════════════════════════════════════════════════
SECTION 9: PRODUCTION FOLDER ARCHITECTURE
════════════════════════════════════════════════════════

  growcad/
  ├── backend/
  │   ├── server.py              # main app (current monolith)
  │   ├── routers/               # split when >3000 lines
  │   │   ├── auth.py
  │   │   ├── payments.py
  │   │   ├── institutes.py
  │   │   ├── students.py
  │   │   └── ...
  │   ├── core/
  │   │   ├── config.py          # all env var loading
  │   │   ├── security.py        # jwt, password, otp helpers
  │   │   ├── database.py        # mongo client + indexes
  │   │   └── audit.py           # audit log helper
  │   ├── jobs/
  │   │   ├── expiry.py          # subscription expiry
  │   │   ├── reminders.py       # fee reminders
  │   │   └── recordings.py      # recording pipeline
  │   ├── migrations/
  │   │   └── seed.py            # one-shot seed (not on startup)
  │   ├── tests/                 # existing tests
  │   ├── .env
  │   └── requirements.txt
  ├── frontend/
  │   ├── src/
  │   │   ├── api/index.js       # axios instance
  │   │   ├── contexts/AuthContext.js
  │   │   ├── pages/
  │   │   ├── components/
  │   │   └── hooks/
  │   └── .env
  ├── nginx/
  │   └── growcad.conf
  ├── docker-compose.yml
  └── deploy.sh


════════════════════════════════════════════════════════
SECTION 10: BACKEND FLOW — FINAL PRODUCTION VERSION
════════════════════════════════════════════════════════

ADMIN ONBOARDING:
  POST /auth/admin-signup
    → validate mobile (10 digits), email, password (8+ chars, 1 digit)
    → check existing (non-verified OK → update record)
    → hash password with bcrypt rounds=12
    → insert user (isVerified=False)
    → store OTPs in db.otp_verifications (HMAC hash, datetime expiry)
    → fire SMS + email OTP concurrently (gather with return_exceptions=True)
    → return {sent: true}
  
  POST /auth/verify-otp (×2: mobile, email)
    → rate check via db.otp_rate_limits (MongoDB TTL)
    → HMAC-compare with timing-safe compare_digest
    → increment attempts (max 5 → 429)
    → mark verified: True atomically (condition: verified=False)
    → return {verified: true}
  
  POST /institute/create
    → verify both OTP records exist and are verified
    → validate slug (regex, reserved words)
    → insert institute (slug unique index catches races → 409)
    → update user (instituteId, isVerified=True)
    → delete used OTP records
    → return JWT (with ver, iat, exp, uid, iid, role)
  
  POST /payments/create-order
    → auth required (JWT)
    → check no existing active subscription
    → validate amount server-side from plan_type + billing_cycle
    → create Razorpay order → persist order_id in pending subscription
    → return {id, amount, currency}
  
  [Frontend opens Razorpay modal]
  
  POST /institute/select-plan (redundant check after modal)
    → auth required
    → idempotency: if already active → return success
    → fetch payment from Razorpay API → verify status + amount
    → insert subscription (DuplicateKeyError on razorpayPaymentId → idempotent)
    → update institute.subscriptionStatus
    → return {activated, status, institute}
  
  POST /payments/webhook (authoritative activation)
    → verify HMAC-SHA256 signature
    → insert webhook_events (unique on eventId → duplicate → 200 early return)
    → payment.captured: verify amount ≥ expected, activate institute
    → payment.failed: mark subscription payment_failed
  
  [For cash]:
    POST /institute/select-plan with payment_mode=cash
    → insert subscription with status=pending_approval
    → super-admin reviews via GET /admin/pending-approvals
    → POST /admin/approve-institute → status=active | rejected

DAILY JOBS:
  _subscription_expiry_loop  → marks renewsAt-past subs as "expired"
  _reminder_background_loop  → fee reminders
  _process_recordings        → R2 upload pipeline


════════════════════════════════════════════════════════
SECTION 11: FINAL CHECKLIST — LAUNCH READINESS
════════════════════════════════════════════════════════

[ ] LB-01  Webhook is authoritative payment activation
[ ] LB-02  Razorpay webhook endpoint with HMAC verification
[ ] LB-03  OTP uses secrets.randbelow (not random.randint)
[ ] LB-04  OTP stored as HMAC-SHA256, compared with hmac.compare_digest
[ ] LB-05  OTP rate limit in MongoDB (not in-memory dict)
[ ] LB-06  bcrypt rounds=12
[ ] LB-07  Subscription expiry background job
[ ] LB-08  JWT includes ver claim; auth() validates ver
[ ] LB-09  DuplicateKeyError catch on subscription insert
[ ] LB-10  webhook_events collection with unique eventId index

[ ] S-03   Subdomain login uses re.escape() for phone number regex
[ ] S-04   Unique index on institutes.slug; wrap insert in try/except
[ ] S-06   ENV vars validated at startup for production mode
[ ] S-07   CORS_ORIGINS is explicit list (not "*") in production
[ ] S-08   /seed returns 404 in production

[ ] F-02   /institute/create validates both OTPs verified
[ ] F-03   PendingApprovalPage handles 401 (session expired)
[ ] F-08   Amount calculated server-side and verified

[ ] INDEX-01  institutes.slug unique index
[ ] INDEX-02  subscriptions.razorpayPaymentId unique sparse index
[ ] INDEX-03  subscriptions.(status, renewsAt) index
[ ] INDEX-04  webhook_events.razorpayEventId unique index + TTL
[ ] INDEX-05  otp_verifications.expiresAt TTL index
[ ] SCHEMA-01 expiresAt stored as datetime object, not ISO string

[ ] NGINX wildcard SSL cert (*.growcad.in via DNS challenge)
[ ] NGINX security headers (CSP, HSTS, X-Frame-Options)
[ ] NGINX rate limits on /api/auth/ (5r/m)
[ ] Razorpay webhook configured in dashboard
[ ] RAZORPAY_WEBHOOK_SECRET in production env
[ ] ENV=production set in production env
[ ] Gunicorn with 4+ UvicornWorker processes
[ ] MongoDB 7 with replica set (required for future transactions)
[ ] Certbot auto-renew configured
[ ] Sentry / error tracking configured
[ ] Health check endpoint: GET /health → {"status": "ok"}
