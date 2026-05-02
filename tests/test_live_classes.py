"""
Test suite for Live Classes module (Iteration 6)
Tests: Plan system, Live class CRUD, Plan-gated recording, RBAC, Dashboard widget
"""
import pytest
import requests
import os
from datetime import datetime, timedelta

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
ADMIN_CREDS = {"email": "admin@growcad.in", "password": "admin123"}
TEACHER_CREDS = {"email": "teacher@growcad.in", "password": "teacher123"}
STUDENT_CREDS = {"email": "student@growcad.in", "password": "student123"}


@pytest.fixture(scope="module")
def admin_token():
    """Get admin auth token"""
    r = requests.post(f"{BASE_URL}/api/auth/login", json=ADMIN_CREDS)
    if r.status_code != 200:
        pytest.skip("Admin login failed")
    return r.json()["token"]


@pytest.fixture(scope="module")
def teacher_token():
    """Get teacher auth token"""
    r = requests.post(f"{BASE_URL}/api/auth/login", json=TEACHER_CREDS)
    if r.status_code != 200:
        pytest.skip("Teacher login failed")
    return r.json()["token"]


@pytest.fixture(scope="module")
def student_token():
    """Get student auth token"""
    r = requests.post(f"{BASE_URL}/api/auth/login", json=STUDENT_CREDS)
    if r.status_code != 200:
        pytest.skip("Student login failed")
    return r.json()["token"]


@pytest.fixture
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}


@pytest.fixture
def teacher_headers(teacher_token):
    return {"Authorization": f"Bearer {teacher_token}", "Content-Type": "application/json"}


@pytest.fixture
def student_headers(student_token):
    return {"Authorization": f"Bearer {student_token}", "Content-Type": "application/json"}


# ─── PLAN SYSTEM TESTS ───

