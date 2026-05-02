from fastapi import FastAPI, APIRouter, HTTPException, Depends, UploadFile, File
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

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

JWT_SECRET = os.environ.get('JWT_SECRET')
JWT_ALGO = "HS256"

logging.basicConfig(level=logging.INFO)
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
api = APIRouter(prefix="/api")
security = HTTPBearer()


def uid():
    return str(uuid.uuid4())


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def hash_pw(pw):
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def check_pw(pw, h):
    return bcrypt.checkpw(pw.encode(), h.encode())


def make_token(user_id, role, institute_id):
    return jwt.encode(
        {"uid": user_id, "role": role, "iid": institute_id,
         "exp": datetime.now(timezone.utc) + timedelta(hours=48)},
        JWT_SECRET, algorithm=JWT_ALGO
    )


async def auth(cred: HTTPAuthorizationCredentials = Depends(security)):
    try:
        p = jwt.decode(cred.credentials, JWT_SECRET, algorithms=[JWT_ALGO])
        u = await db.users.find_one({"id": p["uid"]}, {"_id": 0})
        if not u:
            raise HTTPException(401, "Not found")
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
    u = await db.users.find_one({"email": data.email}, {"_id": 0})
    if not u or not check_pw(data.password, u["passwordHash"]):
        raise HTTPException(401, "Invalid credentials")
    token = make_token(u["id"], u["role"], u["instituteId"])
    return {"token": token, "user": {k: v for k, v in u.items() if k != "passwordHash"}}


@api.get("/auth/me")
async def me(user=Depends(auth)):
    return {k: v for k, v in user.items() if k != "passwordHash"}


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
    students = await db.students.count_documents({"instituteId": iid})
    teachers = await db.teachers.count_documents({"instituteId": iid})

    fees = await db.student_fees.find({"instituteId": iid}, {"_id": 0}).to_list(1000)
    total_paid = sum(f.get("totalPaid", 0) for f in fees)
    total_pending = sum(f.get("totalPending", 0) for f in fees)

    att = await db.attendance.find({"instituteId": iid}, {"_id": 0}).to_list(10000)
    present = sum(1 for a in att if a.get("status") in ("present", "late"))
    att_rate = round(present / len(att) * 100, 1) if att else 0

    monthly = {}
    for f in fees:
        for inst in f.get("installments", []):
            if inst.get("paidDate"):
                m = inst["paidDate"][:7]
                monthly[m] = monthly.get(m, 0) + inst.get("paidAmount", 0)

    today_day = datetime.now(timezone.utc).strftime("%A")
    batches = await db.batches.find({"instituteId": iid}, {"_id": 0}).to_list(100)
    today_classes = [b for b in batches if today_day in b.get("scheduleDays", [])]

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    dues_today = []
    for f in fees:
        for inst in f.get("installments", []):
            if inst.get("dueDate", "")[:10] == today_str and inst.get("status") != "paid":
                student = await db.students.find_one({"id": f["studentId"], "instituteId": iid}, {"_id": 0})
                if student:
                    dues_today.append({"studentName": student.get("name", ""), "amount": inst.get("amount", 0), "studentId": f["studentId"]})

    notifs = await db.notifications.find({"instituteId": iid}, {"_id": 0}).sort("createdAt", -1).to_list(5)

    # Latest announcements
    announcements = await db.announcements.find({"instituteId": iid}, {"_id": 0}).sort("createdAt", -1).to_list(5)

    # Today's attendance %
    today_att = await db.attendance.find({"instituteId": iid, "date": today_str}, {"_id": 0}).to_list(5000)
    today_present = sum(1 for a in today_att if a["status"] in ("present", "late"))
    today_att_rate = round(today_present / len(today_att) * 100, 1) if today_att else 0

    return {
        "role": "admin",
        "totalStudents": students, "totalTeachers": teachers,
        "monthlyRevenue": total_paid, "pendingRevenue": total_pending,
        "attendanceRate": att_rate, "todayAttendanceRate": today_att_rate,
        "monthlyFees": monthly,
        "todayClasses": today_classes[:5], "dueToday": dues_today[:5],
        "notifications": notifs, "totalBatches": len(batches),
        "announcements": announcements
    }


