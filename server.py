from fastapi import FastAPI, APIRouter, HTTPException, Depends, UploadFile, File, BackgroundTasks
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
import uuid
import csv
import io
import random
import asyncio
from pathlib import Path
from pydantic import BaseModel
from typing import List, Optional
from datetime import datetime, timezone, timedelta
import jwt
import bcrypt
import hashlib
import re
import httpx
import razorpay
import hmac
import secrets
from pymongo.errors import DuplicateKeyError
from pymongo import UpdateOne
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

JWT_SECRET = os.environ.get('JWT_SECRET')
if not JWT_SECRET:
    raise Exception("JWT_SECRET is missing in environment")
JWT_ALGO = "HS256"

RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
RAZORPAY_WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET", "")
OTP_EXPIRY_MINS = int(os.environ.get("OTP_EXPIRY_MINS", 5))
OTP_RATE_WINDOW_S = int(os.environ.get("OTP_RATE_WINDOW_S", 30))
TOKEN_VERSION = int(os.environ.get("TOKEN_VERSION", 1))

rzp_client = (
    razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))
    if RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET
    else None
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ─── Twilio (optional - activates when env vars are present) ───
TWILIO_SID = os.environ.get('TWILIO_ACCOUNT_SID')
TWILIO_TOKEN = os.environ.get('TWILIO_AUTH_TOKEN')
TWILIO_PHONE = os.environ.get('TWILIO_PHONE_NUMBER')
TWILIO_WA = os.environ.get('TWILIO_WHATSAPP_NUMBER')
twilio_client = None
if TWILIO_SID and TWILIO_TOKEN:
    try:
        from twilio.rest import Client as TwilioClient
        twilio_client = TwilioClient(TWILIO_SID, TWILIO_TOKEN)
        logger.info("Twilio client initialized successfully")
    except Exception as e:
        logger.warning(f"Twilio init failed: {e}")
else:
    logger.info("Twilio credentials not configured - SMS/WhatsApp disabled, in-app notifications active")



app = FastAPI(title="Growcad API")
@app.on_event("startup")
async def create_indexes():
    # ─── users ───
    await db.users.create_index("email", unique=True)
    await db.users.create_index("mobile")

    # ─── students ───
    await db.students.create_index([("instituteId", 1), ("batchId", 1)])
    await db.students.create_index([("instituteId", 1), ("name", 1)])
    await db.students.create_index("email")

    # ─── teachers ───
    await db.teachers.create_index("instituteId")

    # ─── batches ───
    await db.batches.create_index("instituteId")
    await db.batches.create_index([("instituteId", 1), ("teacherId", 1)])

    # ─── attendance ───
    await db.attendance.create_index([("studentId", 1), ("date", 1)])
    await db.attendance.create_index([("instituteId", 1), ("date", 1)])
    await db.attendance.create_index([("batchId", 1), ("instituteId", 1)])
    await db.attendance.create_index([("instituteId", 1), ("batchId", 1), ("date", 1)])

    # ─── student_fees ───
    await db.student_fees.create_index([("instituteId", 1), ("batchId", 1)])
    await db.student_fees.create_index([("instituteId", 1), ("studentId", 1)])
    await db.student_fees.create_index("studentId")

    # ─── tests / marks ───
    await db.tests.create_index([("instituteId", 1), ("batchId", 1)])
    await db.marks.create_index([("testId", 1), ("studentId", 1)])
    await db.marks.create_index([("instituteId", 1), ("studentId", 1)])

    # ─── notifications ───
    await db.notifications.create_index([("instituteId", 1), ("createdAt", -1)])

    # ─── announcements ───
    await db.announcements.create_index([("instituteId", 1), ("createdAt", -1)])

    # ─── live_classes ───
    await db.live_classes.create_index([("instituteId", 1), ("startTime", -1)])
    await db.live_classes.create_index([("batchId", 1), ("instituteId", 1)])

    # ─── otp_verifications ───
    await db.otp_verifications.create_index(
        [("target", 1), ("channel", 1)], unique=True
    )

    # ─── reminder / message logs ───
    await db.reminder_logs.create_index([("instituteId", 1), ("timestamp", -1)])
    await db.message_logs.create_index([("instituteId", 1), ("timestamp", -1)])
    await db.absent_alerts.create_index([("studentId", 1), ("date", 1), ("instituteId", 1)])

    await db.institutes.create_index("slug", unique=True)
    await db.subscriptions.create_index("razorpayPaymentId", unique=True, sparse=True)
    await db.subscriptions.create_index("razorpayOrderId", sparse=True)
    await db.webhook_events.create_index("razorpayEventId", unique=True)
    await db.webhook_events.create_index("processedAt", expireAfterSeconds=30 * 86400)
    await db.otp_rate_limits.create_index("target", unique=True)
    await db.otp_rate_limits.create_index("expiresAt", expireAfterSeconds=0)


    logger.info("All MongoDB indexes created")

@app.on_event("startup")
async def otp_cleanup_job():
    async def cleanup():
        while True:
            await db.otp_verifications.delete_many({
                "expires_at": {"$lt": datetime.now(timezone.utc).isoformat()}
            })
            await asyncio.sleep(300)

    asyncio.create_task(cleanup())
# ─────────────────────────────────────────────────────────────────────────────
# MIDDLEWARE: extract X-Institute-Slug and attach instituteId to requests
# ─────────────────────────────────────────────────────────────────────────────

#Add this BEFORE app.include_router(api) in server.py:



class TenantMiddleware(BaseHTTPMiddleware):
    # Routes where an unknown/missing slug must not block the request.
    # Onboarding, auth, institute creation and seed are public by nature.
    _SLUG_EXEMPT_PREFIXES = (
        "/api/auth/",
        "/api/institute/",
        "/api/payments/webhook",
        "/api/seed",
        "/docs",
        "/openapi",
        "/redoc",
        "/health",
    )

    async def dispatch(self, request: Request, call_next):
        slug = request.headers.get("X-Institute-Slug", "").lower().strip()

        if slug:
            path = request.url.path
            is_exempt = any(path.startswith(p) for p in self._SLUG_EXEMPT_PREFIXES)

            if is_exempt:
                # Don't require the institute to exist during onboarding/auth flows.
                request.state.institute_id = None
                request.state.institute    = None
            else:
                inst = await db.institutes.find_one({"slug": slug}, {"_id": 0})
                if not inst:
                    return JSONResponse(
                        status_code=404,
                        content={"detail": "Invalid institute slug"}
                    )
                request.state.institute_id = inst["id"]
                request.state.institute    = inst
        else:
            request.state.institute_id = None
            request.state.institute    = None

        return await call_next(request)

app.add_middleware(TenantMiddleware)



api = APIRouter(prefix="/api")
security = HTTPBearer()


def uid():
    return str(uuid.uuid4())


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def hash_pw(pw):
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=12)).decode()


def check_pw(pw, h):
    return bcrypt.checkpw(pw.encode(), h.encode())


def make_token(user_id, role, institute_id, slug=None):
    return jwt.encode(
        {
            "uid": user_id,
            "role": role,
            "iid": institute_id,
            "slug": slug,
            "ver": TOKEN_VERSION,
            "exp": datetime.now(timezone.utc) + timedelta(hours=48)
        },
        JWT_SECRET,
        algorithm=JWT_ALGO
    )


async def auth(cred: HTTPAuthorizationCredentials = Depends(security)):
    try:
        p = jwt.decode(cred.credentials, JWT_SECRET, algorithms=[JWT_ALGO])
        uid_claim = p.get("uid")
        iid_claim = p.get("iid")   # may be "" for pre-onboarding admin tokens

        if p.get("ver", 0) < TOKEN_VERSION:
            raise HTTPException(401, "Token revoked. Please log in again.")

        if not uid_claim:
            raise HTTPException(401, "Malformed token")

        # Build the query: iid can be "" (pre-onboarding) or a real UUID.
        # Allow both so admins can call /institute/select-plan with the token
        # returned by /institute/create.
        query: dict = {"id": uid_claim}
        if iid_claim is not None:
            query["instituteId"] = iid_claim

        u = await db.users.find_one(query, {"_id": 0})
        if not u:
            raise HTTPException(401, "User not found")
        return u
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Invalid token")


def require_role(*roles):
    """Role-check dependency factory"""
    async def checker(user=Depends(auth)):
        if user["role"] not in roles:
            raise HTTPException(403, f"Access denied. Required role: {', '.join(roles)}")
        return user
    return checker

admin_only = require_role("admin")
teacher_or_admin = require_role("admin", "teacher")
any_role = require_role("admin", "teacher", "student")


async def get_teacher_batch_ids(user):
    """Get batch IDs assigned to a teacher"""
    teacher = await db.teachers.find_one({"id": user.get("teacherId", ""), "instituteId": user["instituteId"]}, {"_id": 0})
    if teacher:
        return teacher.get("assignedBatches", [])
    # Fallback: find batches where this teacher is assigned
    batches = await db.batches.find({"teacherId": user.get("teacherId", ""), "instituteId": user["instituteId"]}, {"_id": 0}).to_list(100)
    return [b["id"] for b in batches]

# ─────────────────────────────────────────────────────────────────────────────
# BULK FETCH HELPERS  — use these instead of per-row find_one calls
# ─────────────────────────────────────────────────────────────────────────────

async def _get_students_map(institute_id: str, student_ids: list = None) -> dict:
    """Return {student_id: student_doc}. Optionally scoped to a list of IDs."""
    q = {"instituteId": institute_id}
    if student_ids is not None:
        q["id"] = {"$in": list(set(student_ids))}
    docs = await db.students.find(q, {"_id": 0}).to_list(2000)
    return {d["id"]: d for d in docs}


async def _get_batches_map(institute_id: str, batch_ids: list = None) -> dict:
    """Return {batch_id: batch_doc}."""
    q = {"instituteId": institute_id}
    if batch_ids is not None:
        q["id"] = {"$in": list(set(batch_ids))}
    docs = await db.batches.find(q, {"_id": 0}).to_list(500)
    return {d["id"]: d for d in docs}


async def _get_teachers_map(institute_id: str, teacher_ids: list = None) -> dict:
    """Return {teacher_id: teacher_doc}."""
    q = {"instituteId": institute_id}
    if teacher_ids is not None:
        q["id"] = {"$in": list(set(teacher_ids))}
    docs = await db.teachers.find(q, {"_id": 0}).to_list(500)
    return {d["id"]: d for d in docs}