class TestInstitutePlan:
    """Tests for GET/PUT /api/institute/plan"""
    
    def test_get_plan_returns_current_plan(self, admin_headers):
        """GET /api/institute/plan returns current plan"""
        r = requests.get(f"{BASE_URL}/api/institute/plan", headers=admin_headers)
        assert r.status_code == 200
        data = r.json()
        assert "plan" in data
        assert data["plan"] in ["base", "starter", "standard"]
        print(f"Current plan: {data['plan']}")
    
    def test_update_plan_admin_only(self, admin_headers, teacher_headers, student_headers):
        """PUT /api/institute/plan is admin-only"""
        # Teacher should get 403
        r = requests.put(f"{BASE_URL}/api/institute/plan", 
                        json={"plan": "starter"}, headers=teacher_headers)
        assert r.status_code == 403
        
        # Student should get 403
        r = requests.put(f"{BASE_URL}/api/institute/plan", 
                        json={"plan": "starter"}, headers=student_headers)
        assert r.status_code == 403
        
        # Admin should succeed
        r = requests.put(f"{BASE_URL}/api/institute/plan", 
                        json={"plan": "standard"}, headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["plan"] == "standard"
    
    def test_update_plan_validates_input(self, admin_headers):
        """PUT /api/institute/plan rejects invalid plan names"""
        r = requests.put(f"{BASE_URL}/api/institute/plan", 
                        json={"plan": "invalid_plan"}, headers=admin_headers)
        assert r.status_code == 400


# ─── LIVE CLASS CRUD TESTS ───

class TestLiveClassCRUD:
    """Tests for Live Class CRUD operations"""
    
    @pytest.fixture(autouse=True)
    def ensure_standard_plan(self, admin_headers):
        """Ensure we're on standard plan for most tests"""
        requests.put(f"{BASE_URL}/api/institute/plan", 
                    json={"plan": "standard"}, headers=admin_headers)
    
    def test_create_class_with_meet_link(self, admin_headers):
        """POST /api/live-classes/create generates meet link"""
        # Get a batch ID first
        batches = requests.get(f"{BASE_URL}/api/batches", headers=admin_headers).json()
        if not batches:
            pytest.skip("No batches available")
        batch_id = batches[0]["id"]
        
        start = (datetime.utcnow() + timedelta(hours=1)).isoformat() + "Z"
        end = (datetime.utcnow() + timedelta(hours=2)).isoformat() + "Z"
        
        r = requests.post(f"{BASE_URL}/api/live-classes/create", headers=admin_headers, json={
            "title": "TEST_Physics Class",
            "batchId": batch_id,
            "startTime": start,
            "endTime": end,
            "recordingEnabled": True
        })
        assert r.status_code == 200
        data = r.json()
        
        # Verify response structure
        assert "id" in data
        assert "meetLink" in data
        assert data["meetLink"].startswith("https://meet.google.com/")
        assert data["title"] == "TEST_Physics Class"
        assert data["batchId"] == batch_id
        assert data["recordingEnabled"] == True
        assert data["recordingStatus"] == "pending"  # Standard plan with recording enabled
        print(f"Created class with meet link: {data['meetLink']}")
        
        # Cleanup
        requests.delete(f"{BASE_URL}/api/live-classes/{data['id']}", headers=admin_headers)
    
    def test_get_classes_enriched_with_names(self, admin_headers):
        """GET /api/live-classes returns classes with batchName and teacherName"""
        r = requests.get(f"{BASE_URL}/api/live-classes", headers=admin_headers)
        assert r.status_code == 200
        classes = r.json()
        assert isinstance(classes, list)
        
        if classes:
            c = classes[0]
            assert "batchName" in c
            assert "teacherName" in c
            assert "meetLink" in c
            assert "recordingStatus" in c
            print(f"Found {len(classes)} classes, first: {c.get('title')}")
    
    def test_get_single_class(self, admin_headers):
        """GET /api/live-classes/{id} returns single class with details"""
        # Get existing classes
        classes = requests.get(f"{BASE_URL}/api/live-classes", headers=admin_headers).json()
        if not classes:
            pytest.skip("No classes to test")
        
        class_id = classes[0]["id"]
        r = requests.get(f"{BASE_URL}/api/live-classes/{class_id}", headers=admin_headers)
        assert r.status_code == 200
        data = r.json()
        assert data["id"] == class_id
        assert "batchName" in data
        assert "teacherName" in data
    
    def test_delete_class(self, admin_headers):
        """DELETE /api/live-classes/{id} deletes class"""
        # Create a class to delete
        batches = requests.get(f"{BASE_URL}/api/batches", headers=admin_headers).json()
        if not batches:
            pytest.skip("No batches available")
        
        start = (datetime.utcnow() + timedelta(hours=5)).isoformat() + "Z"
        end = (datetime.utcnow() + timedelta(hours=6)).isoformat() + "Z"
        
        create_r = requests.post(f"{BASE_URL}/api/live-classes/create", headers=admin_headers, json={
            "title": "TEST_Delete Me",
            "batchId": batches[0]["id"],
            "startTime": start,
            "endTime": end,
            "recordingEnabled": False
        })
        class_id = create_r.json()["id"]
        
        # Delete it
        r = requests.delete(f"{BASE_URL}/api/live-classes/{class_id}", headers=admin_headers)
        assert r.status_code == 200
        assert r.json()["ok"] == True
        
        # Verify it's gone
        r = requests.get(f"{BASE_URL}/api/live-classes/{class_id}", headers=admin_headers)
        assert r.status_code == 404


# ─── PLAN ENFORCEMENT TESTS ───

class TestPlanEnforcement:
    """Tests for plan-based feature gating"""
    
    def test_base_plan_blocks_class_creation(self, admin_headers):
        """On base plan, class creation returns 403"""
        # Set to base plan
        requests.put(f"{BASE_URL}/api/institute/plan", 
                    json={"plan": "base"}, headers=admin_headers)
        
        batches = requests.get(f"{BASE_URL}/api/batches", headers=admin_headers).json()
        if not batches:
            pytest.skip("No batches available")
        
        start = (datetime.utcnow() + timedelta(hours=1)).isoformat() + "Z"
        end = (datetime.utcnow() + timedelta(hours=2)).isoformat() + "Z"
        
        r = requests.post(f"{BASE_URL}/api/live-classes/create", headers=admin_headers, json={
            "title": "TEST_Should Fail",
            "batchId": batches[0]["id"],
            "startTime": start,
            "endTime": end,
            "recordingEnabled": False
        })
        assert r.status_code == 403
        assert "Base plan" in r.json().get("detail", "")
        
        # Restore standard plan
        requests.put(f"{BASE_URL}/api/institute/plan", 
                    json={"plan": "standard"}, headers=admin_headers)
    
    def test_starter_plan_disables_recording(self, admin_headers):
        """On starter plan, recording is always disabled"""
        # Set to starter plan
        requests.put(f"{BASE_URL}/api/institute/plan", 
                    json={"plan": "starter"}, headers=admin_headers)
        
        batches = requests.get(f"{BASE_URL}/api/batches", headers=admin_headers).json()
        if not batches:
            pytest.skip("No batches available")
        
        start = (datetime.utcnow() + timedelta(hours=2)).isoformat() + "Z"
        end = (datetime.utcnow() + timedelta(hours=3)).isoformat() + "Z"
        
        r = requests.post(f"{BASE_URL}/api/live-classes/create", headers=admin_headers, json={
            "title": "TEST_Starter Class",
            "batchId": batches[0]["id"],
            "startTime": start,
            "endTime": end,
            "recordingEnabled": True  # Request recording
        })
        assert r.status_code == 200
        data = r.json()
        
        # Recording should be disabled despite request
        assert data["recordingEnabled"] == False
        assert data["recordingStatus"] == "not_available"
        print(f"Starter plan class created with recording disabled")
        
        # Cleanup
        requests.delete(f"{BASE_URL}/api/live-classes/{data['id']}", headers=admin_headers)
        
        # Restore standard plan
        requests.put(f"{BASE_URL}/api/institute/plan", 
                    json={"plan": "standard"}, headers=admin_headers)
    
    def test_standard_plan_enables_recording(self, admin_headers):
        """On standard plan, recording_enabled=true sets recordingStatus='pending'"""
        # Ensure standard plan
        requests.put(f"{BASE_URL}/api/institute/plan", 
                    json={"plan": "standard"}, headers=admin_headers)
        
        batches = requests.get(f"{BASE_URL}/api/batches", headers=admin_headers).json()
        if not batches:
            pytest.skip("No batches available")
        
        start = (datetime.utcnow() + timedelta(hours=3)).isoformat() + "Z"
        end = (datetime.utcnow() + timedelta(hours=4)).isoformat() + "Z"
        
        r = requests.post(f"{BASE_URL}/api/live-classes/create", headers=admin_headers, json={
            "title": "TEST_Standard Class",
            "batchId": batches[0]["id"],
            "startTime": start,
            "endTime": end,
            "recordingEnabled": True
        })
        assert r.status_code == 200
        data = r.json()
        
        assert data["recordingEnabled"] == True
        assert data["recordingStatus"] == "pending"
        print(f"Standard plan class created with recording pending")
        
        # Cleanup
        requests.delete(f"{BASE_URL}/api/live-classes/{data['id']}", headers=admin_headers)


# ─── RBAC TESTS ───

class TestLiveClassRBAC:
    """Tests for role-based access control"""
    
    @pytest.fixture(autouse=True)
    def ensure_standard_plan(self, admin_headers):
        """Ensure standard plan for RBAC tests"""
        requests.put(f"{BASE_URL}/api/institute/plan", 
                    json={"plan": "standard"}, headers=admin_headers)
    
    def test_teacher_can_create_class(self, teacher_headers, admin_headers):
        """Teacher can create live classes"""
        batches = requests.get(f"{BASE_URL}/api/batches", headers=teacher_headers).json()
        if not batches:
            pytest.skip("Teacher has no batches")
        
        start = (datetime.utcnow() + timedelta(hours=4)).isoformat() + "Z"
        end = (datetime.utcnow() + timedelta(hours=5)).isoformat() + "Z"
        
        r = requests.post(f"{BASE_URL}/api/live-classes/create", headers=teacher_headers, json={
            "title": "TEST_Teacher Class",
            "batchId": batches[0]["id"],
            "startTime": start,
            "endTime": end,
            "recordingEnabled": False
        })
        assert r.status_code == 200
        data = r.json()
        assert "meetLink" in data
        
        # Cleanup with admin
        requests.delete(f"{BASE_URL}/api/live-classes/{data['id']}", headers=admin_headers)
    
    def test_student_cannot_create_class(self, student_headers):
        """Student cannot create live classes"""
        start = (datetime.utcnow() + timedelta(hours=1)).isoformat() + "Z"
        end = (datetime.utcnow() + timedelta(hours=2)).isoformat() + "Z"
        
        r = requests.post(f"{BASE_URL}/api/live-classes/create", headers=student_headers, json={
            "title": "TEST_Student Class",
            "batchId": "batch_001",
            "startTime": start,
            "endTime": end,
            "recordingEnabled": False
        })
        assert r.status_code == 403
    
    def test_student_can_view_batch_classes(self, student_headers):
        """Student can view classes for their batch"""
        r = requests.get(f"{BASE_URL}/api/live-classes", headers=student_headers)
        assert r.status_code == 200
        classes = r.json()
        assert isinstance(classes, list)
        print(f"Student sees {len(classes)} classes")
    
    def test_teacher_sees_own_classes_only(self, teacher_headers, admin_headers):
        """Teacher only sees their own classes"""
        # Get teacher's classes
        teacher_classes = requests.get(f"{BASE_URL}/api/live-classes", headers=teacher_headers).json()
        
        # Get all classes (admin)
        all_classes = requests.get(f"{BASE_URL}/api/live-classes", headers=admin_headers).json()
        
        # Teacher should see subset or equal
        assert len(teacher_classes) <= len(all_classes)
        print(f"Teacher sees {len(teacher_classes)} of {len(all_classes)} total classes")


# ─── DASHBOARD WIDGET TESTS ───

class TestDashboardUpcomingClasses:
    """Tests for GET /api/dashboard/upcoming-classes"""
    
    def test_upcoming_classes_returns_future_classes(self, admin_headers):
        """GET /api/dashboard/upcoming-classes returns future classes sorted by start time"""
        r = requests.get(f"{BASE_URL}/api/dashboard/upcoming-classes", headers=admin_headers)
        assert r.status_code == 200
        classes = r.json()
        assert isinstance(classes, list)
        
        if len(classes) > 1:
            # Verify sorted by start time ascending
            for i in range(len(classes) - 1):
                assert classes[i]["startTime"] <= classes[i+1]["startTime"]
        
        # Verify enriched with names
        if classes:
            assert "batchName" in classes[0]
            assert "teacherName" in classes[0]
        
        print(f"Found {len(classes)} upcoming classes")
    
    def test_upcoming_classes_respects_role(self, student_headers, teacher_headers):
        """Upcoming classes widget respects user role"""
        # Student should see their batch's classes
        r = requests.get(f"{BASE_URL}/api/dashboard/upcoming-classes", headers=student_headers)
        assert r.status_code == 200
        
        # Teacher should see their classes
        r = requests.get(f"{BASE_URL}/api/dashboard/upcoming-classes", headers=teacher_headers)
        assert r.status_code == 200


# ─── RETRY RECORDING TESTS ───

class TestRetryRecording:
    """Tests for POST /api/live-classes/{id}/retry-recording"""
    
    def test_retry_recording_admin_only(self, admin_headers, teacher_headers):
        """Retry recording is admin-only"""
        # Get a class
        classes = requests.get(f"{BASE_URL}/api/live-classes", headers=admin_headers).json()
        if not classes:
            pytest.skip("No classes to test")
        
        class_id = classes[0]["id"]
        
        # Teacher should get 403
        r = requests.post(f"{BASE_URL}/api/live-classes/{class_id}/retry-recording", 
                         headers=teacher_headers)
        assert r.status_code == 403
    
    def test_retry_recording_requires_failed_status(self, admin_headers):
        """Retry recording only works on failed recordings"""
        classes = requests.get(f"{BASE_URL}/api/live-classes", headers=admin_headers).json()
        if not classes:
            pytest.skip("No classes to test")
        
        # Find a class that's not in failed state
        for c in classes:
            if c.get("recordingStatus") != "failed":
                r = requests.post(f"{BASE_URL}/api/live-classes/{c['id']}/retry-recording", 
                                 headers=admin_headers)
                assert r.status_code == 400
                break


# ─── EXISTING SYSTEM TESTS ───

class TestExistingFeatures:
    """Verify existing fee/reminder system still works"""
    
    def test_reminder_settings_endpoint(self, admin_headers):
        """GET /api/settings/reminders returns valid data"""
        r = requests.get(f"{BASE_URL}/api/settings/reminders", headers=admin_headers)
        assert r.status_code == 200
        data = r.json()
        assert "enabled" in data
        assert "channels" in data
        assert "timing" in data
        print(f"Reminder settings: enabled={data['enabled']}, channels={data['channels']}")
    
    def test_batches_endpoint(self, admin_headers):
        """GET /api/batches still works"""
        r = requests.get(f"{BASE_URL}/api/batches", headers=admin_headers)
        assert r.status_code == 200
        batches = r.json()
        assert isinstance(batches, list)
        print(f"Found {len(batches)} batches")
    
    def test_students_endpoint(self, admin_headers):
        """GET /api/students still works"""
        r = requests.get(f"{BASE_URL}/api/students", headers=admin_headers)
        assert r.status_code == 200
        students = r.json()
        assert isinstance(students, list)
        print(f"Found {len(students)} students")


# ─── CLEANUP ───

@pytest.fixture(scope="module", autouse=True)
def cleanup_test_data(admin_token):
    """Cleanup TEST_ prefixed data after all tests"""
    yield
    headers = {"Authorization": f"Bearer {admin_token}", "Content-Type": "application/json"}
    
    # Restore standard plan
    requests.put(f"{BASE_URL}/api/institute/plan", json={"plan": "standard"}, headers=headers)
    
    # Delete test classes
    classes = requests.get(f"{BASE_URL}/api/live-classes", headers=headers).json()
    for c in classes:
        if c.get("title", "").startswith("TEST_"):
            requests.delete(f"{BASE_URL}/api/live-classes/{c['id']}", headers=headers)
    
    print("Cleanup complete - restored standard plan")