async def _teacher_dashboard(user):
    iid = user["instituteId"]
    batch_ids = await get_teacher_batch_ids(user)

    batches = []
    total_students = 0
    for bid in batch_ids:
        b = await db.batches.find_one({"id": bid, "instituteId": iid}, {"_id": 0})
        if b:
            sc = await db.students.count_documents({"batchId": bid, "instituteId": iid})
            b["studentCount"] = sc
            total_students += sc
            batches.append(b)

    today_day = datetime.now(timezone.utc).strftime("%A")
    today_classes = [b for b in batches if today_day in b.get("scheduleDays", [])]

    # Recent attendance for teacher's batches
    att_q = {"instituteId": iid}
    if batch_ids:
        att_q["batchId"] = {"$in": batch_ids}
    att = await db.attendance.find(att_q, {"_id": 0}).to_list(5000)
    present = sum(1 for a in att if a.get("status") in ("present", "late"))
    att_rate = round(present / len(att) * 100, 1) if att else 0

    # Upcoming tests
    test_q = {"instituteId": iid}
    if batch_ids:
        test_q["batchId"] = {"$in": batch_ids}
    tests = await db.tests.find(test_q, {"_id": 0}).sort("testDate", -1).to_list(5)
    for t in tests:
        batch = await db.batches.find_one({"id": t.get("batchId", "")}, {"_id": 0})
        t["batchName"] = batch["batchName"] if batch else ""

    notifs = await db.notifications.find({"instituteId": iid}, {"_id": 0}).sort("createdAt", -1).to_list(5)

    return {
        "role": "teacher",
        "myBatches": batches,
        "totalStudents": total_students,
        "todayClasses": today_classes,
        "attendanceRate": att_rate,
        "recentTests": tests,
        "notifications": notifs,
        "totalBatches": len(batches)
    }