"""
backend/auth_routes.py
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
NEW ROUTES — paste these into server.py alongside the
existing auth block. They slot into the same `api` router
and share all existing helpers (uid, now_iso, hash_pw,
check_pw, make_token, db, jwt, etc.).

ENVIRONMENT VARIABLES REQUIRED:
  MSG91_AUTH_KEY   – MSG91 key for mobile OTP
  RESEND_API_KEY   – Resend key for email OTP
  OTP_EXPIRY_MINS  – default 5 (optional override)

NEW COLLECTIONS:
  otp_verifications  { id, target, channel, otp_hash, verified, expires_at, created_at }
  subscriptions      { id, institute_id, plan_type, addons, billing_cycle, amount, status, created_at }

The existing `institutes` collection is extended with:
  slug, address, total_strength, subscription_status, owner_id
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

# ── Extra imports (add to server.py top) ─────────────────────────────────────
# import hashlib
# import httpx          # pip install httpx
# from pydantic import BaseModel, Field
# from typing import Optional, List

# ── Extra env vars ────────────────────────────────────────────────────────────
# MSG91_AUTH_KEY  = os.environ.get("MSG91_AUTH_KEY", "")
# RESEND_API_KEY  = os.environ.get("RESEND_API_KEY", "")
# OTP_EXPIRY_MINS = int(os.environ.get("OTP_EXPIRY_MINS", 5))

# ─────────────────────────────────────────────────────────────────────────────
# PYDANTIC MODELS  (add to the existing Models section)
# ─────────────────────────────────────────────────────────────────────────────

class AdminSignupReq(BaseModel):
    name:     str
    mobile:   str          # digits only, 10 chars
    email:    str
    password: str

class SendOtpReq(BaseModel):
    target:  str           # mobile number or email address
    channel: str = "mobile"  # "mobile" | "email"

class VerifyOtpReq(BaseModel):
    target:  str
    otp:     str
    channel: str = "mobile"

class AdminLoginReq(BaseModel):
    email:    str
    password: str

class SubdomainLoginReq(BaseModel):
    mobile: str
    otp:    str
    slug:   Optional[str] = None            # institute slug from frontend subdomain detection

class InstituteCreateReq(BaseModel):
    mobile:           str
    email:            str
    institute_name:   str
    institute_slug:   str
    institute_address: str = ""
    total_strength:   str = "0-150"

class SelectPlanReq(BaseModel):
    institute_id: str
    plan_type: str
    addons: List[str] = []
    billing_cycle: str = "monthly"
    amount: float
    payment_mode: str = "razorpay"  # "razorpay" | "cash"
    razorpay_payment_id: Optional[str] = None
    razorpay_order_id: Optional[str] = None
    razorpay_signature: Optional[str] = None


class CreateOrderReq(BaseModel):
    institute_id: str
    plan_type: str
    addons: List[str] = []
    billing_cycle: str = "monthly"
    amount: float
    currency: str = "INR"



class ApprovalReq(BaseModel):
    institute_id: str
    action: str
    note: str = ""



# ─────────────────────────────────────────────────────────────────────────────
# OTP HELPERS
# ─────────────────────────────────────────────────────────────────────────────
otp_rate_limit = {}

async def check_otp_rate(target):
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=OTP_RATE_WINDOW_S)

    try:
        await db.otp_rate_limits.insert_one({
            "target": target,
            "createdAt": now_iso(),
            "expiresAt": expires_at,
        })
    except DuplicateKeyError:
        raise HTTPException(
            429,
            f"Wait {OTP_RATE_WINDOW_S} seconds before requesting another OTP",
        )


def _otp_hash(otp: str) -> str:
    return hmac.new(JWT_SECRET.encode(), otp.encode(), hashlib.sha256).hexdigest()


def _otp_matches(otp: str, stored_hash: str) -> bool:
    return hmac.compare_digest(stored_hash or "", _otp_hash(otp))


def _generate_otp(length: int = 6) -> str:
    return "".join(str(secrets.randbelow(10)) for _ in range(length))



async def _send_mobile_otp(mobile: str, otp: str) -> bool:
    """Send OTP via MSG91 Flow API. Returns True on success."""
    key         = os.getenv("MSG91_AUTH_KEY", "")
    template_id = os.getenv("MSG91_TEMPLATE_ID", "")

    if not key or not template_id:
        logger.warning(f"[OTP-DEV] MSG91 not configured — mobile={mobile} otp={otp}")
        return True

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                "https://control.msg91.com/api/v5/flow/",
                json={
                    "flow_id": template_id,
                    "mobiles": f"91{mobile}",
                    "VAR1":    otp,
                },
                headers={
                    "authkey":      key,
                    "Content-Type": "application/json",
                },
            )
        if r.status_code == 200:
            return True
        logger.error(f"MSG91 non-200: {r.status_code} {r.text}")
        return False
    except Exception as e:
        logger.error(f"MSG91 error: {e}")
        return False


async def _send_email_otp(email: str, otp: str) -> bool:
    """Send OTP via Resend. Returns True on success."""
    key        = os.getenv("RESEND_API_KEY", "")
    email_from = os.getenv("EMAIL_FROM", "Growcad <noreply@growcad.in>")

    if not key:
        logger.warning(f"[OTP-DEV] Resend not configured — email={email} otp={otp}")
        return True

    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                "https://api.resend.com/emails",
                json={
                    "from":    email_from,
                    "to":      [email],
                    "subject": "Your Growcad OTP",
                    "html":    (
                        f'<div style="font-family:sans-serif;max-width:480px;margin:auto;padding:32px">'
                        f'<h2 style="color:#1a1625">Verify your email</h2>'
                        f'<p>Use the code below to complete your Growcad sign-up.</p>'
                        f'<div style="font-size:36px;font-weight:700;letter-spacing:8px;color:#6C3CF4;'
                        f'background:#f3f0ff;border-radius:12px;padding:20px 24px;'
                        f'display:inline-block;margin:16px 0">{otp}</div>'
                        f'<p style="color:#888">Valid for 5 minutes.</p>'
                        f'</div>'
                    ),
                },
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type":  "application/json",
                },
            )
        if r.status_code in (200, 201):
            return True
        logger.error(f"Resend non-200: {r.status_code} {r.text}")
        return False
    except Exception as e:
        logger.error(f"Resend error: {e}")
        return False


# ── Public wrappers (raise HTTPException on failure) ─────────────────────────

async def send_sms_otp(mobile: str, otp: str) -> None:
    """Send SMS OTP via MSG91 Flow API. Raises HTTP 500 on failure."""
    ok = await _send_mobile_otp(mobile, otp)
    if not ok:
        raise HTTPException(500, "Failed to send SMS OTP")


async def send_email_otp(email: str, otp: str) -> None:
    """Send email OTP via Resend. Raises HTTP 500 on failure."""
    ok = await _send_email_otp(email, otp)
    if not ok:
        raise HTTPException(500, "Failed to send Email OTP")


async def _store_otp(target: str, channel: str, otp: str) -> None:
    """Upsert OTP record (one live OTP per target+channel at a time)."""
    expiry_mins = int(os.getenv("OTP_EXPIRY_MINS", 5))
    await db.otp_verifications.update_one(
        {"target": target, "channel": channel},
        {"$set": {
            "id":         uid(),
            "otp_hash":   _otp_hash(otp),
            "verified":   False,
            "attempts":   0,
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=expiry_mins)).isoformat(),
            "created_at": now_iso(),
        }},
        upsert=True,
    )


async def _consume_otp(target: str, channel: str, otp: str) -> bool:
    rec = await db.otp_verifications.find_one({"target": target, "channel": channel})

    if not rec:
        return False
    if rec.get("verified"):
        return False

    # Parse expires_at safely regardless of whether it carries timezone info
    expires_raw = rec.get("expires_at", "")
    try:
        expires_dt = datetime.fromisoformat(expires_raw)
    except (ValueError, TypeError):
        return False
    # Ensure timezone-aware comparison
    if expires_dt.tzinfo is None:
        expires_dt = expires_dt.replace(tzinfo=timezone.utc)
    if expires_dt < datetime.now(timezone.utc):
        return False

    # 🚨 LIMIT ATTEMPTS
    if rec.get("attempts", 0) >= 5:
        raise HTTPException(429, "Too many OTP attempts")

    # ❌ WRONG OTP
    if not _otp_matches(otp, rec.get("otp_hash", "")):
        await db.otp_verifications.update_one(
            {"_id": rec["_id"]},
            {"$inc": {"attempts": 1}}
        )
        return False

    # ✅ CORRECT OTP
    await db.otp_verifications.update_one(
        {"_id": rec["_id"]},
        {"$set": {"verified": True, "otp_hash": None}}
    )

    return True


# ─────────────────────────────────────────────────────────────────────────────
# NEW AUTH ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@api.post("/auth/send-otp")
async def send_otp(data: SendOtpReq):
    """Send a 6-digit OTP to mobile (MSG91 Flow) or email (Resend)."""
    target  = data.target.strip()
    channel = data.channel.strip().lower()

    if not target:
        raise HTTPException(400, "target (mobile number or email) is required.")
    if channel not in ("mobile", "email"):
        raise HTTPException(400, "channel must be 'mobile' or 'email'.")

    # Normalise mobile number (strip country code and spaces)
    if channel == "mobile":
        target = target.replace("+91", "").replace(" ", "").strip()
        if not target.isdigit() or len(target) != 10:
            raise HTTPException(400, "mobile must be a 10-digit number.")

    await check_otp_rate(target)
    otp = _generate_otp()
    await _store_otp(target, channel, otp)

    if channel == "mobile":
        await send_sms_otp(target, otp)
    else:
        await send_email_otp(target, otp)

    return {"sent": True, "channel": channel}


@api.post("/auth/verify-otp")
async def verify_otp_endpoint(data: VerifyOtpReq):
    """Verify an OTP without logging the user in (used during signup flow)."""
    target  = data.target.strip()
    channel = data.channel.strip().lower()
    otp     = data.otp.strip()

    if not target:
        raise HTTPException(400, "target is required.")
    if channel == "mobile":
        target = target.replace("+91", "").replace(" ", "").strip()
    if not otp.isdigit() or len(otp) != 6:
        raise HTTPException(400, "OTP must be exactly 6 digits.")

    ok = await _consume_otp(target, channel, otp)
    if not ok:
        raise HTTPException(400, "Invalid or expired OTP.")
    return {"verified": True}


@api.post("/auth/admin-signup")
async def admin_signup(data: AdminSignupReq):
    """
    Pre-register admin user + trigger OTPs for both mobile and email.
    User is created with isVerified=False; /auth/verify-otp must be called
    for both channels before /institute/create is called.

    Re-signup is allowed if the existing account is still unverified
    (i.e. the user never finished onboarding), so they can request fresh OTPs.
    """
    mobile = data.mobile.replace("+91", "").replace(" ", "").strip()
    

    email  = data.email.strip().lower()

    existing_email  = await db.users.find_one({"email": email},  {"_id": 0})
    existing_mobile = await db.users.find_one({"mobile": mobile}, {"_id": 0})

    # Block signup only when the account is already fully set up
    if existing_email and existing_email.get("isVerified"):
        raise HTTPException(400, "An account with this email already exists.")
    if existing_mobile and existing_mobile.get("isVerified") and existing_mobile.get("email") != email:
        raise HTTPException(400, "An account with this mobile number already exists.")

    if existing_email and not existing_email.get("isVerified"):
        # Allow re-signup: update the pending record with latest details
        await db.users.update_one(
            {"email": email},
            {"$set": {
                "name":         data.name.strip(),
                "mobile":       mobile,
                "passwordHash": hash_pw(data.password),
                "isVerified":   False,
            }},
        )
    else:
        # Fresh signup
        pending = {
            "id":           uid(),
            "name":         data.name.strip(),
            "mobile":       mobile,
            "email":        email,
            "passwordHash": hash_pw(data.password),
            "role":         "admin",
            "instituteId":  "",
            "isVerified":   False,
            "createdAt":    now_iso(),
        }
        await db.users.insert_one(pending)

    # Fire both OTPs concurrently; log failures but don't crash signup
    mobile_otp = _generate_otp()
    email_otp  = _generate_otp()
    await _store_otp(mobile, "mobile", mobile_otp)
    await _store_otp(email,  "email",  email_otp)

    results = await asyncio.gather(
        _send_mobile_otp(mobile, mobile_otp),
        _send_email_otp(email,   email_otp),
        return_exceptions=True,
    )
    for i, r in enumerate(results):
        if isinstance(r, Exception) or r is False:
            channel = "mobile" if i == 0 else "email"
            logger.error(f"OTP send failed for {channel} during admin-signup: {r}")

    return {"sent": True, "mobile": mobile, "email": email}





@api.post("/auth/subdomain-login")
async def subdomain_login(data: SubdomainLoginReq, request: Request):
    slug = _resolve_slug(request, data.slug)

    if not slug:
        raise HTTPException(400, "Institute not identified")

    institute = await db.institutes.find_one({"slug": slug}, {"_id": 0})
    if not institute:
        raise HTTPException(404, f"Institute '{slug}' not found.")

    iid = institute["id"]
    mobile = data.mobile.replace("+91", "").replace(" ", "").strip()
    phone_variants = [
        mobile,
        f"+91{mobile}",
        f"+91-{mobile}",
        f"91{mobile}",
    ]

    # ✅ verify OTP
    ok = await _consume_otp(mobile, "mobile", data.otp)
    if not ok:
        raise HTTPException(400, "Invalid or expired OTP.")

    

    # Resolve user: check student first, then teacher, both scoped to this institute
    student = await db.students.find_one(
        {"phoneNumber": {"$in": phone_variants}, "instituteId": iid},
        {"_id": 0},
    )


    u = None
    if student:
        u = await db.users.find_one(
            {"studentId": student["id"], "instituteId": iid}, {"_id": 0}
        )
    else:
        teacher = await db.teachers.find_one(
            {"phoneNumber": {"$in": phone_variants}, "instituteId": iid},
            {"_id": 0},
        )

        if teacher:
            u = await db.users.find_one(
                {"teacherId": teacher["id"], "instituteId": iid}, {"_id": 0}
            )

    if not u:
        raise HTTPException(
            404,
            "No account found for this mobile number in this institute. "
            "Please contact your admin.",
        )

    token = make_token(u["id"], u["role"], iid, slug)

    return {
        "token": token,
        "user": _safe_user(u),
        "institute": institute
    }


# ─────────────────────────────────────────────────────────────────────────────
# INSTITUTE ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@api.get("/institute/check-slug")
async def check_institute_slug(slug: str):
    """Returns { available: bool } for a given slug."""
    slug = slug.lower().strip()
    if len(slug) < 3:
        return {"available": False, "reason": "Slug must be at least 3 characters."}

    # Reserved slugs
    RESERVED = {"www", "api", "app", "mail", "admin", "growcad", "support", "help", "demo"}
    if slug in RESERVED:
        return {"available": False, "reason": "This slug is reserved."}

    existing = await db.institutes.find_one({"slug": slug})
    return {"available": existing is None}


@api.post("/institute/create")
async def create_institute(data: InstituteCreateReq):
    """
    Create institute after OTP verification and onboarding form.
    Links the pending admin user to this institute.
    """
    mobile = data.mobile.replace("+91", "").replace(" ", "").strip()
    email  = data.email.strip().lower()
    slug   = data.institute_slug.lower().strip()

    # 🔐 Ensure OTP verified before creating institute
    mobile_verified = await db.otp_verifications.find_one({
        "target": mobile,
        "channel": "mobile",
        "verified": True
    })

    email_verified = await db.otp_verifications.find_one({
        "target": email,
        "channel": "email",
        "verified": True
    })

    if not mobile_verified or not email_verified:
        raise HTTPException(403, "Please verify OTP first")

    # Double-check slug
    if await db.institutes.find_one({"slug": slug}):
        raise HTTPException(400, "This slug is already taken.")

    # Find the pending admin user
    admin = await db.users.find_one({"email": email, "role": "admin"}, {"_id": 0})
    if not admin:
        raise HTTPException(404, "Admin account not found. Please sign up first.")

    # Create institute
    iid = uid()
    institute = {
        "id":                 iid,
        "name":               data.institute_name.strip(),
        "slug":               slug,
        "domain":             f"{slug}.growcad.in",
        "address":            data.institute_address.strip(),
        "totalStrength":      data.total_strength,
        "ownerId":            admin["id"],
        "subscriptionStatus": "pending",   # becomes "active" after plan selection
        "plan":               None,
        "addons":             [],
        "createdAt":          now_iso(),
    }
    await db.institutes.insert_one(institute)

    # Link admin → institute + mark verified
    await db.users.update_one(
        {"id": admin["id"]},
        {"$set": {"instituteId": iid, "isVerified": True}},
    )
    # Refresh admin dict from DB so all fields (instituteId, isVerified) are current
    admin = await db.users.find_one({"id": admin["id"]}, {"_id": 0})

    token    = make_token(admin["id"], "admin", iid, slug)
    user_out = _safe_user(admin)
    inst_out = {k: v for k, v in institute.items() if k != "_id"}

    return {
        "token":     token,
        "user":      user_out,
        "institute": inst_out,
    }


   # already defined above (alias)
@api.get("/institute/by-slug")
async def get_institute_by_slug(slug: str):
    """Resolve institute by slug (used by subdomain middleware and frontend)."""
    inst = await db.institutes.find_one({"slug": slug.lower()}, {"_id": 0})
    if not inst:
        raise HTTPException(404, f"Institute '{slug}' not found.")
    return inst


@api.get("/institute/by-id/{iid}")
async def get_institute_by_id(iid: str, user=Depends(auth)):
    """Fetch institute details by ID (auth required, tenant-scoped)."""
    if user.get("instituteId") != iid and user.get("role") != "superadmin":
        raise HTTPException(403, "Access denied.")
    inst = await db.institutes.find_one({"id": iid}, {"_id": 0})
    if not inst:
        raise HTTPException(404, "Institute not found.")
    return inst


@api.post("/payments/create-order")
async def create_razorpay_order(data: CreateOrderReq, user=Depends(auth)):
    if user.get("instituteId") != data.institute_id:
        raise HTTPException(403, "Access denied.")

    if not rzp_client:
        raise HTTPException(503, "Payment gateway not configured.")

    try:
        order = rzp_client.order.create({
            "amount": int(data.amount * 100),
            "currency": data.currency,
            "receipt": f"order_{uid()[:8]}",
            "notes": {
                "institute_id": data.institute_id,
                "admin_email": user.get("email", ""),
            },
        })
    except Exception as e:
        logger.error(f"Razorpay order creation failed: {e}")
        raise HTTPException(500, "Failed to create payment order.")
    await db.subscriptions.update_one(
        {
            "instituteId": data.institute_id,
            "status": "pending",
            "paymentMode": "razorpay",
        },
        {
            "$set": {
                "planType": data.plan_type,
                "addons": data.addons,
                "billingCycle": data.billing_cycle,
                "amount": data.amount,
                "paymentMode": "razorpay",
                "razorpayOrderId": order["id"],
                "updatedAt": now_iso(),
            },
            "$setOnInsert": {
                "id": uid(),
                "instituteId": data.institute_id,
                "status": "pending",
                "createdAt": now_iso(),
            },
        },
        upsert=True,
    )



    return {
        "id": order["id"],
        "amount": order["amount"],
        "currency": order["currency"],
    }
@api.post("/payments/webhook")
async def razorpay_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Razorpay-Signature", "")

    if RAZORPAY_WEBHOOK_SECRET:
        expected = hmac.new(
            RAZORPAY_WEBHOOK_SECRET.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(expected, signature):
            raise HTTPException(400, "Invalid webhook signature.")

    import json
    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(400, "Invalid webhook payload.")

    event_id = payload.get("id", "")
    event_type = payload.get("event", "")

    try:
        await db.webhook_events.insert_one({
            "razorpayEventId": event_id,
            "event": event_type,
            "processedAt": datetime.now(timezone.utc),
        })
    except DuplicateKeyError:
        return {"ok": True, "duplicate": True}

    if event_type == "payment.captured":
        payment = payload.get("payload", {}).get("payment", {}).get("entity", {})
        order_id = payment.get("order_id")
        payment_id = payment.get("id")
        amount_paise = payment.get("amount", 0)
        institute_id = payment.get("notes", {}).get("institute_id")

        if institute_id and order_id:
            sub = await db.subscriptions.find_one({
                "instituteId": institute_id,
                "razorpayOrderId": order_id,
            })

            if sub:
                expected_paise = int(sub.get("amount", 0) * 100)

                if amount_paise >= expected_paise:
                    renews_at = (
                        datetime.now(timezone.utc)
                        + timedelta(days=365 if sub.get("billingCycle") == "yearly" else 30)
                    ).isoformat()

                    await db.subscriptions.update_one(
                        {"_id": sub["_id"]},
                        {"$set": {
                            "status": "active",
                            "razorpayPaymentId": payment_id,
                            "activatedAt": now_iso(),
                            "renewsAt": renews_at,
                        }},
                    )

                    await db.institutes.update_one(
                        {"id": institute_id},
                        {"$set": {"subscriptionStatus": "active"}},
                    )

                    await db.institute_plans.update_one(
                        {"instituteId": institute_id},
                        {"$set": {
                            "plan": sub.get("planType", "standard"),
                            "updatedAt": now_iso(),
                        }},
                        upsert=True,
                    )

    elif event_type == "payment.failed":
        payment = payload.get("payload", {}).get("payment", {}).get("entity", {})
        order_id = payment.get("order_id")
        institute_id = payment.get("notes", {}).get("institute_id")

        if institute_id and order_id:
            await db.subscriptions.update_one(
                {"instituteId": institute_id, "razorpayOrderId": order_id},
                {"$set": {"status": "payment_failed", "failedAt": now_iso()}},
            )

    return {"ok": True}


@api.post("/institute/select-plan")
async def select_plan(data: SelectPlanReq, user=Depends(auth)):
    iid = data.institute_id

    if user.get("instituteId") != iid:
        raise HTTPException(403, "Access denied.")

    if data.payment_mode == "razorpay":
        if not rzp_client:
            raise HTTPException(503, "Payment gateway not configured.")

        if not data.razorpay_payment_id or not data.razorpay_order_id or not data.razorpay_signature:
            raise HTTPException(400, "Missing Razorpay payment details.")

        try:
            rzp_client.utility.verify_payment_signature({
                "razorpay_order_id": data.razorpay_order_id,
                "razorpay_payment_id": data.razorpay_payment_id,
                "razorpay_signature": data.razorpay_signature,
            })

            payment = rzp_client.payment.fetch(data.razorpay_payment_id)
            if payment.get("status") not in ("captured", "authorized"):
                raise HTTPException(402, "Payment not completed.")

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Razorpay verification failed: {e}")
            raise HTTPException(402, "Payment verification failed.")

        subscription_status = "active"

    elif data.payment_mode == "cash":
        subscription_status = "pending_approval"

    else:
        raise HTTPException(400, "payment_mode must be 'razorpay' or 'cash'.")

    sub = {
        "id": uid(),
        "instituteId": iid,
        "planType": data.plan_type,
        "addons": data.addons,
        "billingCycle": data.billing_cycle,
        "amount": data.amount,
        "paymentMode": data.payment_mode,
        "status": subscription_status,
        "createdAt": now_iso(),
        "renewsAt": (
            datetime.now(timezone.utc) + timedelta(days=365 if data.billing_cycle == "yearly" else 30)
        ).isoformat() if subscription_status == "active" else None,
    }

    if data.payment_mode == "razorpay":
        sub["razorpayPaymentId"] = data.razorpay_payment_id
        sub["razorpayOrderId"] = data.razorpay_order_id

    sub_update = {k: v for k, v in sub.items() if k not in ("id", "createdAt")}

    try:
        await db.subscriptions.update_one(
            (
                {"instituteId": iid, "razorpayOrderId": data.razorpay_order_id}
                if data.payment_mode == "razorpay"
                else {"instituteId": iid, "status": "pending_approval", "paymentMode": "cash"}
            ),
            {
                "$set": sub_update,
                "$setOnInsert": {"id": sub["id"], "createdAt": sub["createdAt"]},
            },
            upsert=True,
        )
    except DuplicateKeyError:
        existing = await db.subscriptions.find_one(
            {"razorpayPaymentId": data.razorpay_payment_id},
            {"_id": 0},
        )
        updated_inst = await db.institutes.find_one({"id": iid}, {"_id": 0})
        return {
            "activated": True,
            "status": "active",
            "subscriptionId": existing["id"] if existing else "",
            "institute": updated_inst,
        }

    await db.institutes.update_one(
        {"id": iid},
        {"$set": {
            "subscriptionStatus": subscription_status,
            "plan": data.plan_type,
            "addons": data.addons,
            "billingCycle": data.billing_cycle,
        }},
    )

    if subscription_status == "active":
        await db.institute_plans.update_one(
            {"instituteId": iid},
            {"$set": {"plan": data.plan_type, "updatedAt": now_iso()}},
            upsert=True,
        )

    updated_inst = await db.institutes.find_one({"id": iid}, {"_id": 0})

    return {
        "activated": subscription_status == "active",
        "status": subscription_status,
        "subscriptionId": sub["id"],
        "institute": updated_inst,
    }



@api.post("/admin/approve-institute")
async def approve_institute(data: ApprovalReq, user=Depends(auth)):
    if user.get("role") != "superadmin":
        raise HTTPException(403, "Super-admin only.")

    action = data.action.lower()
    if action not in ("approve", "reject"):
        raise HTTPException(400, "action must be 'approve' or 'reject'.")

    new_status = "active" if action == "approve" else "rejected"

    await db.institutes.update_one(
        {"id": data.institute_id},
        {"$set": {"subscriptionStatus": new_status}},
    )

    await db.subscriptions.update_one(
        {"instituteId": data.institute_id, "status": "pending_approval"},
        {"$set": {
            "status": new_status,
            "approvalNote": data.note,
            "approvedAt": now_iso(),
        }},
    )

    if new_status == "active":
        inst = await db.institutes.find_one({"id": data.institute_id}, {"_id": 0})
        if inst:
            await db.institute_plans.update_one(
                {"instituteId": data.institute_id},
                {"$set": {"plan": inst.get("plan", "standard"), "updatedAt": now_iso()}},
                upsert=True,
            )

    return {"ok": True, "status": new_status}


@api.get("/admin/pending-approvals")
async def list_pending_approvals(user=Depends(auth)):
    if user.get("role") != "superadmin":
        raise HTTPException(403, "Super-admin only.")

    insts = await db.institutes.find(
        {"subscriptionStatus": "pending_approval"},
        {"_id": 0},
    ).sort("createdAt", -1).to_list(100)

    for inst in insts:
        inst["subscription"] = await db.subscriptions.find_one(
            {"instituteId": inst["id"], "status": "pending_approval"},
            {"_id": 0},
        )

    return insts







# ─── Models ───

class UserRegister(BaseModel):
    name: str
    email: str
    password: str
    role: str = "admin"
    instituteName: str = ""


class UserLogin(BaseModel):
    email: str
    password: str


class StudentCreate(BaseModel):
    name: str
    phoneNumber: str = ""
    parentPhoneNumber: str = ""
    email: str = ""
    batchId: str = ""
    admissionDate: str = ""


class TeacherCreate(BaseModel):
    name: str
    phoneNumber: str = ""
    email: str = ""
    subjectExpertise: str = ""
    assignedBatches: List[str] = []
    joiningDate: str = ""
    salary: float = 0


class BatchCreate(BaseModel):
    batchName: str
    courseName: str = ""
    subject: str = ""
    teacherId: str = ""
    classDuration: str = ""
    scheduleDays: List[str] = []
    startDate: str = ""
    maxStudents: int = 30


class AttendanceMark(BaseModel):
    batchId: str
    date: str
    records: List[dict]  # {studentId, status: present|absent|late}


class LiveClassCreate(BaseModel):
    title: str
    batchId: str
    startTime: str  # ISO datetime
    endTime: str    # ISO datetime
    recordingEnabled: bool = False


class FeeStructureCreate(BaseModel):
    batchId: str
    totalCourseFee: float
    paymentPlan: str
    firstDueDate: str
    numberOfInstallments: int = 1
    lateFeePerDay: float = 0


class FeeAssign(BaseModel):
    studentId: str
    feeStructureId: str
    paymentPlan: Optional[str] = None


class FeePaymentReq(BaseModel):
    studentFeeId: str
    installmentIndex: int
    amount: float


class TestCreate(BaseModel):
    testName: str
    subject: str = ""
    batchId: str
    maximumMarks: float = 100
    testDate: str = ""


class MarksEntry(BaseModel):
    marks: List[dict]


# ─── GOOGLE SERVICE (Mock - swap for real API later) ───

class GoogleService:
    """Mock Google service. Replace internals with real Google Calendar/Drive API calls."""

    @staticmethod
    def generate_meet_link():
        """Generate a realistic-looking Google Meet link."""
        import string
        chars = string.ascii_lowercase
        def seg(n):
            return ''.join(random.choices(chars, k=n))
        return f"https://meet.google.com/{seg(3)}-{seg(4)}-{seg(3)}"

    @staticmethod
    def create_calendar_event(title, start_time, end_time, attendees=None):
        """Mock: Would create Google Calendar event with Meet link."""
        return {
            "eventId": f"evt_{uid()[:8]}",
            "meetLink": GoogleService.generate_meet_link(),
            "calendarLink": f"https://calendar.google.com/event/{uid()[:12]}"
        }

    @staticmethod
    def check_drive_for_recording(class_title, start_time):
        """Mock: Would search Google Drive for recording files matching this class."""
        # In real implementation: use Drive API to search by title/timestamp
        return None  # Return file_id if found, None otherwise

    @staticmethod
    def download_from_drive(file_id):
        """Mock: Would download file from Google Drive."""
        return b""  # Return bytes in real implementation

    @staticmethod
    def delete_from_drive(file_id):
        """Mock: Would delete file from Google Drive to save storage."""
        return True

    @staticmethod
    def upload_to_r2(class_id, file_bytes):
        """Mock: Would upload to Cloudflare R2. Returns URL."""
        return f"https://r2.growcad.in/recordings/{class_id}.mp4"


google_service = GoogleService()


async def get_institute_plan(institute_id):
    """Get the plan for an institute. Defaults to 'standard' if not set."""
    plan = await db.institute_plans.find_one({"instituteId": institute_id}, {"_id": 0})
    if not plan:
        return "standard"
    return plan.get("plan", "standard")

def _safe_user(u: dict):
    return {k: v for k, v in u.items() if k not in ("passwordHash", "_id")}

def _resolve_slug(request: Request, body_slug: str = None):
    header = request.headers.get("X-Institute-Slug", "").strip().lower()
    return header or (body_slug.strip().lower() if body_slug else None)
# ─── AUTH ───

@api.post("/auth/register")
async def register(data: UserRegister):
    if await db.users.find_one({"email": data.email}):
        raise HTTPException(400, "Email already exists")
    iid = uid()
    if data.instituteName:
        await db.institutes.insert_one({"id": iid, "name": data.instituteName, "createdAt": now_iso()})
    user = {
        "id": uid(), "name": data.name, "email": data.email,
        "passwordHash": hash_pw(data.password), "role": data.role,
        "instituteId": iid, "createdAt": now_iso()
    }
    await db.users.insert_one(user)
    token = make_token(user["id"], user["role"], iid)
    return {"token": token, "user": {k: v for k, v in user.items() if k not in ("passwordHash", "_id")}}


@api.post("/auth/login")
async def login(data: UserLogin):
    email = data.email.strip().lower()

    u = await db.users.find_one({"email": email}, {"_id": 0})

    if not u or not check_pw(data.password, u["passwordHash"]):
        raise HTTPException(401, "Invalid email or password.")

    if u.get("role") not in ("admin", "superadmin"):
        raise HTTPException(403, "Only admin can login from main domain")


    iid = u.get("instituteId", "")
    

    institute = None
    if iid:
        institute = await db.institutes.find_one({"id": iid}, {"_id": 0})

    slug = institute["slug"] if institute else None
    token = make_token(u["id"], u["role"], iid, slug)

    return {
        "token": token,
        "user": _safe_user(u),
        "institute": institute
    }


@api.post("/auth/admin-login")
async def admin_login_alias(data: UserLogin):
    return await login(data)


@api.get("/auth/me")
async def me(user=Depends(auth)):
    return _safe_user(user)


# ─── DASHBOARD ───

@api.get("/dashboard/stats")
async def dashboard_stats(user=Depends(auth)):
    role = user["role"]

    if role == "student":
        return await _student_dashboard(user)
    elif role == "teacher":
        return await _teacher_dashboard(user)
    else:
        return await _admin_dashboard(user)


async def _admin_dashboard(user):
    iid = user["instituteId"]
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_day  = datetime.now(timezone.utc).strftime("%A")

    # ── 1. All independent queries in parallel ────────────────────────────
    (
        student_count,
        teacher_count,
        fees,
        att_agg,
        today_att_agg,
        batches,
        notifs,
        announcements,
    ) = await asyncio.gather(
        db.students.count_documents({"instituteId": iid}),
        db.teachers.count_documents({"instituteId": iid}),
        db.student_fees.find(
            {"instituteId": iid},
            {"_id": 0, "totalPaid": 1, "totalPending": 1, "installments": 1, "studentId": 1},
        ).to_list(2000),
        db.attendance.aggregate([
            {"$match": {"instituteId": iid}},
            {"$group": {
                "_id":     None,
                "present": {"$sum": {"$cond": [{"$in": ["$status", ["present", "late"]]}, 1, 0]}},
                "total":   {"$sum": 1},
            }},
        ]).to_list(1),
        db.attendance.aggregate([
            {"$match": {"instituteId": iid, "date": today_str}},
            {"$group": {
                "_id":     None,
                "present": {"$sum": {"$cond": [{"$in": ["$status", ["present", "late"]]}, 1, 0]}},
                "total":   {"$sum": 1},
            }},
        ]).to_list(1),
        db.batches.find({"instituteId": iid}, {"_id": 0}).to_list(200),
        db.notifications.find({"instituteId": iid}, {"_id": 0}).sort("createdAt", -1).to_list(5),
        db.announcements.find({"instituteId": iid}, {"_id": 0}).sort("createdAt", -1).to_list(5),
    )

    # ── 2. Compute fee totals (Python, no extra DB hits) ──────────────────
    total_paid    = sum(f.get("totalPaid", 0)    for f in fees)
    total_pending = sum(f.get("totalPending", 0) for f in fees)

    monthly: dict = {}
    for f in fees:
        for inst in f.get("installments", []):
            pd = inst.get("paidDate")
            if pd:
                m = pd[:7]
                monthly[m] = monthly.get(m, 0) + inst.get("paidAmount", 0)

    # ── 3. Attendance rates from aggregation results ───────────────────────
    att_r = att_agg[0] if att_agg else {"present": 0, "total": 0}
    att_rate = round(att_r["present"] / att_r["total"] * 100, 1) if att_r["total"] else 0

    today_r = today_att_agg[0] if today_att_agg else {"present": 0, "total": 0}
    today_att_rate = round(today_r["present"] / today_r["total"] * 100, 1) if today_r["total"] else 0

    # ── 4. Today's classes (pure Python — batches already fetched) ─────────
    today_classes = [b for b in batches if today_day in b.get("scheduleDays", [])]

    # ── 5. Dues today — bulk student fetch, no loop queries ───────────────
    due_student_ids = [
        f["studentId"]
        for f in fees
        for inst in f.get("installments", [])
        if inst.get("dueDate", "")[:10] == today_str and inst.get("status") != "paid"
    ]
    students_map = await _get_students_map(iid, due_student_ids) if due_student_ids else {}

    dues_today = []
    for f in fees:
        if len(dues_today) >= 5:
            break
        for inst in f.get("installments", []):
            if inst.get("dueDate", "")[:10] == today_str and inst.get("status") != "paid":
                s = students_map.get(f["studentId"], {})
                if s:
                    dues_today.append({
                        "studentName": s.get("name", ""),
                        "amount":      inst.get("amount", 0),
                        "studentId":   f["studentId"],
                    })
                break

    return {
        "role":               "admin",
        "totalStudents":      student_count,
        "totalTeachers":      teacher_count,
        "monthlyRevenue":     total_paid,
        "pendingRevenue":     total_pending,
        "attendanceRate":     att_rate,
        "todayAttendanceRate": today_att_rate,
        "monthlyFees":        monthly,
        "todayClasses":       today_classes[:5],
        "dueToday":           dues_today[:5],
        "notifications":      notifs,
        "totalBatches":       len(batches),
        "announcements":      announcements,
    }


    

async def _student_dashboard(user):
    iid = user["instituteId"]
    sid = user.get("studentId", "")

    # ── All independent queries in parallel ───────────────────────────────
    student, att_agg, fees, marks, notifs = await asyncio.gather(
        db.students.find_one({"id": sid, "instituteId": iid}, {"_id": 0}),
        db.attendance.aggregate([
            {"$match": {"studentId": sid, "instituteId": iid}},
            {"$group": {
                "_id":     None,
                "present": {"$sum": {"$cond": [{"$eq": ["$status", "present"]}, 1, 0]}},
                "late":    {"$sum": {"$cond": [{"$eq": ["$status", "late"]},    1, 0]}},
                "absent":  {"$sum": {"$cond": [{"$eq": ["$status", "absent"]},  1, 0]}},
                "total":   {"$sum": 1},
            }},
        ]).to_list(1),
        db.student_fees.find({"studentId": sid, "instituteId": iid}, {"_id": 0}).to_list(10),
        db.marks.find({"studentId": sid, "instituteId": iid}, {"_id": 0}).to_list(100),
        db.notifications.find({"instituteId": iid}, {"_id": 0}).sort("createdAt", -1).to_list(5),
    )

    # ── Batch + tests in second parallel round (depends on student) ───────
    batch_id = student.get("batchId", "") if student else ""
    test_ids  = [m["testId"] for m in marks]

    async def _none():
        return None

    batch, tests_raw, announcements = await asyncio.gather(
        db.batches.find_one({"id": batch_id}, {"_id": 0}) if batch_id else _none(),
        db.tests.find({"id": {"$in": test_ids}}, {"_id": 0}).to_list(100) if test_ids else _none(),
        db.announcements.find(
            {"instituteId": iid, "$or": [{"targetBatchId": ""}, {"targetBatchId": batch_id}]},
            {"_id": 0},
        ).sort("createdAt", -1).to_list(5),
    )

    # ── Attendance summary ────────────────────────────────────────────────
    att_r = att_agg[0] if att_agg else {"present": 0, "late": 0, "absent": 0, "total": 0}
    att_rate = round((att_r["present"] + att_r.get("late", 0)) / att_r["total"] * 100, 1) if att_r["total"] else 0

    # ── Fee summary ───────────────────────────────────────────────────────
    total_fee     = sum(f.get("totalFee",     0) for f in fees)
    total_paid    = sum(f.get("totalPaid",    0) for f in fees)
    total_pending = sum(f.get("totalPending", 0) for f in fees)
    next_due = None
    for f in fees:
        for inst in f.get("installments", []):
            if inst.get("status") != "paid":
                next_due = {"amount": inst["amount"], "dueDate": inst["dueDate"]}
                break
        if next_due:
            break

    # ── Test results (use map — no per-mark DB call) ──────────────────────
    tests_map = {t["id"]: t for t in (tests_raw or [])}
    test_results = []
    for m in marks:
        test = tests_map.get(m["testId"])
        if test:
            max_m = test["maximumMarks"]
            test_results.append({
                "testName":      test["testName"],
                "subject":       test.get("subject", ""),
                "marksObtained": m["marksObtained"],
                "maximumMarks":  max_m,
                "percentage":    round(m["marksObtained"] / max_m * 100, 1) if max_m else 0,
                "testDate":      test.get("testDate", ""),
            })

    return {
        "role":    "student",
        "student": student,
        "batch":   batch,
        "attendanceSummary": {
            "present": att_r["present"],
            "absent":  att_r["absent"],
            "total":   att_r["total"],
            "rate":    att_rate,
        },
        "feeSummary": {
            "totalFee":     total_fee,
            "totalPaid":    total_paid,
            "totalPending": total_pending,
            "nextDue":      next_due,
        },
        "testResults":   test_results,
        "notifications": notifs,
        "fees":          fees,
        "announcements": announcements,
    }


# ─── STUDENTS ───

@api.get("/students")
async def list_students(user=Depends(auth), batchId: str = "", search: str = ""):
    q = {"instituteId": user["instituteId"]}
    # Teachers can only see students in their assigned batches
    if user["role"] == "teacher":
        batch_ids = await get_teacher_batch_ids(user)
        if batchId and batchId in batch_ids:
            q["batchId"] = batchId
        elif batch_ids:
            q["batchId"] = {"$in": batch_ids}
        else:
            return []
    elif user["role"] == "student":
        # Students can only see themselves
        q["id"] = user.get("studentId", "")
    else:
        if batchId:
            q["batchId"] = batchId
    if search:
        q["name"] = {"$regex": search, "$options": "i"}
    return await db.students.find(q, {"_id": 0}).to_list(1000)


@api.post("/students")
async def create_student(data: StudentCreate, user=Depends(admin_only)):
    s = {"id": uid(), **data.model_dump(), "instituteId": user["instituteId"], "createdAt": now_iso()}
    await db.students.insert_one(s)
    s.pop("_id", None)
    if data.email:
        existing = await db.users.find_one({"email": data.email})
        if not existing:
            su = {"id": uid(), "name": data.name, "email": data.email, "passwordHash": hash_pw("student123"),
                  "role": "student", "instituteId": user["instituteId"], "studentId": s["id"], "createdAt": now_iso()}
            await db.users.insert_one(su)
    if data.batchId:
        fs = await db.fee_structures.find_one({"batchId": data.batchId, "instituteId": user["instituteId"]}, {"_id": 0})
        if fs:
            await _assign_student_fee(s["id"], fs, user["instituteId"])
    return s


@api.put("/students/{sid}")
async def update_student(sid: str, data: StudentCreate, user=Depends(admin_only)):
    update = {k: v for k, v in data.model_dump().items() if v}
    await db.students.update_one({"id": sid, "instituteId": user["instituteId"]}, {"$set": update})
    return await db.students.find_one({"id": sid}, {"_id": 0})


@api.delete("/students/{sid}")
async def delete_student(sid: str, user=Depends(admin_only)):
    await db.students.delete_one({"id": sid, "instituteId": user["instituteId"]})
    return {"ok": True}


@api.post("/students/bulk-upload")
async def bulk_upload_students(file: UploadFile = File(...), user=Depends(admin_only)):
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    iid = user["instituteId"]

    # Pre-load async def _teacher_dashboard(user):batches for name->id lookup
    all_batches = await db.batches.find({"instituteId": iid}, {"_id": 0}).to_list(200)
    batch_map = {b["batchName"].strip().lower(): b["id"] for b in all_batches}

    success = []
    failed = []
    for row_num, row in enumerate(reader, start=2):
        errors = []
        name = (row.get("name") or "").strip()
        phone = (row.get("phone") or row.get("phoneNumber") or "").strip()
        parent_phone = (row.get("parentPhone") or row.get("parentPhoneNumber") or "").strip()
        email = (row.get("email") or "").strip()
        batch_name = (row.get("batch") or row.get("batchName") or "").strip()

        if not name:
            errors.append("Name is required")
        batch_id = ""
        if batch_name:
            batch_id = batch_map.get(batch_name.lower(), "")
            if not batch_id:
                errors.append(f"Batch '{batch_name}' not found")

        if email:
            existing_email = await db.students.find_one({"email": email, "instituteId": iid})
            if existing_email:
                errors.append(f"Email '{email}' already exists")

        if errors:
            failed.append({"row": row_num, "data": name or f"Row {row_num}", "errors": errors})
            continue

        sid = uid()
        s = {
            "id": sid, "name": name, "phoneNumber": phone,
            "parentPhoneNumber": parent_phone, "email": email,
            "batchId": batch_id, "admissionDate": now_iso()[:10],
            "instituteId": iid, "createdAt": now_iso()
        }
        await db.students.insert_one(s)
        s.pop("_id", None)

        # Create user account if email provided
        if email:
            existing_user = await db.users.find_one({"email": email})
            if not existing_user:
                await db.users.insert_one({
                    "id": uid(), "name": name, "email": email,
                    "passwordHash": hash_pw("student123"), "role": "student",
                    "instituteId": iid, "studentId": sid, "createdAt": now_iso()
                })

        # Auto-assign fee structure
        if batch_id:
            fs = await db.fee_structures.find_one({"batchId": batch_id, "instituteId": iid}, {"_id": 0})
            if fs:
                await _assign_student_fee(sid, fs, iid)

        success.append({"name": name, "email": email, "batch": batch_name})

    return {
        "summary": {"total": len(success) + len(failed), "success": len(success), "failed": len(failed)},
        "success": success, "failed": failed
    }


# ─── TEACHERS ───

@api.get("/teachers")
async def list_teachers(user=Depends(auth), search: str = ""):
    q = {"instituteId": user["instituteId"]}
    if search:
        q["name"] = {"$regex": search, "$options": "i"}
    return await db.teachers.find(q, {"_id": 0}).to_list(1000)


@api.post("/teachers")
async def create_teacher(data: TeacherCreate, user=Depends(admin_only)):
    t = {"id": uid(), **data.model_dump(), "instituteId": user["instituteId"], "createdAt": now_iso()}
    await db.teachers.insert_one(t)
    t.pop("_id", None)
    if data.email:
        existing = await db.users.find_one({"email": data.email})
        if not existing:
            tu = {"id": uid(), "name": data.name, "email": data.email, "passwordHash": hash_pw("teacher123"),
                  "role": "teacher", "instituteId": user["instituteId"], "teacherId": t["id"], "createdAt": now_iso()}
            await db.users.insert_one(tu)
    await db.notifications.insert_one({
        "id": uid(), "title": "New Teacher Added",
        "message": f"{data.name} has joined as {data.subjectExpertise} teacher.",
        "type": "teacher", "createdAt": now_iso(), "instituteId": user["instituteId"], "read": False
    })
    return t


@api.put("/teachers/{tid}")
async def update_teacher(tid: str, data: TeacherCreate, user=Depends(admin_only)):
    update = {k: v for k, v in data.model_dump().items() if v or v == 0}
    await db.teachers.update_one({"id": tid, "instituteId": user["instituteId"]}, {"$set": update})
    return await db.teachers.find_one({"id": tid}, {"_id": 0})


@api.delete("/teachers/{tid}")
async def delete_teacher(tid: str, user=Depends(admin_only)):
    await db.teachers.delete_one({"id": tid, "instituteId": user["instituteId"]})
    return {"ok": True}


@api.post("/teachers/bulk-upload")
async def bulk_upload_teachers(file: UploadFile = File(...), user=Depends(admin_only)):
    content = await file.read()
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = content.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text))
    iid = user["instituteId"]

    success = []
    failed = []
    for row_num, row in enumerate(reader, start=2):
        errors = []
        name = (row.get("name") or "").strip()
        phone = (row.get("phone") or row.get("phoneNumber") or "").strip()
        email = (row.get("email") or "").strip()
        subject = (row.get("subject") or row.get("subjectExpertise") or "").strip()

        if not name:
            errors.append("Name is required")

        if email:
            existing_email = await db.teachers.find_one({"email": email, "instituteId": iid})
            if existing_email:
                errors.append(f"Email '{email}' already exists")

        if errors:
            failed.append({"row": row_num, "data": name or f"Row {row_num}", "errors": errors})
            continue

        tid = uid()
        t = {
            "id": tid, "name": name, "phoneNumber": phone,
            "email": email, "subjectExpertise": subject,
            "assignedBatches": [], "joiningDate": now_iso()[:10],
            "salary": 0, "instituteId": iid, "createdAt": now_iso()
        }
        await db.teachers.insert_one(t)
        t.pop("_id", None)

        # Create user account if email provided
        if email:
            existing_user = await db.users.find_one({"email": email})
            if not existing_user:
                await db.users.insert_one({
                    "id": uid(), "name": name, "email": email,
                    "passwordHash": hash_pw("teacher123"), "role": "teacher",
                    "instituteId": iid, "teacherId": tid, "createdAt": now_iso()
                })

        success.append({"name": name, "email": email, "subject": subject})

    return {
        "summary": {"total": len(success) + len(failed), "success": len(success), "failed": len(failed)},
        "success": success, "failed": failed
    }


# ─── BATCHES ───



# NEW — two bulk queries, zero loop DB calls
@api.get("/batches")
async def list_batches(user=Depends(auth)):
    iid = user["instituteId"]

    if user["role"] == "teacher":
        batch_ids = await get_teacher_batch_ids(user)
        if not batch_ids:
            return []
        batches = await db.batches.find(
            {"id": {"$in": batch_ids}, "instituteId": iid}, {"_id": 0}
        ).to_list(100)
    elif user["role"] == "student":
        student = await db.students.find_one(
            {"id": user.get("studentId", ""), "instituteId": iid}, {"_id": 0}
        )
        if student and student.get("batchId"):
            batches = await db.batches.find(
                {"id": student["batchId"], "instituteId": iid}, {"_id": 0}
            ).to_list(1)
        else:
            return []
    else:
        batches = await db.batches.find({"instituteId": iid}, {"_id": 0}).to_list(200)

    if not batches:
        return []

    bid_list = [b["id"] for b in batches]
    tid_list = [b.get("teacherId", "") for b in batches if b.get("teacherId")]

    # Parallel: student counts per batch + teacher lookup
    count_agg, teachers_map = await asyncio.gather(
        db.students.aggregate([
            {"$match": {"batchId": {"$in": bid_list}, "instituteId": iid}},
            {"$group": {"_id": "$batchId", "count": {"$sum": 1}}},
        ]).to_list(200),
        _get_teachers_map(iid, tid_list),
    )

    count_map = {r["_id"]: r["count"] for r in count_agg}

    for b in batches:
        b["studentCount"] = count_map.get(b["id"], 0)
        b["teacherName"]  = teachers_map.get(b.get("teacherId", ""), {}).get("name", "")

    return batches

@api.post("/batches")
async def create_batch(data: BatchCreate, user=Depends(admin_only)):
    b = {"id": uid(), **data.model_dump(), "instituteId": user["instituteId"], "createdAt": now_iso()}
    await db.batches.insert_one(b)
    b.pop("_id", None)
    return b


@api.put("/batches/{bid}")
async def update_batch(bid: str, data: BatchCreate, user=Depends(admin_only)):
    update = {k: v for k, v in data.model_dump().items() if v or isinstance(v, list) or v == 0}
    await db.batches.update_one({"id": bid, "instituteId": user["instituteId"]}, {"$set": update})
    return await db.batches.find_one({"id": bid}, {"_id": 0})


@api.delete("/batches/{bid}")
async def delete_batch(bid: str, user=Depends(admin_only)):
    await db.batches.delete_one({"id": bid, "instituteId": user["instituteId"]})
    return {"ok": True}


# ─── ATTENDANCE ───



@api.post("/attendance/mark")
async def mark_attendance(
    data: AttendanceMark,
    background_tasks: BackgroundTasks,
    user=Depends(teacher_or_admin)
):
    iid = user["instituteId"]

    operations = []

    for r in data.records:
        operations.append(
            UpdateOne(
                {
                    "studentId": r["studentId"],
                    "batchId": data.batchId,
                    "date": data.date,
                    "instituteId": iid
                },
                {
                    "$set": {"status": r["status"]},
                    "$setOnInsert": {"id": uid()}
                },
                upsert=True
            )
        )

    if operations:
        await db.attendance.bulk_write(operations)

    # 🚀 BACKGROUND TASK (UNCHANGED)
    absent_ids = [r["studentId"] for r in data.records if r["status"] == "absent"]

    if absent_ids:
        background_tasks.add_task(
            _send_absent_alerts,
            absent_ids,
            data.batchId,
            data.date,
            iid
        )

    return {"ok": True}


async def _send_absent_alerts(student_ids, batch_id, date, iid):
    """Send absent alerts via configured channels. Dedup: 1 per student per day."""
    settings = await _get_reminder_settings(iid)
    channels = settings.get("channels", ["in_app"])
    institute = await db.institutes.find_one({"id": iid}, {"_id": 0})
    inst_name = institute.get("name", "Institute") if institute else "Institute"
    batch = await db.batches.find_one({"id": batch_id}, {"_id": 0})
    batch_name = batch.get("batchName", "") if batch else ""
    today_str = now_iso()

    for sid in student_ids:
        # Dedup check
        existing = await db.absent_alerts.find_one({
            "studentId": sid, "date": date, "instituteId": iid
        })
        if existing:
            continue

        student = await db.students.find_one({"id": sid, "instituteId": iid}, {"_id": 0})
        if not student:
            continue
        student_name = student.get("name", "Student")
        parent_phone = student.get("parentPhoneNumber", "")
        phone = student.get("phoneNumber", "")
        msg = (f"Hello, {student_name} was marked absent in {batch_name} on {date}. "
               f"Please contact the institute for details. - {inst_name}")

        # In-app notification
        if "in_app" in channels:
            await db.notifications.insert_one({
                "id": uid(), "title": "Absent Alert",
                "message": msg, "type": "absent_alert",
                "createdAt": today_str, "instituteId": iid, "read": False
            })

        # SMS
        if "sms" in channels and twilio_client and TWILIO_PHONE:
            for target in [phone, parent_phone]:
                if not target:
                    logger.info(f"Absent alert SMS skipped for {student_name}: no phone")
                    continue
                try:
                    twilio_client.messages.create(body=msg, from_=TWILIO_PHONE, to=target)
                except Exception as e:
                    logger.error(f"Absent SMS failed for {target}: {e}")

        # WhatsApp
        if "whatsapp" in channels and twilio_client and TWILIO_WA:
            for target in [phone, parent_phone]:
                if not target:
                    logger.info(f"Absent alert WA skipped for {student_name}: no phone")
                    continue
                try:
                    twilio_client.messages.create(
                        body=msg, from_=f"whatsapp:{TWILIO_WA}", to=f"whatsapp:{target}")
                except Exception as e:
                    logger.error(f"Absent WA failed for {target}: {e}")

        # Log the alert
        await db.absent_alerts.insert_one({
            "id": uid(), "studentId": sid, "studentName": student_name,
            "batchId": batch_id, "batchName": batch_name, "date": date,
            "phone": phone, "parentPhone": parent_phone,
            "channels": channels, "message": msg,
            "timestamp": today_str, "instituteId": iid
        })


@api.get("/attendance")
async def get_attendance(user=Depends(auth), batchId: str = "", date: str = "", studentId: str = ""):
    q = {"instituteId": user["instituteId"]}
    if user["role"] == "student":
        q["studentId"] = user.get("studentId", "")
    elif user["role"] == "teacher":
        batch_ids = await get_teacher_batch_ids(user)
        if batchId and batchId in batch_ids:
            q["batchId"] = batchId
        elif batch_ids:
            q["batchId"] = {"$in": batch_ids}
        else:
            return []
    else:
        if batchId:
            q["batchId"] = batchId
    if date:
        q["date"] = date
    if studentId and user["role"] != "student":
        q["studentId"] = studentId
    return await db.attendance.find(q, {"_id": 0}).to_list(1000)


# ─── FEES ───

async def _assign_student_fee(student_id, fee_structure, institute_id):
    installments = []
    total = fee_structure["totalCourseFee"]
    n = fee_structure.get("numberOfInstallments", 1)
    plan = fee_structure.get("paymentPlan", "one_time")
    first_due = fee_structure.get("firstDueDate", now_iso()[:10])
    try:
        base_date = datetime.strptime(first_due, "%Y-%m-%d")
    except Exception:
        base_date = datetime.now(timezone.utc)
    amt_per = round(total / n, 2)
    months_map = {"one_time": 0, "monthly": 1, "quarterly": 3, "half_yearly": 6, "annually": 12, "custom": 1}
    gap = months_map.get(plan, 1)
    for i in range(n):
        due = base_date + timedelta(days=gap * 30 * i)
        installments.append({
            "index": i, "amount": amt_per, "dueDate": due.strftime("%Y-%m-%d"),
            "status": "pending", "paidAmount": 0, "paidDate": None
        })
    sf = {
        "id": uid(), "studentId": student_id, "feeStructureId": fee_structure["id"],
        "batchId": fee_structure["batchId"], "totalFee": total, "paymentPlan": plan,
        "installments": installments, "totalPaid": 0, "totalPending": total,
        "instituteId": institute_id, "createdAt": now_iso()
    }
    await db.student_fees.insert_one(sf)
    sf.pop("_id", None)
    return sf


@api.get("/fee-structures")
async def list_fee_structures(user=Depends(auth)):
    fss = await db.fee_structures.find({"instituteId": user["instituteId"]}, {"_id": 0}).to_list(100)
    batch_ids = [fs.get("batchId", "") for fs in fss]

    batches_map = await _get_batches_map(user["instituteId"], batch_ids)

    for fs in fss:
        fs["batchName"] = batches_map.get(fs.get("batchId", ""), {}).get("batchName", "")
    return fss


@api.post("/fee-structures")
async def create_fee_structure(data: FeeStructureCreate, user=Depends(admin_only)):
    fs = {"id": uid(), **data.model_dump(), "instituteId": user["instituteId"], "createdAt": now_iso()}
    await db.fee_structures.insert_one(fs)
    fs.pop("_id", None)
    return fs


@api.post("/fees/assign")
async def assign_fee(data: FeeAssign, user=Depends(admin_only)):
    fs = await db.fee_structures.find_one({"id": data.feeStructureId, "instituteId": user["instituteId"]}, {"_id": 0})
    if not fs:
        raise HTTPException(404, "Fee structure not found")
    if data.paymentPlan:
        fs["paymentPlan"] = data.paymentPlan
    sf = await _assign_student_fee(data.studentId, fs, user["instituteId"])
    return sf


@api.get("/student-fees")
async def list_student_fees(user=Depends(auth), studentId: str = "", batchId: str = ""):
    iid = user["instituteId"]

    q = {"instituteId": iid}

    if user["role"] == "student":
        q["studentId"] = user.get("studentId", "")
    else:
        if studentId:
            q["studentId"] = studentId
        if batchId:
            q["batchId"] = batchId

    fees = await db.student_fees.find(q, {"_id": 0}).to_list(1000)

    if not fees:
        return []

    # 🔥 BULK FETCH
    student_ids = list({f["studentId"] for f in fees})
    batch_ids = list({f.get("batchId", "") for f in fees})

    students_map, batches_map = await asyncio.gather(
        _get_students_map(iid, student_ids),
        _get_batches_map(iid, batch_ids),
    )

    # 🚀 NO DB CALLS INSIDE LOOP
    for f in fees:
        f["studentName"] = students_map.get(f["studentId"], {}).get("name", "")
        f["batchName"] = batches_map.get(f.get("batchId", ""), {}).get("batchName", "")

    return fees


@api.post("/fees/pay")
async def pay_fee(data: FeePaymentReq, user=Depends(admin_only)):
    sf = await db.student_fees.find_one({"id": data.studentFeeId, "instituteId": user["instituteId"]})
    if not sf:
        raise HTTPException(404, "Student fee not found")
    installments = sf["installments"]
    if data.installmentIndex >= len(installments):
        raise HTTPException(400, "Invalid installment index")
    inst = installments[data.installmentIndex]
    inst["paidAmount"] = data.amount
    inst["paidDate"] = now_iso()[:10]
    inst["status"] = "paid" if data.amount >= inst["amount"] else "partial"
    total_paid = sum(i.get("paidAmount", 0) for i in installments)
    total_pending = sf["totalFee"] - total_paid
    await db.student_fees.update_one(
        {"_id": sf["_id"]},
        {"$set": {"installments": installments, "totalPaid": total_paid, "totalPending": total_pending}}
    )
    student = await db.students.find_one({"id": sf["studentId"]}, {"_id": 0})
    await db.notifications.insert_one({
        "id": uid(), "title": "Fee Payment Received",
        "message": f"Payment of Rs.{data.amount} received from {student['name'] if student else 'student'}",
        "type": "fee", "createdAt": now_iso(), "instituteId": user["instituteId"], "read": False
    })
    updated = await db.student_fees.find_one({"id": data.studentFeeId}, {"_id": 0})
    return updated


# ─── TESTS ───

@api.get("/tests")
async def list_tests(user=Depends(auth), batchId: str = ""):
    iid = user["instituteId"]
    q = {"instituteId": iid}

    if user["role"] == "teacher":
        batch_ids = await get_teacher_batch_ids(user)
        if batchId and batchId in batch_ids:
            q["batchId"] = batchId
        elif batch_ids:
            q["batchId"] = {"$in": batch_ids}
        else:
            return []

    elif user["role"] == "student":
        student = await db.students.find_one(
            {"id": user.get("studentId", ""), "instituteId": iid},
            {"_id": 0}
        )
        if student and student.get("batchId"):
            q["batchId"] = student["batchId"]
        else:
            return []

    else:
        if batchId:
            q["batchId"] = batchId

    tests = await db.tests.find(q, {"_id": 0}).to_list(100)

    if not tests:
        return []

    # 🔥 BULK FETCH BATCHES
    batch_ids = list({t.get("batchId", "") for t in tests})

    # 🔥 BULK COUNT MARKS
    test_ids = [t["id"] for t in tests]

    batches_map, marks_agg = await asyncio.gather(
        _get_batches_map(iid, batch_ids),
        db.marks.aggregate([
            {"$match": {"testId": {"$in": test_ids}}},
            {"$group": {"_id": "$testId", "count": {"$sum": 1}}},
        ]).to_list(100),
    )

    marks_map = {m["_id"]: m["count"] for m in marks_agg}

    for t in tests:
        t["batchName"] = batches_map.get(t.get("batchId", ""), {}).get("batchName", "")
        t["marksCount"] = marks_map.get(t["id"], 0)

    return tests


@api.post("/tests")
async def create_test(data: TestCreate, user=Depends(teacher_or_admin)):
    t = {"id": uid(), **data.model_dump(), "instituteId": user["instituteId"], "createdAt": now_iso()}
    await db.tests.insert_one(t)
    t.pop("_id", None)
    return t


@api.delete("/tests/{test_id}")
async def delete_test(test_id: str, user=Depends(teacher_or_admin)):
    await db.tests.delete_one({"id": test_id, "instituteId": user["instituteId"]})
    await db.marks.delete_many({"testId": test_id, "instituteId": user["instituteId"]})
    return {"ok": True}


@api.post("/tests/{test_id}/marks")
async def upload_marks(test_id: str, data: MarksEntry, user=Depends(teacher_or_admin)):
    test = await db.tests.find_one({"id": test_id, "instituteId": user["instituteId"]}, {"_id": 0})
    if not test:
        raise HTTPException(404, "Test not found")
    for m in data.marks:
        existing = await db.marks.find_one({"testId": test_id, "studentId": m["studentId"]})
        if existing:
            await db.marks.update_one({"_id": existing["_id"]}, {"$set": {"marksObtained": m["marksObtained"]}})
        else:
            await db.marks.insert_one({
                "id": uid(), "testId": test_id, "studentId": m["studentId"],
                "marksObtained": m["marksObtained"], "instituteId": user["instituteId"]
            })
    await db.notifications.insert_one({
        "id": uid(), "title": "Test Results Uploaded",
        "message": f"Results for {test['testName']} have been uploaded.",
        "type": "test", "createdAt": now_iso(), "instituteId": user["instituteId"], "read": False
    })
    return {"ok": True}


@api.get("/tests/{test_id}/results")
async def get_test_results(test_id: str, user=Depends(auth)):
    test = await db.tests.find_one({"id": test_id, "instituteId": user["instituteId"]}, {"_id": 0})
    if not test:
        raise HTTPException(404, "Test not found")
    mq = {"testId": test_id, "instituteId": user["instituteId"]}
    if user["role"] == "student":
        mq["studentId"] = user.get("studentId", "")
    marks = await db.marks.find(mq, {"_id": 0}).to_list(1000)
    for m in marks:
        student = await db.students.find_one({"id": m["studentId"]}, {"_id": 0})
        m["studentName"] = student["name"] if student else ""
        m["percentage"] = round(m["marksObtained"] / test["maximumMarks"] * 100, 1) if test["maximumMarks"] else 0
    marks.sort(key=lambda x: x.get("marksObtained", 0), reverse=True)
    return {"test": test, "results": marks}


# ─── REPORTS ───

@api.get("/reports/attendance")
async def attendance_report(
    user=Depends(auth),
    batchId: str = "",
    startDate: str = "",
    endDate: str = "",
):
    iid = user["instituteId"]

    # ── 1. Build match stage ──────────────────────────────────────────────
    match: dict = {"instituteId": iid}
    if batchId:
        match["batchId"] = batchId
    if startDate or endDate:
        date_q: dict = {}
        if startDate:
            date_q["$gte"] = startDate
        if endDate:
            date_q["$lte"] = endDate
        match["date"] = date_q

    # ── 2. Run all three aggregations in PARALLEL ─────────────────────────
    student_pipeline = [
        {"$match": match},
        {"$group": {
            "_id": "$studentId",
            "present": {"$sum": {"$cond": [{"$eq": ["$status", "present"]}, 1, 0]}},
            "late":    {"$sum": {"$cond": [{"$eq": ["$status", "late"]},    1, 0]}},
            "absent":  {"$sum": {"$cond": [{"$eq": ["$status", "absent"]},  1, 0]}},
            "total":   {"$sum": 1},
        }},
    ]

    batch_pipeline = [
        {"$match": match},
        {"$group": {
            "_id": "$batchId",
            "present": {"$sum": {"$cond": [{"$in": ["$status", ["present", "late"]]}, 1, 0]}},
            "total":   {"$sum": 1},
        }},
    ]

    trend_pipeline = [
        {"$match": match},
        {"$group": {
            "_id":     {"$substr": ["$date", 0, 7]},   # YYYY-MM
            "present": {"$sum": {"$cond": [{"$in": ["$status", ["present", "late"]]}, 1, 0]}},
            "total":   {"$sum": 1},
        }},
        {"$sort": {"_id": 1}},
    ]

    student_agg, batch_agg, trend_agg = await asyncio.gather(
        db.attendance.aggregate(student_pipeline).to_list(5000),
        db.attendance.aggregate(batch_pipeline).to_list(500),
        db.attendance.aggregate(trend_pipeline).to_list(100),
    )

    # ── 3. Bulk-fetch names (single query each, no loops) ─────────────────
    sid_list = [r["_id"] for r in student_agg]
    bid_list = [r["_id"] for r in batch_agg]

    students_map, batches_map = await asyncio.gather(
        _get_students_map(iid, sid_list),
        _get_batches_map(iid, bid_list),
    )

    # ── 4. Assemble response (no DB calls inside these loops) ─────────────
    summaries = []
    for r in student_agg:
        sid = r["_id"]
        total = r["total"]
        rate = round((r["present"] + r["late"]) / total * 100, 1) if total else 0
        summaries.append({
            "studentId":   sid,
            "studentName": students_map.get(sid, {}).get("name", ""),
            "present":     r["present"],
            "late":        r["late"],
            "absent":      r["absent"],
            "total":       total,
            "rate":        rate,
        })

    batch_summaries = []
    for r in batch_agg:
        bid = r["_id"]
        total = r["total"]
        rate = round(r["present"] / total * 100, 1) if total else 0
        batch_summaries.append({
            "batchId":   bid,
            "batchName": batches_map.get(bid, {}).get("batchName", ""),
            "present":   r["present"],
            "total":     total,
            "rate":      rate,
        })

    trend = []
    for r in trend_agg:
        total = r["total"]
        rate = round(r["present"] / total * 100, 1) if total else 0
        trend.append({"month": r["_id"], "present": r["present"], "total": total, "rate": rate})

    return {"students": summaries, "batches": batch_summaries, "monthlyTrend": trend}


@api.get("/reports/fees")
async def fees_report(user=Depends(auth), batchId: str = ""):
    iid = user["instituteId"]
    q: dict = {"instituteId": iid}
    if batchId:
        q["batchId"] = batchId

    # ── 1. Aggregate totals per batch + overall in ONE pass ───────────────
    batch_pipeline = [
        {"$match": q},
        {"$group": {
            "_id":       "$batchId",
            "collected": {"$sum": "$totalPaid"},
            "pending":   {"$sum": "$totalPending"},
            "total":     {"$sum": "$totalFee"},
        }},
    ]

    overall_pipeline = [
        {"$match": q},
        {"$group": {
            "_id":            None,
            "totalCollected": {"$sum": "$totalPaid"},
            "totalPending":   {"$sum": "$totalPending"},
            "totalFee":       {"$sum": "$totalFee"},
        }},
    ]

    # Fetch raw fees (capped) for overdue scan + trend; parallel with aggregations
    fees_future = db.student_fees.find(q, {"_id": 0,
        "studentId": 1, "totalPaid": 1, "totalPending": 1,
        "totalFee": 1, "batchId": 1, "installments": 1,
    }).to_list(2000)

    batch_agg, overall_agg, fees = await asyncio.gather(
        db.attendance.aggregate(batch_pipeline).to_list(500)
        if False else db.student_fees.aggregate(batch_pipeline).to_list(500),
        db.student_fees.aggregate(overall_pipeline).to_list(1),
        fees_future,
    )

    # ── 2. Bulk-fetch names ───────────────────────────────────────────────
    bid_list = list({r["_id"] for r in batch_agg})
    sid_list = list({f["studentId"] for f in fees})

    batches_map, students_map = await asyncio.gather(
        _get_batches_map(iid, bid_list),
        _get_students_map(iid, sid_list),
    )

    # ── 3. Assemble batch summaries ───────────────────────────────────────
    batch_summaries = [
        {
            "batchId":   r["_id"],
            "batchName": batches_map.get(r["_id"], {}).get("batchName", ""),
            "collected": r["collected"],
            "pending":   r["pending"],
            "total":     r["total"],
        }
        for r in batch_agg
    ]

    overall = overall_agg[0] if overall_agg else {}

    # ── 4. Overdue scan (Python, but no DB calls — maps already loaded) ───
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    overdue = []
    for f in fees:
        if len(overdue) >= 20:
            break
        student = students_map.get(f["studentId"], {})
        for inst in f.get("installments", []):
            due = inst.get("dueDate", "")
            if inst.get("status") != "paid" and due and due < today:
                overdue.append({
                    "studentName": student.get("name", ""),
                    "amount":      inst["amount"],
                    "dueDate":     due,
                    "studentId":   f["studentId"],
                })
                break  # one overdue entry per student is enough for dashboard

    # ── 5. Monthly collection trend ───────────────────────────────────────
    monthly_collection: dict = {}
    for f in fees:
        for inst in f.get("installments", []):
            pd = inst.get("paidDate")
            if pd:
                m = pd[:7]
                monthly_collection[m] = monthly_collection.get(m, 0) + inst.get("paidAmount", 0)

    collection_trend = [
        {"month": m, "collected": monthly_collection[m]}
        for m in sorted(monthly_collection)
    ]

    return {
        "totalCollected": overall.get("totalCollected", 0),
        "totalPending":   overall.get("totalPending", 0),
        "totalFee":       overall.get("totalFee", 0),
        "batches":        batch_summaries,
        "overdue":        overdue[:20],
        "collectionTrend": collection_trend,
    }


@api.get("/reports/performance")
async def performance_report(user=Depends(auth), batchId: str = "", subject: str = ""):
    iid = user["instituteId"]
    q = {"instituteId": iid}
    if batchId:
        q["batchId"] = batchId
    if subject:
        q["subject"] = {"$regex": subject, "$options": "i"}
    tests = await db.tests.find(q, {"_id": 0}).to_list(100)
    results = []
    student_totals = {}  # Track aggregate student performance across tests

    for test in tests:
        marks = await db.marks.find({"testId": test["id"], "instituteId": iid}, {"_id": 0}).to_list(1000)
        if marks:
            avg = round(sum(m["marksObtained"] for m in marks) / len(marks), 1)
            highest = max(m["marksObtained"] for m in marks)
            lowest = min(m["marksObtained"] for m in marks)
            batch = await db.batches.find_one({"id": test.get("batchId", "")}, {"_id": 0})
            results.append({
                "testName": test["testName"], "subject": test.get("subject", ""),
                "batchName": batch["batchName"] if batch else "",
                "average": avg, "highest": highest, "lowest": lowest,
                "totalStudents": len(marks), "maximumMarks": test["maximumMarks"]
            })
            for m in marks:
                sid = m["studentId"]
                if sid not in student_totals:
                    student_totals[sid] = {"totalMarks": 0, "totalMax": 0, "testCount": 0}
                student_totals[sid]["totalMarks"] += m["marksObtained"]
                student_totals[sid]["totalMax"] += test["maximumMarks"]
                student_totals[sid]["testCount"] += 1

    # Top performing students
    top_students = []
    for sid, data in student_totals.items():
        pct = round(data["totalMarks"] / data["totalMax"] * 100, 1) if data["totalMax"] > 0 else 0
        student = await db.students.find_one({"id": sid, "instituteId": iid}, {"_id": 0})
        if student:
            batch = await db.batches.find_one({"id": student.get("batchId", "")}, {"_id": 0})
            top_students.append({
                "studentId": sid, "studentName": student["name"],
                "batchName": batch["batchName"] if batch else "",
                "totalMarks": round(data["totalMarks"], 1), "totalMax": data["totalMax"],
                "percentage": pct, "testCount": data["testCount"]
            })
    top_students.sort(key=lambda x: x["percentage"], reverse=True)

    return {"tests": results, "topStudents": top_students[:20]}


# ─── NOTIFICATIONS ───

@api.get("/notifications")
async def list_notifications(user=Depends(auth)):
    return await db.notifications.find({"instituteId": user["instituteId"]}, {"_id": 0}).sort("createdAt", -1).to_list(50)


@api.put("/notifications/{nid}/read")
async def mark_notification_read(nid: str, user=Depends(auth)):
    await db.notifications.update_one({"id": nid, "instituteId": user["instituteId"]}, {"$set": {"read": True}})
    return {"ok": True}


@api.put("/notifications/read-all")
async def mark_all_read(user=Depends(auth)):
    await db.notifications.update_many({"instituteId": user["instituteId"]}, {"$set": {"read": True}})
    return {"ok": True}


# ─── SETTINGS ───

@api.get("/settings")
async def get_settings(user=Depends(auth)):
    inst = await db.institutes.find_one({"id": user["instituteId"]}, {"_id": 0})
    return {"institute": inst, "user": {k: v for k, v in user.items() if k != "passwordHash"}}


@api.put("/settings/profile")
async def update_profile(data: dict, user=Depends(auth)):
    allowed = {"name", "email", "phoneNumber"}
    update = {k: v for k, v in data.items() if k in allowed}
    if update:
        await db.users.update_one({"id": user["id"]}, {"$set": update})
    return {"ok": True}


@api.put("/settings/institute")
async def update_institute(data: dict, user=Depends(admin_only)):
    allowed = {"name", "address", "phone", "email", "website"}
    update = {k: v for k, v in data.items() if k in allowed}
    if update:
        await db.institutes.update_one({"id": user["instituteId"]}, {"$set": update})
    return {"ok": True}


# ─── FEE REMINDERS ───

DEFAULT_REMINDER_SETTINGS = {
    "enabled": True,
    "channels": ["in_app"],
    "timing": {"daysBefore": 1, "onDueDate": True, "daysAfterOverdue": 3},
}


async def _get_reminder_settings(institute_id):
    settings = await db.reminder_settings.find_one({"instituteId": institute_id}, {"_id": 0})
    if not settings:
        return {**DEFAULT_REMINDER_SETTINGS, "instituteId": institute_id}
    return settings


@api.get("/settings/reminders")
async def get_reminder_settings(user=Depends(admin_only)):
    settings = await _get_reminder_settings(user["instituteId"])
    twilio_status = {
        "smsAvailable": bool(twilio_client and TWILIO_PHONE),
        "whatsappAvailable": bool(twilio_client and TWILIO_WA),
    }
    return {**settings, "twilioStatus": twilio_status}


@api.put("/settings/reminders")
async def update_reminder_settings(data: dict, user=Depends(admin_only)):
    allowed = {"enabled", "channels", "timing"}
    update = {k: v for k, v in data.items() if k in allowed}
    update["instituteId"] = user["instituteId"]
    update["updatedAt"] = now_iso()
    await db.reminder_settings.update_one(
        {"instituteId": user["instituteId"]}, {"$set": update}, upsert=True
    )
    return {"ok": True}


@api.get("/reminder-logs")
async def get_reminder_logs(user=Depends(admin_only), limit: int = 50, skip: int = 0):
    logs = await db.reminder_logs.find(
        {"instituteId": user["instituteId"]}, {"_id": 0}
    ).sort("timestamp", -1).skip(skip).to_list(limit)
    total = await db.reminder_logs.count_documents({"instituteId": user["instituteId"]})
    return {"logs": logs, "total": total}


@api.get("/dashboard/pending-reminders")
async def pending_reminders(user=Depends(admin_only)):
    iid = user["instituteId"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")

    fees = await db.student_fees.find({"instituteId": iid}, {"_id": 0}).to_list(2000)

    upcoming = []
    overdue = []
    for f in fees:
        student = await db.students.find_one({"id": f["studentId"], "instituteId": iid}, {"_id": 0})
        if not student:
            continue
        for inst in f.get("installments", []):
            if inst.get("status") == "paid":
                continue
            due = inst.get("dueDate", "")
            if due == tomorrow or due == today:
                upcoming.append({
                    "studentId": f["studentId"], "studentName": student.get("name", ""),
                    "phone": student.get("phoneNumber", ""), "parentPhone": student.get("parentPhoneNumber", ""),
                    "amount": inst["amount"], "dueDate": due,
                    "batchId": f.get("batchId", ""), "studentFeeId": f["id"],
                    "installmentIndex": inst["index"], "type": "due_today" if due == today else "upcoming"
                })
            elif due < today:
                overdue.append({
                    "studentId": f["studentId"], "studentName": student.get("name", ""),
                    "phone": student.get("phoneNumber", ""), "parentPhone": student.get("parentPhoneNumber", ""),
                    "amount": inst["amount"], "dueDate": due,
                    "batchId": f.get("batchId", ""), "studentFeeId": f["id"],
                    "installmentIndex": inst["index"], "type": "overdue"
                })

    overdue.sort(key=lambda x: x["dueDate"])
    return {"upcoming": upcoming[:20], "overdue": overdue[:20], "totalUpcoming": len(upcoming), "totalOverdue": len(overdue)}


async def _send_reminder(student, amount, due_date, reminder_type, institute, iid, channels):
    """Send reminder through configured channels. Returns list of log entries."""
    student_name = student.get("name", "Student")
    institute_name = institute.get("name", "Institute") if institute else "Institute"
    phone = student.get("phoneNumber", "")
    parent_phone = student.get("parentPhoneNumber", "")

    if reminder_type == "overdue":
        msg = (f"Hello, the fee installment of Rs.{amount:,.0f} for {student_name} "
               f"was due on {due_date} and is now overdue. "
               f"Please clear the payment as soon as possible. - {institute_name}")
    else:
        msg = (f"Hello, this is a reminder that the fee installment of Rs.{amount:,.0f} "
               f"for {student_name} is due on {due_date}. "
               f"Please complete the payment to avoid late fees. - {institute_name}")

    logs = []
    today_str = now_iso()

    # In-app notification (always)
    if "in_app" in channels:
        title = "Fee Overdue" if reminder_type == "overdue" else "Fee Reminder"
        await db.notifications.insert_one({
            "id": uid(), "title": title, "message": msg,
            "type": "fee_reminder", "createdAt": today_str,
            "instituteId": iid, "read": False
        })
        logs.append({
            "id": uid(), "studentId": student["id"], "studentName": student_name,
            "parentPhone": parent_phone, "phone": phone,
            "message": msg, "channel": "in_app", "status": "sent",
            "reminderType": reminder_type, "amount": amount, "dueDate": due_date,
            "timestamp": today_str, "instituteId": iid
        })

    # SMS via Twilio
    if "sms" in channels and twilio_client and TWILIO_PHONE:
        for target_phone in [phone, parent_phone]:
            if not target_phone:
                continue
            try:
                twilio_client.messages.create(body=msg, from_=TWILIO_PHONE, to=target_phone)
                status = "sent"
            except Exception as e:
                logger.error(f"SMS send failed to {target_phone}: {e}")
                status = "failed"
            logs.append({
                "id": uid(), "studentId": student["id"], "studentName": student_name,
                "parentPhone": parent_phone, "phone": target_phone,
                "message": msg, "channel": "sms", "status": status,
                "reminderType": reminder_type, "amount": amount, "dueDate": due_date,
                "timestamp": today_str, "instituteId": iid
            })
    elif "sms" in channels:
        logs.append({
            "id": uid(), "studentId": student["id"], "studentName": student_name,
            "parentPhone": parent_phone, "phone": phone,
            "message": msg, "channel": "sms", "status": "skipped_no_credentials",
            "reminderType": reminder_type, "amount": amount, "dueDate": due_date,
            "timestamp": today_str, "instituteId": iid
        })

    # WhatsApp via Twilio
    if "whatsapp" in channels and twilio_client and TWILIO_WA:
        for target_phone in [phone, parent_phone]:
            if not target_phone:
                continue
            try:
                twilio_client.messages.create(
                    body=msg, from_=f"whatsapp:{TWILIO_WA}", to=f"whatsapp:{target_phone}"
                )
                status = "sent"
            except Exception as e:
                logger.error(f"WhatsApp send failed to {target_phone}: {e}")
                status = "failed"
            logs.append({
                "id": uid(), "studentId": student["id"], "studentName": student_name,
                "parentPhone": parent_phone, "phone": target_phone,
                "message": msg, "channel": "whatsapp", "status": status,
                "reminderType": reminder_type, "amount": amount, "dueDate": due_date,
                "timestamp": today_str, "instituteId": iid
            })
    elif "whatsapp" in channels:
        logs.append({
            "id": uid(), "studentId": student["id"], "studentName": student_name,
            "parentPhone": parent_phone, "phone": phone,
            "message": msg, "channel": "whatsapp", "status": "skipped_no_credentials",
            "reminderType": reminder_type, "amount": amount, "dueDate": due_date,
            "timestamp": today_str, "instituteId": iid
        })

    # Save logs
    if logs:
        await db.reminder_logs.insert_many(logs)
    return logs


async def _check_already_sent(student_id, due_date, reminder_type, iid):
    """Check if reminder already sent today for this student/installment/type"""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    existing = await db.reminder_logs.find_one({
        "studentId": student_id, "dueDate": due_date,
        "reminderType": reminder_type, "instituteId": iid,
        "timestamp": {"$regex": f"^{today}"}
    })
    return existing is not None


async def run_reminder_check(iid):
    """Run reminder check for one institute"""
    settings = await _get_reminder_settings(iid)
    if not settings.get("enabled", True):
        return {"skipped": True, "reason": "reminders disabled"}

    channels = settings.get("channels", ["in_app"])
    timing = settings.get("timing", DEFAULT_REMINDER_SETTINGS["timing"])
    days_before = timing.get("daysBefore", 1)
    on_due = timing.get("onDueDate", True)
    days_after = timing.get("daysAfterOverdue", 3)

    today = datetime.now(timezone.utc)
    today_str = today.strftime("%Y-%m-%d")
    before_str = (today + timedelta(days=days_before)).strftime("%Y-%m-%d")
    overdue_threshold = (today - timedelta(days=days_after)).strftime("%Y-%m-%d")

    institute = await db.institutes.find_one({"id": iid}, {"_id": 0})
    fees = await db.student_fees.find({"instituteId": iid}, {"_id": 0}).to_list(2000)

    sent_count = 0
    for f in fees:
        student = await db.students.find_one({"id": f["studentId"], "instituteId": iid}, {"_id": 0})
        if not student:
            continue
        for inst in f.get("installments", []):
            if inst.get("status") == "paid":
                continue
            due = inst.get("dueDate", "")
            reminder_type = None

            if due == before_str and days_before > 0:
                reminder_type = "upcoming"
            elif due == today_str and on_due:
                reminder_type = "due_today"
            elif due <= overdue_threshold and due < today_str:
                reminder_type = "overdue"

            if reminder_type:
                already = await _check_already_sent(f["studentId"], due, reminder_type, iid)
                if not already:
                    await _send_reminder(student, inst["amount"], due, reminder_type, institute, iid, channels)
                    sent_count += 1

    return {"sent": sent_count}


@api.post("/reminders/run-check")
async def manual_reminder_check(user=Depends(admin_only)):
    result = await run_reminder_check(user["instituteId"])
    return result


@api.post("/reminders/send-now")
async def send_reminder_now(data: dict, user=Depends(admin_only)):
    student_id = data.get("studentId")
    amount = data.get("amount", 0)
    due_date = data.get("dueDate", "")
    reminder_type = data.get("type", "manual")

    student = await db.students.find_one({"id": student_id, "instituteId": user["instituteId"]}, {"_id": 0})
    if not student:
        raise HTTPException(404, "Student not found")

    settings = await _get_reminder_settings(user["instituteId"])
    channels = settings.get("channels", ["in_app"])
    institute = await db.institutes.find_one({"id": user["instituteId"]}, {"_id": 0})

    logs = await _send_reminder(student, amount, due_date, reminder_type, institute, user["instituteId"], channels)
    return {"sent": len(logs), "channels": [entry["channel"] for entry in logs]}


async def _reminder_background_loop():
    """Background loop that checks reminders periodically"""
    while True:
        try:
            await asyncio.sleep(3600)  # Check every hour
            institutes = await db.institutes.find({}, {"_id": 0, "id": 1}).to_list(100)
            for inst in institutes:
                try:
                    await run_reminder_check(inst["id"])
                except Exception as e:
                    logger.error(f"Reminder check failed for institute {inst['id']}: {e}")
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Reminder background loop error: {e}")
            await asyncio.sleep(60)


# ─── STUDENT PROFILE ───

@api.get("/students/{sid}/profile")
async def student_profile(sid: str, user=Depends(auth)):
    iid = user["instituteId"]
    student = await db.students.find_one({"id": sid, "instituteId": iid}, {"_id": 0})
    if not student:
        raise HTTPException(404, "Student not found")
    batch = await db.batches.find_one({"id": student.get("batchId", "")}, {"_id": 0})
    # Attendance
    att = await db.attendance.find({"studentId": sid, "instituteId": iid}, {"_id": 0}).to_list(5000)
    present = sum(1 for a in att if a["status"] == "present")
    late = sum(1 for a in att if a["status"] == "late")
    absent = len(att) - present - late
    att_rate = round((present + late) / len(att) * 100, 1) if att else 0
    # Fees
    fees = await db.student_fees.find({"studentId": sid, "instituteId": iid}, {"_id": 0}).to_list(10)
    total_fee = sum(f.get("totalFee", 0) for f in fees)
    total_paid = sum(f.get("totalPaid", 0) for f in fees)
    total_pending = sum(f.get("totalPending", 0) for f in fees)
    # Tests
    # In student_profile, replace the marks loop with:

    marks_list = await db.marks.find({"studentId": sid, "instituteId": iid}, {"_id": 0}).to_list(100)
    test_ids   = [m["testId"] for m in marks_list]
    tests_docs = await db.tests.find({"id": {"$in": test_ids}}, {"_id": 0}).to_list(100) if test_ids else []
    tests_map  = {t["id"]: t for t in tests_docs}

    test_results = []
    for m in marks_list:
        test = tests_map.get(m["testId"])
        if test:
            max_m = test["maximumMarks"]
            test_results.append({
                "testName":      test["testName"],
                "subject":       test.get("subject", ""),
                "marksObtained": m["marksObtained"],
                "maximumMarks":  max_m,
                "percentage":    round(m["marksObtained"] / max_m * 100, 1) if max_m else 0,
                "testDate":      test.get("testDate", ""),
            })
    return {
        "student": student,
        "batch": batch,
        "attendance": {"present": present, "late": late, "absent": absent, "total": len(att), "rate": att_rate},
        "fees": {"totalFee": total_fee, "totalPaid": total_paid, "totalPending": total_pending, "records": fees},
        "tests": test_results
    }


# ─── ANNOUNCEMENTS ───

@api.get("/announcements")
async def list_announcements(user=Depends(auth), limit: int = 20):
    iid = user["instituteId"]
    q = {"instituteId": iid}
    # Students only see announcements targeted at all or their batch
    if user["role"] == "student":
        student = await db.students.find_one({"id": user.get("studentId", "")}, {"_id": 0})
        bid = student.get("batchId", "") if student else ""
        q["$or"] = [{"targetBatchId": ""}, {"targetBatchId": bid}]
    return await db.announcements.find(q, {"_id": 0}).sort("createdAt", -1).to_list(limit)


@api.post("/announcements")
async def create_announcement(data: dict, user=Depends(admin_only)):
    title = data.get("title", "").strip()
    message = data.get("message", "").strip()
    target_batch = data.get("targetBatchId", "")
    if not title or not message:
        raise HTTPException(400, "Title and message required")
    batch_name = ""
    if target_batch:
        batch = await db.batches.find_one({"id": target_batch}, {"_id": 0})
        batch_name = batch["batchName"] if batch else ""
    ann = {
        "id": uid(), "title": title, "message": message,
        "targetBatchId": target_batch, "targetBatchName": batch_name,
        "createdBy": user.get("name", "Admin"),
        "createdAt": now_iso(), "instituteId": user["instituteId"]
    }
    await db.announcements.insert_one(ann)
    ann.pop("_id", None)
    return ann


@api.delete("/announcements/{aid}")
async def delete_announcement(aid: str, user=Depends(admin_only)):
    await db.announcements.delete_one({"id": aid, "instituteId": user["instituteId"]})
    return {"ok": True}


# ─── COMMUNICATION CENTER ───

@api.post("/messages/send")
async def send_message(data: dict, user=Depends(admin_only)):
    iid = user["instituteId"]
    target_type = data.get("targetType")  # "student" or "batch"
    target_id = data.get("targetId", "")
    message = data.get("message", "").strip()
    channel = data.get("channel", "in_app")

    if not message:
        raise HTTPException(400, "Message is required")
    if target_type not in ("student", "batch"):
        raise HTTPException(400, "targetType must be 'student' or 'batch'")

    institute = await db.institutes.find_one({"id": iid}, {"_id": 0})
    inst_name = institute.get("name", "Institute") if institute else "Institute"
    recipients = []

    if target_type == "student":
        student = await db.students.find_one({"id": target_id, "instituteId": iid}, {"_id": 0})
        if not student:
            raise HTTPException(404, "Student not found")
        recipients.append(student)
    else:
        students = await db.students.find({"batchId": target_id, "instituteId": iid}, {"_id": 0}).to_list(200)
        recipients = students

    sent = 0
    skipped = 0
    for student in recipients:
        phone = student.get("phoneNumber", "")
        parent_phone = student.get("parentPhoneNumber", "")
        full_msg = f"{message} - {inst_name}"
        status = "sent"

        # In-app
        if channel == "in_app":
            await db.notifications.insert_one({
                "id": uid(), "title": "Message from Institute",
                "message": full_msg, "type": "custom_message",
                "createdAt": now_iso(), "instituteId": iid, "read": False
            })
        elif channel == "sms":
            if twilio_client and TWILIO_PHONE:
                for target in [phone, parent_phone]:
                    if not target:
                        continue
                    try:
                        twilio_client.messages.create(body=full_msg, from_=TWILIO_PHONE, to=target)
                    except Exception as e:
                        logger.error(f"SMS failed to {target}: {e}")
                        status = "failed"
            else:
                status = "skipped_no_credentials"
        elif channel == "whatsapp":
            if twilio_client and TWILIO_WA:
                for target in [phone, parent_phone]:
                    if not target:
                        continue
                    try:
                        twilio_client.messages.create(
                            body=full_msg, from_=f"whatsapp:{TWILIO_WA}", to=f"whatsapp:{target}")
                    except Exception as e:
                        logger.error(f"WA failed to {target}: {e}")
                        status = "failed"
            else:
                status = "skipped_no_credentials"

        # Log
        await db.message_logs.insert_one({
            "id": uid(), "studentId": student["id"], "studentName": student.get("name", ""),
            "phone": phone, "parentPhone": parent_phone,
            "message": full_msg, "channel": channel, "status": status,
            "targetType": target_type, "targetId": target_id,
            "sentBy": user.get("name", "Admin"),
            "timestamp": now_iso(), "instituteId": iid
        })
        if status == "sent":
            sent += 1
        else:
            skipped += 1

    return {"sent": sent, "skipped": skipped, "total": len(recipients)}


@api.get("/messages/history")
async def message_history(user=Depends(admin_only), limit: int = 50, skip: int = 0):
    logs = await db.message_logs.find(
        {"instituteId": user["instituteId"]}, {"_id": 0}
    ).sort("timestamp", -1).skip(skip).to_list(limit)
    total = await db.message_logs.count_documents({"instituteId": user["instituteId"]})
    return {"logs": logs, "total": total}


# ─── FEATURE FLAGS ───

DEFAULT_FEATURE_FLAGS = {
    "attendance_enabled": True,
    "fee_enabled": True,
    "reminders_enabled": True,
    "communication_enabled": True,
}


@api.get("/settings/features")
async def get_feature_flags(user=Depends(auth)):
    flags = await db.feature_flags.find_one({"instituteId": user["instituteId"]}, {"_id": 0})
    if not flags:
        return {**DEFAULT_FEATURE_FLAGS, "instituteId": user["instituteId"]}
    return flags


@api.put("/settings/features")
async def update_feature_flags(data: dict, user=Depends(admin_only)):
    allowed = {"attendance_enabled", "fee_enabled", "reminders_enabled", "communication_enabled"}
    update = {k: bool(v) for k, v in data.items() if k in allowed}
    update["instituteId"] = user["instituteId"]
    update["updatedAt"] = now_iso()
    await db.feature_flags.update_one(
        {"instituteId": user["instituteId"]}, {"$set": update}, upsert=True
    )
    return {"ok": True}


# ─── LIVE CLASSES ───

@api.get("/institute/plan")
async def get_plan(user=Depends(auth)):
    plan = await get_institute_plan(user["instituteId"])
    return {"plan": plan}


@api.put("/institute/plan")
async def set_plan(data: dict, user=Depends(admin_only)):
    plan = data.get("plan", "standard")
    if plan not in ("base", "starter", "standard"):
        raise HTTPException(400, "Invalid plan")
    await db.institute_plans.update_one(
        {"instituteId": user["instituteId"]},
        {"$set": {"plan": plan, "instituteId": user["instituteId"], "updatedAt": now_iso()}},
        upsert=True
    )
    return {"ok": True, "plan": plan}


@api.post("/live-classes/create")
async def create_live_class(data: LiveClassCreate, user=Depends(teacher_or_admin)):
    iid = user["instituteId"]
    plan = await get_institute_plan(iid)

    if plan == "base":
        raise HTTPException(403, "Live classes not available on Base plan. Please upgrade.")

    # Enforce recording rules by plan
    recording_enabled = data.recordingEnabled and plan == "standard"
    recording_status = "not_available" if plan != "standard" else ("pending" if recording_enabled else "not_available")

    # Get teacher info
    teacher_id = user.get("teacherId", "")
    if user["role"] == "admin":
        batch = await db.batches.find_one({"id": data.batchId, "instituteId": iid}, {"_id": 0})
        teacher_id = batch.get("teacherId", "") if batch else ""

    # Generate Meet link via Google service
    event = google_service.create_calendar_event(data.title, data.startTime, data.endTime)

    lc = {
        "id": uid(), "title": data.title, "batchId": data.batchId,
        "teacherId": teacher_id, "startTime": data.startTime, "endTime": data.endTime,
        "meetLink": event["meetLink"], "calendarEventId": event.get("eventId", ""),
        "createdBy": user.get("name", ""),
        "planType": plan,
        "recordingEnabled": recording_enabled,
        "recordingStatus": recording_status,
        "recordingUrl": "", "driveFileId": "",
        "recordingDuration": 0, "recordingSize": 0,
        "instituteId": iid, "createdAt": now_iso()
    }
    await db.live_classes.insert_one(lc)
    lc.pop("_id", None)
    return lc


@api.get("/live-classes")
async def list_live_classes(user=Depends(auth)):
    iid = user["instituteId"]
    q = {"instituteId": iid}

    # Teacher: only their classes
    if user["role"] == "teacher":
        q["teacherId"] = user.get("teacherId", "")

    # Student: only their batch's classes
    if user["role"] == "student":
        student = await db.students.find_one({"id": user.get("studentId", "")}, {"_id": 0})
        if student:
            q["batchId"] = student.get("batchId", "")
        else:
            return []

    classes = await db.live_classes.find(q, {"_id": 0}).sort("startTime", -1).to_list(100)

    # Enrich with batch and teacher names
    # Replace the enrichment loop at the bottom of list_live_classes:

    if not classes:
        return []

    bid_list = [c.get("batchId",  "") for c in classes]
    tid_list = [c.get("teacherId","") for c in classes]

    batches_map, teachers_map = await asyncio.gather(
        _get_batches_map(iid, bid_list),
        _get_teachers_map(iid, tid_list),
    )

    for c in classes:
        c["batchName"]   = batches_map.get(c.get("batchId",  ""), {}).get("batchName", "")
        c["teacherName"] = teachers_map.get(c.get("teacherId",""), {}).get("name",      "")

    return classes


@api.get("/live-classes/{class_id}")
async def get_live_class(class_id: str, user=Depends(auth)):
    lc = await db.live_classes.find_one({"id": class_id, "instituteId": user["instituteId"]}, {"_id": 0})
    if not lc:
        raise HTTPException(404, "Class not found")
    batch = await db.batches.find_one({"id": lc.get("batchId", "")}, {"_id": 0})
    lc["batchName"] = batch["batchName"] if batch else ""
    teacher = await db.teachers.find_one({"id": lc.get("teacherId", "")}, {"_id": 0})
    lc["teacherName"] = teacher["name"] if teacher else ""
    return lc


@api.delete("/live-classes/{class_id}")
async def delete_live_class(class_id: str, user=Depends(teacher_or_admin)):
    lc = await db.live_classes.find_one({"id": class_id, "instituteId": user["instituteId"]})
    if not lc:
        raise HTTPException(404, "Class not found")
    # Teachers can only delete their own classes
    if user["role"] == "teacher" and lc.get("teacherId") != user.get("teacherId", ""):
        raise HTTPException(403, "Can only delete your own classes")
    await db.live_classes.delete_one({"id": class_id, "instituteId": user["instituteId"]})
    return {"ok": True}


@api.get("/dashboard/upcoming-classes")
async def upcoming_classes_widget(user=Depends(auth)):
    iid = user["instituteId"]
    now = datetime.now(timezone.utc).isoformat()
    q = {"instituteId": iid, "startTime": {"$gte": now}}

    if user["role"] == "teacher":
        q["teacherId"] = user.get("teacherId", "")
    elif user["role"] == "student":
        student = await db.students.find_one({"id": user.get("studentId", "")}, {"_id": 0})
        if student:
            q["batchId"] = student.get("batchId", "")

    classes = await db.live_classes.find(q, {"_id": 0}).sort("startTime", 1).to_list(5)
    for c in classes:
        batch = await db.batches.find_one({"id": c.get("batchId", "")}, {"_id": 0})
        c["batchName"] = batch["batchName"] if batch else ""
        teacher = await db.teachers.find_one({"id": c.get("teacherId", "")}, {"_id": 0})
        c["teacherName"] = teacher["name"] if teacher else ""
    return classes


# ─── RECORDING PIPELINE (Background Worker) ───

async def _process_recordings():
    """Background worker: check for recordings that need processing.
    Runs every 10 minutes. Idempotent - skips already-processed recordings."""
    while True:
        try:
            await asyncio.sleep(600)  # 10 minutes
            # Find classes with pending recordings (standard plan only)
            pending = await db.live_classes.find({
                "recordingEnabled": True,
                "recordingStatus": "pending",
                "planType": "standard"
            }, {"_id": 0}).to_list(50)

            for lc in pending:
                class_id = lc["id"]

                # Double-check not already processed (idempotent)
                current = await db.live_classes.find_one({"id": class_id})
                if not current or current.get("recordingStatus") != "pending":
                    continue

                # Check if class has ended
                try:
                    end_time = datetime.fromisoformat(lc["endTime"].replace("Z", "+00:00"))
                    if datetime.now(timezone.utc) < end_time + timedelta(minutes=5):
                        continue  # Class hasn't ended yet, wait
                except (ValueError, KeyError):
                    continue

                # Step 1: Mark as processing
                await db.live_classes.update_one(
                    {"id": class_id, "recordingStatus": "pending"},
                    {"$set": {"recordingStatus": "processing"}}
                )

                try:
                    # Step 2: Check Google Drive for recording
                    drive_file_id = google_service.check_drive_for_recording(
                        lc["title"], lc["startTime"]
                    )

                    # Mock: simulate recording being found after class ends
                    if not drive_file_id:
                        # In mock mode, simulate a recording appearing
                        drive_file_id = f"drive_{class_id[:8]}"

                    # Step 3: Download from Drive
                    file_bytes = google_service.download_from_drive(drive_file_id)

                    # Step 4: Upload to R2
                    r2_url = google_service.upload_to_r2(class_id, file_bytes)

                    # Step 5: Calculate mock metadata
                    try:
                        start = datetime.fromisoformat(lc["startTime"].replace("Z", "+00:00"))
                        end = datetime.fromisoformat(lc["endTime"].replace("Z", "+00:00"))
                        duration_min = int((end - start).total_seconds() / 60)
                    except (ValueError, KeyError):
                        duration_min = 60
                    file_size_mb = round(duration_min * 15.5, 1)  # ~15.5 MB/min

                    # Step 6: Delete from Google Drive
                    google_service.delete_from_drive(drive_file_id)

                    # Step 7: Update DB
                    await db.live_classes.update_one(
                        {"id": class_id},
                        {"$set": {
                            "recordingStatus": "ready",
                            "recordingUrl": r2_url,
                            "driveFileId": drive_file_id,
                            "recordingDuration": duration_min,
                            "recordingSize": file_size_mb,
                        }}
                    )
                    logger.info(f"Recording processed for class {class_id}")

                except Exception as e:
                    logger.error(f"Recording pipeline failed for {class_id}: {e}")
                    await db.live_classes.update_one(
                        {"id": class_id},
                        {"$set": {"recordingStatus": "failed"}}
                    )

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Recording pipeline loop error: {e}")
            await asyncio.sleep(30)


@api.post("/live-classes/{class_id}/retry-recording")
async def retry_recording(class_id: str, user=Depends(admin_only)):
    lc = await db.live_classes.find_one({"id": class_id, "instituteId": user["instituteId"]})
    if not lc:
        raise HTTPException(404, "Class not found")
    if lc.get("recordingStatus") != "failed":
        raise HTTPException(400, "Recording is not in failed state")
    await db.live_classes.update_one({"id": class_id}, {"$set": {"recordingStatus": "pending"}})
    return {"ok": True, "recordingStatus": "pending"}


# ─── SEED ───

async def do_seed():
    existing = await db.institutes.find_one({"name": "Aman Gupta Coaching Institute"})
    if existing:
        return {"message": "Data already seeded", "instituteId": existing["id"]}

    iid = "inst_demo_001"

    await db.institutes.insert_one({
        "id": iid, "name": "Aman Gupta Coaching Institute",
        "address": "123 Education Lane, New Delhi", "phone": "+91-9876543210",
        "email": "info@amangupta.edu", "createdAt": now_iso()
    })

    await db.users.insert_one({
        "id": "user_admin_001", "name": "Aman Gupta", "email": "admin@growcad.in",
        "passwordHash": hash_pw("admin123"), "role": "admin", "instituteId": iid, "createdAt": now_iso()
    })

    teachers_data = [
        {"name": "Rajesh Kumar", "email": "teacher@growcad.in", "phone": "+91-9876543211", "subject": "Physics", "salary": 45000},
        {"name": "Priya Sharma", "email": "priya@growcad.in", "phone": "+91-9876543212", "subject": "Chemistry", "salary": 42000},
        {"name": "Amit Patel", "email": "amit@growcad.in", "phone": "+91-9876543213", "subject": "Mathematics", "salary": 48000},
        {"name": "Sunita Verma", "email": "sunita@growcad.in", "phone": "+91-9876543214", "subject": "Biology", "salary": 40000},
        {"name": "Deepak Singh", "email": "deepak@growcad.in", "phone": "+91-9876543215", "subject": "English", "salary": 35000},
    ]
    teacher_ids = []
    for i, td in enumerate(teachers_data):
        tid = f"teacher_{i + 1:03d}"
        teacher_ids.append(tid)
        await db.teachers.insert_one({
            "id": tid, "name": td["name"], "phoneNumber": td["phone"], "email": td["email"],
            "subjectExpertise": td["subject"], "assignedBatches": [], "joiningDate": "2025-01-15",
            "salary": td["salary"], "instituteId": iid, "createdAt": now_iso()
        })
        await db.users.insert_one({
            "id": f"user_teacher_{i + 1:03d}", "name": td["name"], "email": td["email"],
            "passwordHash": hash_pw("teacher123"), "role": "teacher",
            "instituteId": iid, "teacherId": tid, "createdAt": now_iso()
        })

    batches_data = [
        {"name": "JEE Advanced 2026", "course": "JEE Preparation", "subject": "Physics",
         "teacher": teacher_ids[0], "duration": "2 hours", "days": ["Monday", "Wednesday", "Friday"], "max": 30},
        {"name": "NEET 2026", "course": "NEET Preparation", "subject": "Biology",
         "teacher": teacher_ids[3], "duration": "2 hours", "days": ["Tuesday", "Thursday", "Saturday"], "max": 35},
        {"name": "Foundation Course", "course": "Class 10 Foundation", "subject": "Mathematics",
         "teacher": teacher_ids[2], "duration": "1.5 hours", "days": ["Monday", "Tuesday", "Thursday", "Friday"], "max": 25},
    ]
    batch_ids = []
    for i, bd in enumerate(batches_data):
        bid = f"batch_{i + 1:03d}"
        batch_ids.append(bid)
        await db.batches.insert_one({
            "id": bid, "batchName": bd["name"], "courseName": bd["course"], "subject": bd["subject"],
            "teacherId": bd["teacher"], "classDuration": bd["duration"], "scheduleDays": bd["days"],
            "startDate": "2025-04-01", "maxStudents": bd["max"], "instituteId": iid, "createdAt": now_iso()
        })

    await db.teachers.update_one({"id": teacher_ids[0]}, {"$set": {"assignedBatches": [batch_ids[0]]}})
    await db.teachers.update_one({"id": teacher_ids[2]}, {"$set": {"assignedBatches": [batch_ids[2]]}})
    await db.teachers.update_one({"id": teacher_ids[3]}, {"$set": {"assignedBatches": [batch_ids[1]]}})

    students_names = [
        ("Aarav Mehta", "aarav"), ("Ananya Iyer", "ananya"), ("Rohan Kapoor", "rohan"),
        ("Ishika Reddy", "ishika"), ("Arjun Nair", "arjun"), ("Meera Joshi", "meera"),
        ("Karan Malhotra", "karan"), ("Prachi Desai", "prachi"), ("Vivek Saxena", "vivek"),
        ("Neha Agarwal", "neha"), ("Sahil Khanna", "sahil"), ("Pooja Rao", "pooja"),
        ("Dev Chauhan", "dev"), ("Ritika Bose", "ritika"), ("Aditya Tiwari", "aditya"),
        ("Tanvi Kulkarni", "tanvi"), ("Manish Pandey", "manish"), ("Sneha Gupta", "sneha"),
        ("Rahul Verma", "rahul"), ("Kavya Menon", "kavya"),
    ]
    student_ids = []
    batch_assignment = [0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2]

    for i, (name, slug) in enumerate(students_names):
        sid = f"student_{i + 1:03d}"
        student_ids.append(sid)
        bid = batch_ids[batch_assignment[i]]
        email = "student@growcad.in" if i == 0 else f"{slug}@growcad.in"
        await db.students.insert_one({
            "id": sid, "name": name, "phoneNumber": f"+91-98765{43200 + i}",
            "parentPhoneNumber": f"+91-98765{43300 + i}", "email": email,
            "batchId": bid, "admissionDate": "2025-04-01", "instituteId": iid, "createdAt": now_iso()
        })
        if i == 0:
            await db.users.insert_one({
                "id": "user_student_001", "name": name, "email": email,
                "passwordHash": hash_pw("student123"), "role": "student",
                "instituteId": iid, "studentId": sid, "createdAt": now_iso()
            })

    fee_structures = [
        {"batch": batch_ids[0], "total": 150000, "plan": "monthly", "installments": 12, "late": 50},
        {"batch": batch_ids[1], "total": 120000, "plan": "quarterly", "installments": 4, "late": 50},
        {"batch": batch_ids[2], "total": 80000, "plan": "half_yearly", "installments": 2, "late": 30},
    ]
    fs_ids = []
    for i, fsd in enumerate(fee_structures):
        fsid = f"fs_{i + 1:03d}"
        fs_ids.append(fsid)
        await db.fee_structures.insert_one({
            "id": fsid, "batchId": fsd["batch"], "totalCourseFee": fsd["total"],
            "paymentPlan": fsd["plan"], "firstDueDate": "2025-04-15",
            "numberOfInstallments": fsd["installments"], "lateFeePerDay": fsd["late"],
            "instituteId": iid, "createdAt": now_iso()
        })

    for i, sid in enumerate(student_ids):
        bi = batch_assignment[i]
        fs = await db.fee_structures.find_one({"id": fs_ids[bi]}, {"_id": 0})
        sf = await _assign_student_fee(sid, fs, iid)
        if i % 3 != 2:
            num_paid = random.randint(1, min(3, len(sf["installments"])))
            installments = sf["installments"]
            total_paid = 0
            for j in range(num_paid):
                installments[j]["status"] = "paid"
                installments[j]["paidAmount"] = installments[j]["amount"]
                installments[j]["paidDate"] = f"2025-{4 + j:02d}-15"
                total_paid += installments[j]["amount"]
            await db.student_fees.update_one(
                {"id": sf["id"]},
                {"$set": {"installments": installments, "totalPaid": total_paid, "totalPending": sf["totalFee"] - total_paid}}
            )

    for day_offset in range(14):
        date = (datetime.now(timezone.utc) - timedelta(days=day_offset)).strftime("%Y-%m-%d")
        day_name = (datetime.now(timezone.utc) - timedelta(days=day_offset)).strftime("%A")
        for bi, bid in enumerate(batch_ids):
            batch = batches_data[bi]
            if day_name not in batch["days"]:
                continue
            batch_students = [student_ids[j] for j in range(len(student_ids)) if batch_assignment[j] == bi]
            for sid in batch_students:
                status = "present" if random.random() > 0.15 else "absent"
                await db.attendance.insert_one({
                    "id": uid(), "studentId": sid, "batchId": bid,
                    "date": date, "status": status, "instituteId": iid
                })

    tests_data = [
        {"name": "Physics Unit Test 1 - Mechanics", "subject": "Physics", "batch": batch_ids[0], "max": 100, "date": "2025-05-10"},
        {"name": "Biology Unit Test 1 - Cell Biology", "subject": "Biology", "batch": batch_ids[1], "max": 100, "date": "2025-05-12"},
        {"name": "Mathematics Weekly Test", "subject": "Mathematics", "batch": batch_ids[2], "max": 50, "date": "2025-05-15"},
    ]
    for i, td in enumerate(tests_data):
        test_id = f"test_{i + 1:03d}"
        await db.tests.insert_one({
            "id": test_id, "testName": td["name"], "subject": td["subject"],
            "batchId": td["batch"], "maximumMarks": td["max"], "testDate": td["date"],
            "instituteId": iid, "createdAt": now_iso()
        })
        bi = batch_ids.index(td["batch"])
        batch_students = [student_ids[j] for j in range(len(student_ids)) if batch_assignment[j] == bi]
        for sid in batch_students:
            marks_val = round(random.uniform(0.4, 0.95) * td["max"], 1)
            await db.marks.insert_one({
                "id": uid(), "testId": test_id, "studentId": sid,
                "marksObtained": marks_val, "instituteId": iid
            })

    notifs = [
        {"title": "Welcome to Growcad!", "message": "Your institute has been set up successfully.", "type": "system"},
        {"title": "New Teacher Added", "message": "Rajesh Kumar has joined as Physics teacher.", "type": "teacher"},
        {"title": "Fee Reminder", "message": "5 students have pending fee payments for this month.", "type": "fee"},
        {"title": "Attendance Report", "message": "Weekly attendance report is ready for review.", "type": "attendance"},
        {"title": "Test Results Uploaded", "message": "Physics Unit Test 1 results have been uploaded.", "type": "test"},
    ]
    for n in notifs:
        await db.notifications.insert_one({
            "id": uid(), "title": n["title"], "message": n["message"],
            "type": n["type"], "createdAt": now_iso(), "instituteId": iid, "read": False
        })

    # Seed institute plan (standard for full feature demo)
    await db.institute_plans.insert_one({
        "instituteId": iid, "plan": "standard", "updatedAt": now_iso()
    })

    # Seed live classes
    now = datetime.now(timezone.utc)
    live_classes_seed = [
        {
            "title": "Physics - Mechanics Revision", "batch": batch_ids[0],
            "teacher": teacher_ids[0], "delta_hours": 2,  # 2 hours from now
            "duration_hrs": 1.5, "recording": True
        },
        {
            "title": "Biology - Cell Division Deep Dive", "batch": batch_ids[1],
            "teacher": teacher_ids[3], "delta_hours": 26,  # tomorrow
            "duration_hrs": 2, "recording": True
        },
        {
            "title": "Maths - Calculus Practice", "batch": batch_ids[2],
            "teacher": teacher_ids[2], "delta_hours": 50,  # 2 days from now
            "duration_hrs": 1.5, "recording": False
        },
        {
            "title": "Physics - Electrostatics", "batch": batch_ids[0],
            "teacher": teacher_ids[0], "delta_hours": -48,  # 2 days ago
            "duration_hrs": 2, "recording": True, "past": True, "rec_status": "ready"
        },
        {
            "title": "Biology - Genetics Introduction", "batch": batch_ids[1],
            "teacher": teacher_ids[3], "delta_hours": -24,  # yesterday
            "duration_hrs": 1.5, "recording": True, "past": True, "rec_status": "processing"
        },
    ]
    for lcd in live_classes_seed:
        start = now + timedelta(hours=lcd["delta_hours"])
        end = start + timedelta(hours=lcd["duration_hrs"])
        dur_min = int(lcd["duration_hrs"] * 60)
        rec_enabled = lcd.get("recording", False)
        rec_status = lcd.get("rec_status", "pending") if rec_enabled else "not_available"
        if not lcd.get("past") and rec_enabled:
            rec_status = "pending"
        rec_url = ""
        rec_size = 0
        if rec_status == "ready":
            cid = uid()[:8]
            rec_url = f"https://r2.growcad.in/recordings/{cid}.mp4"
            rec_size = round(dur_min * 15.5, 1)

        await db.live_classes.insert_one({
            "id": uid(), "title": lcd["title"], "batchId": lcd["batch"],
            "teacherId": lcd["teacher"],
            "startTime": start.isoformat(), "endTime": end.isoformat(),
            "meetLink": google_service.generate_meet_link(),
            "calendarEventId": f"evt_{uid()[:8]}",
            "createdBy": "Admin", "planType": "standard",
            "recordingEnabled": rec_enabled, "recordingStatus": rec_status,
            "recordingUrl": rec_url, "driveFileId": "",
            "recordingDuration": dur_min if rec_status == "ready" else 0,
            "recordingSize": rec_size,
            "instituteId": iid, "createdAt": now_iso()
        })

    return {
        "message": "Demo data seeded successfully",
        "credentials": {
            "admin": "admin@growcad.in / admin123",
            "teacher": "teacher@growcad.in / teacher123",
            "student": "student@growcad.in / student123"
        }
    }


@api.post("/seed")
async def seed_endpoint():
    if os.environ.get("ENV") == "production":
        raise HTTPException(404)
    return await do_seed()



app.include_router(api)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup():
    logger.info("Starting Growcad API...")
    if os.environ.get("ENV") != "production":
        await do_seed()

    asyncio.create_task(_reminder_background_loop())
    logger.info("Fee reminder background job started")
    asyncio.create_task(_process_recordings())
    logger.info("Recording pipeline background job started")


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