async def _student_dashboard(user):
    iid = user["instituteId"]
    sid = user.get("studentId", "")

    student = await db.students.find_one({"id": sid, "instituteId": iid}, {"_id": 0})
    batch = None
    if student:
        batch = await db.batches.find_one({"id": student.get("batchId", "")}, {"_id": 0})

    # Attendance summary
    att = await db.attendance.find({"studentId": sid, "instituteId": iid}, {"_id": 0}).to_list(5000)
    present = sum(1 for a in att if a.get("status") in ("present", "late"))
    absent = sum(1 for a in att if a.get("status") == "absent")
    att_rate = round(present / len(att) * 100, 1) if att else 0

    # Fee status
    fees = await db.student_fees.find({"studentId": sid, "instituteId": iid}, {"_id": 0}).to_list(10)
    total_fee = sum(f.get("totalFee", 0) for f in fees)
    total_paid = sum(f.get("totalPaid", 0) for f in fees)
    total_pending = sum(f.get("totalPending", 0) for f in fees)
    next_due = None
    for f in fees:
        for inst in f.get("installments", []):
            if inst.get("status") != "paid":
                next_due = {"amount": inst["amount"], "dueDate": inst["dueDate"]}
                break
        if next_due:
            break

    # Recent test results
    marks = await db.marks.find({"studentId": sid, "instituteId": iid}, {"_id": 0}).to_list(100)
    test_results = []
    for m in marks:
        test = await db.tests.find_one({"id": m["testId"]}, {"_id": 0})
        if test:
            test_results.append({
                "testName": test["testName"], "subject": test.get("subject", ""),
                "marksObtained": m["marksObtained"], "maximumMarks": test["maximumMarks"],
                "percentage": round(m["marksObtained"] / test["maximumMarks"] * 100, 1) if test["maximumMarks"] else 0,
                "testDate": test.get("testDate", "")
            })

    notifs = await db.notifications.find({"instituteId": iid}, {"_id": 0}).sort("createdAt", -1).to_list(5)

    # Announcements for student (all + their batch)
    ann_q = {"instituteId": iid, "$or": [{"targetBatchId": ""}, {"targetBatchId": student.get("batchId", "") if student else ""}]}
    announcements = await db.announcements.find(ann_q, {"_id": 0}).sort("createdAt", -1).to_list(5)

    return {
        "role": "student",
        "student": student,
        "batch": batch,
        "attendanceSummary": {"present": present, "absent": absent, "total": len(att), "rate": att_rate},
        "feeSummary": {"totalFee": total_fee, "totalPaid": total_paid, "totalPending": total_pending, "nextDue": next_due},
        "testResults": test_results,
        "notifications": notifs,
        "fees": fees,
        "announcements": announcements
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

    # Pre-load batches for name->id lookup
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

@api.get("/batches")
async def list_batches(user=Depends(auth)):
    iid = user["instituteId"]
    # Teachers see only their batches
    if user["role"] == "teacher":
        batch_ids = await get_teacher_batch_ids(user)
        if not batch_ids:
            return []
        batches = await db.batches.find({"id": {"$in": batch_ids}, "instituteId": iid}, {"_id": 0}).to_list(100)
    elif user["role"] == "student":
        student = await db.students.find_one({"id": user.get("studentId", ""), "instituteId": iid}, {"_id": 0})
        if student and student.get("batchId"):
            batches = await db.batches.find({"id": student["batchId"], "instituteId": iid}, {"_id": 0}).to_list(1)
        else:
            return []
    else:
        batches = await db.batches.find({"instituteId": iid}, {"_id": 0}).to_list(100)
    for b in batches:
        b["studentCount"] = await db.students.count_documents({"batchId": b["id"], "instituteId": iid})
        teacher = await db.teachers.find_one({"id": b.get("teacherId", "")}, {"_id": 0})
        b["teacherName"] = teacher["name"] if teacher else ""
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
async def mark_attendance(data: AttendanceMark, user=Depends(teacher_or_admin)):
    iid = user["instituteId"]
    for r in data.records:
        existing = await db.attendance.find_one({
            "studentId": r["studentId"], "batchId": data.batchId,
            "date": data.date, "instituteId": iid
        })
        if existing:
            await db.attendance.update_one({"_id": existing["_id"]}, {"$set": {"status": r["status"]}})
        else:
            await db.attendance.insert_one({
                "id": uid(), "studentId": r["studentId"], "batchId": data.batchId,
                "date": data.date, "status": r["status"], "instituteId": iid
            })

    # Absent alert system — fire for absent students, dedup per student per day
    absent_ids = [r["studentId"] for r in data.records if r["status"] == "absent"]
    if absent_ids:
        asyncio.create_task(_send_absent_alerts(absent_ids, data.batchId, data.date, iid))

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
    return await db.attendance.find(q, {"_id": 0}).to_list(10000)


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
    for fs in fss:
        batch = await db.batches.find_one({"id": fs.get("batchId", "")}, {"_id": 0})
        fs["batchName"] = batch["batchName"] if batch else ""
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
    q = {"instituteId": user["instituteId"]}
    if user["role"] == "student":
        q["studentId"] = user.get("studentId", "")
    else:
        if studentId:
            q["studentId"] = studentId
        if batchId:
            q["batchId"] = batchId
    fees = await db.student_fees.find(q, {"_id": 0}).to_list(1000)
    for f in fees:
        student = await db.students.find_one({"id": f["studentId"]}, {"_id": 0})
        f["studentName"] = student["name"] if student else ""
        batch = await db.batches.find_one({"id": f.get("batchId", "")}, {"_id": 0})
        f["batchName"] = batch["batchName"] if batch else ""
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
    q = {"instituteId": user["instituteId"]}
    if user["role"] == "teacher":
        batch_ids = await get_teacher_batch_ids(user)
        if batchId and batchId in batch_ids:
            q["batchId"] = batchId
        elif batch_ids:
            q["batchId"] = {"$in": batch_ids}
        else:
            return []
    elif user["role"] == "student":
        student = await db.students.find_one({"id": user.get("studentId", ""), "instituteId": user["instituteId"]}, {"_id": 0})
        if student and student.get("batchId"):
            q["batchId"] = student["batchId"]
        else:
            return []
    else:
        if batchId:
            q["batchId"] = batchId
    tests = await db.tests.find(q, {"_id": 0}).to_list(100)
    for t in tests:
        batch = await db.batches.find_one({"id": t.get("batchId", "")}, {"_id": 0})
        t["batchName"] = batch["batchName"] if batch else ""
        t["marksCount"] = await db.marks.count_documents({"testId": t["id"]})
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
async def attendance_report(user=Depends(auth), batchId: str = "", startDate: str = "", endDate: str = ""):
    iid = user["instituteId"]
    q = {"instituteId": iid}
    if batchId:
        q["batchId"] = batchId
    if startDate or endDate:
        date_q = {}
        if startDate:
            date_q["$gte"] = startDate
        if endDate:
            date_q["$lte"] = endDate
        if date_q:
            q["date"] = date_q
    records = await db.attendance.find(q, {"_id": 0}).to_list(10000)

    # Student-wise attendance
    student_att = {}
    for r in records:
        sid = r["studentId"]
        if sid not in student_att:
            student_att[sid] = {"present": 0, "absent": 0, "late": 0, "total": 0}
        student_att[sid]["total"] += 1
        if r["status"] == "present":
            student_att[sid]["present"] += 1
        elif r["status"] == "late":
            student_att[sid]["late"] += 1
        else:
            student_att[sid]["absent"] += 1
    summaries = []
    for sid, data in student_att.items():
        student = await db.students.find_one({"id": sid}, {"_id": 0})
        rate = round((data["present"] + data["late"]) / data["total"] * 100, 1) if data["total"] > 0 else 0
        summaries.append({
            "studentId": sid, "studentName": student["name"] if student else "",
            "present": data["present"], "late": data["late"], "absent": data["absent"], "total": data["total"], "rate": rate
        })

    # Batch-wise attendance
    batch_att = {}
    for r in records:
        bid = r["batchId"]
        if bid not in batch_att:
            batch_att[bid] = {"present": 0, "total": 0}
        batch_att[bid]["total"] += 1
        if r["status"] in ("present", "late"):
            batch_att[bid]["present"] += 1
    batch_summaries = []
    for bid, data in batch_att.items():
        batch = await db.batches.find_one({"id": bid}, {"_id": 0})
        rate = round(data["present"] / data["total"] * 100, 1) if data["total"] > 0 else 0
        batch_summaries.append({
            "batchId": bid, "batchName": batch["batchName"] if batch else "",
            "present": data["present"], "total": data["total"], "rate": rate
        })

    # Monthly trend data
    monthly_trend = {}
    for r in records:
        month = r.get("date", "")[:7]  # YYYY-MM
        if not month:
            continue
        if month not in monthly_trend:
            monthly_trend[month] = {"present": 0, "total": 0}
        monthly_trend[month]["total"] += 1
        if r["status"] in ("present", "late"):
            monthly_trend[month]["present"] += 1
    trend = []
    for m in sorted(monthly_trend.keys()):
        d = monthly_trend[m]
        rate = round(d["present"] / d["total"] * 100, 1) if d["total"] > 0 else 0
        trend.append({"month": m, "present": d["present"], "total": d["total"], "rate": rate})

    return {"students": summaries, "batches": batch_summaries, "monthlyTrend": trend}


@api.get("/reports/fees")
async def fees_report(user=Depends(auth), batchId: str = ""):
    iid = user["instituteId"]
    q = {"instituteId": iid}
    if batchId:
        q["batchId"] = batchId
    fees = await db.student_fees.find(q, {"_id": 0}).to_list(1000)
    total_collected = sum(f.get("totalPaid", 0) for f in fees)
    total_pending = sum(f.get("totalPending", 0) for f in fees)
    total_fee = sum(f.get("totalFee", 0) for f in fees)
    batch_fees = {}
    for f in fees:
        bid = f.get("batchId", "")
        if bid not in batch_fees:
            batch_fees[bid] = {"collected": 0, "pending": 0, "total": 0}
        batch_fees[bid]["collected"] += f.get("totalPaid", 0)
        batch_fees[bid]["pending"] += f.get("totalPending", 0)
        batch_fees[bid]["total"] += f.get("totalFee", 0)
    batch_summaries = []
    for bid, data in batch_fees.items():
        batch = await db.batches.find_one({"id": bid}, {"_id": 0})
        batch_summaries.append({"batchId": bid, "batchName": batch["batchName"] if batch else "", **data})
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    overdue = []
    for f in fees:
        for inst in f.get("installments", []):
            if inst.get("status") != "paid" and inst.get("dueDate", "") < today:
                student = await db.students.find_one({"id": f["studentId"]}, {"_id": 0})
                overdue.append({
                    "studentName": student["name"] if student else "",
                    "amount": inst["amount"], "dueDate": inst["dueDate"], "studentId": f["studentId"]
                })

    # Monthly collection trend
    monthly_collection = {}
    for f in fees:
        for inst in f.get("installments", []):
            if inst.get("paidDate"):
                m = inst["paidDate"][:7]
                monthly_collection[m] = monthly_collection.get(m, 0) + inst.get("paidAmount", 0)
    collection_trend = [{"month": m, "collected": monthly_collection[m]} for m in sorted(monthly_collection.keys())]

    return {
        "totalCollected": total_collected, "totalPending": total_pending,
        "totalFee": total_fee, "batches": batch_summaries, "overdue": overdue[:20],
        "collectionTrend": collection_trend
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
    marks_list = await db.marks.find({"studentId": sid, "instituteId": iid}, {"_id": 0}).to_list(100)
    test_results = []
    for m in marks_list:
        test = await db.tests.find_one({"id": m["testId"]}, {"_id": 0})
        if test:
            test_results.append({
                "testName": test["testName"], "subject": test.get("subject", ""),
                "marksObtained": m["marksObtained"], "maximumMarks": test["maximumMarks"],
                "percentage": round(m["marksObtained"] / test["maximumMarks"] * 100, 1) if test["maximumMarks"] else 0,
                "testDate": test.get("testDate", "")
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
    for c in classes:
        batch = await db.batches.find_one({"id": c.get("batchId", "")}, {"_id": 0})
        c["batchName"] = batch["batchName"] if batch else ""
        teacher = await db.teachers.find_one({"id": c.get("teacherId", "")}, {"_id": 0})
        c["teacherName"] = teacher["name"] if teacher else ""

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
    await do_seed()
    asyncio.create_task(_reminder_background_loop())
    logger.info("Fee reminder background job started")
    asyncio.create_task(_process_recordings())
    logger.info("Recording pipeline background job started")


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
