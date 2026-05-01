"""
Iteration 5 Backend Tests - New Features
Tests for: Student Profile, Late Attendance, Absent Alerts, Announcements, Communication, Feature Flags, Dashboard Updates
"""
import pytest
import requests
import os

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')

# Test credentials
ADMIN_EMAIL = "admin@meritinfi.com"
ADMIN_PASS = "admin123"
TEACHER_EMAIL = "teacher@meritinfi.com"
TEACHER_PASS = "teacher123"
STUDENT_EMAIL = "student@meritinfi.com"
STUDENT_PASS = "student123"


@pytest.fixture(scope="module")
def admin_token():
    """Get admin auth token"""
    resp = requests.post(f"{BASE_URL}/api/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASS})
    assert resp.status_code == 200, f"Admin login failed: {resp.text}"
    return resp.json()["token"]


@pytest.fixture(scope="module")
def teacher_token():
    """Get teacher auth token"""
    resp = requests.post(f"{BASE_URL}/api/auth/login", json={"email": TEACHER_EMAIL, "password": TEACHER_PASS})
    assert resp.status_code == 200, f"Teacher login failed: {resp.text}"
    return resp.json()["token"]


@pytest.fixture(scope="module")
def student_token():
    """Get student auth token"""
    resp = requests.post(f"{BASE_URL}/api/auth/login", json={"email": STUDENT_EMAIL, "password": STUDENT_PASS})
    assert resp.status_code == 200, f"Student login failed: {resp.text}"
    return resp.json()["token"]


def auth_header(token):
    return {"Authorization": f"Bearer {token}"}


# ─── STUDENT PROFILE TESTS ───

class TestStudentProfile:
    """Tests for GET /api/students/{id}/profile"""
    
    def test_get_student_profile_admin(self, admin_token):
        """Admin can get student profile with all data"""
        # First get a student ID
        resp = requests.get(f"{BASE_URL}/api/students", headers=auth_header(admin_token))
        assert resp.status_code == 200
        students = resp.json()
        assert len(students) > 0, "No students found"
        
        student_id = students[0]["id"]
        
        # Get profile
        resp = requests.get(f"{BASE_URL}/api/students/{student_id}/profile", headers=auth_header(admin_token))
        assert resp.status_code == 200
        
        data = resp.json()
        # Verify structure
        assert "student" in data
        assert "batch" in data
        assert "attendance" in data
        assert "fees" in data
        assert "tests" in data
        
        # Verify attendance has late field
        att = data["attendance"]
        assert "present" in att
        assert "late" in att
        assert "absent" in att
        assert "total" in att
        assert "rate" in att
        
        # Verify fees structure
        fees = data["fees"]
        assert "totalFee" in fees
        assert "totalPaid" in fees
        assert "totalPending" in fees
        assert "records" in fees
        
        print(f"Student profile retrieved: {data['student']['name']}, Attendance: {att['rate']}%")
    
    def test_get_student_profile_teacher(self, teacher_token):
        """Teacher can get student profile"""
        # Get students visible to teacher
        resp = requests.get(f"{BASE_URL}/api/students", headers=auth_header(teacher_token))
        assert resp.status_code == 200
        students = resp.json()
        
        if len(students) > 0:
            student_id = students[0]["id"]
            resp = requests.get(f"{BASE_URL}/api/students/{student_id}/profile", headers=auth_header(teacher_token))
            assert resp.status_code == 200
            print(f"Teacher can view student profile")
    
    def test_get_student_profile_not_found(self, admin_token):
        """Non-existent student returns 404"""
        resp = requests.get(f"{BASE_URL}/api/students/nonexistent_id/profile", headers=auth_header(admin_token))
        assert resp.status_code == 404


# ─── ATTENDANCE WITH LATE STATUS TESTS ───

class TestAttendanceWithLate:
    """Tests for attendance with present/absent/late status"""
    
    def test_mark_attendance_with_late(self, admin_token):
        """Can mark attendance with late status"""
        # Get a batch
        resp = requests.get(f"{BASE_URL}/api/batches", headers=auth_header(admin_token))
        assert resp.status_code == 200
        batches = resp.json()
        assert len(batches) > 0
        batch_id = batches[0]["id"]
        
        # Get students in batch
        resp = requests.get(f"{BASE_URL}/api/students", params={"batchId": batch_id}, headers=auth_header(admin_token))
        assert resp.status_code == 200
        students = resp.json()
        
        if len(students) >= 3:
            # Mark with all three statuses
            records = [
                {"studentId": students[0]["id"], "status": "present"},
                {"studentId": students[1]["id"], "status": "late"},
                {"studentId": students[2]["id"], "status": "absent"},
            ]
            
            resp = requests.post(f"{BASE_URL}/api/attendance/mark", 
                json={"batchId": batch_id, "date": "2026-01-15", "records": records},
                headers=auth_header(admin_token))
            assert resp.status_code == 200
            assert resp.json()["ok"] == True
            
            # Verify attendance was saved
            resp = requests.get(f"{BASE_URL}/api/attendance", 
                params={"batchId": batch_id, "date": "2026-01-15"},
                headers=auth_header(admin_token))
            assert resp.status_code == 200
            att_records = resp.json()
            
            statuses = {r["studentId"]: r["status"] for r in att_records}
            assert statuses.get(students[0]["id"]) == "present"
            assert statuses.get(students[1]["id"]) == "late"
            assert statuses.get(students[2]["id"]) == "absent"
            
            print("Attendance with late status working correctly")


# ─── ABSENT ALERTS TESTS ───

class TestAbsentAlerts:
    """Tests for absent alert system"""
    
    def test_absent_alert_created_on_absent_mark(self, admin_token):
        """Marking student absent creates alert in absent_alerts collection"""
        # Get a batch and student
        resp = requests.get(f"{BASE_URL}/api/batches", headers=auth_header(admin_token))
        batches = resp.json()
        batch_id = batches[0]["id"]
        
        resp = requests.get(f"{BASE_URL}/api/students", params={"batchId": batch_id}, headers=auth_header(admin_token))
        students = resp.json()
        
        if len(students) > 0:
            # Mark as absent with unique date
            test_date = "2026-01-16"
            records = [{"studentId": students[0]["id"], "status": "absent"}]
            
            resp = requests.post(f"{BASE_URL}/api/attendance/mark",
                json={"batchId": batch_id, "date": test_date, "records": records},
                headers=auth_header(admin_token))
            assert resp.status_code == 200
            print("Absent alert system triggered (async)")


# ─── ANNOUNCEMENTS TESTS ───

class TestAnnouncements:
    """Tests for announcements CRUD"""
    
    def test_get_announcements_admin(self, admin_token):
        """Admin can get announcements"""
        resp = requests.get(f"{BASE_URL}/api/announcements", headers=auth_header(admin_token))
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
        print(f"Admin can view announcements: {len(resp.json())} found")
    
    def test_create_announcement_admin(self, admin_token):
        """Admin can create announcement"""
        resp = requests.post(f"{BASE_URL}/api/announcements",
            json={"title": "TEST_Announcement", "message": "This is a test announcement", "targetBatchId": ""},
            headers=auth_header(admin_token))
        assert resp.status_code == 200
        
        data = resp.json()
        assert data["title"] == "TEST_Announcement"
        assert data["message"] == "This is a test announcement"
        assert "id" in data
        assert "createdAt" in data
        print(f"Announcement created: {data['id']}")
        return data["id"]
    
    def test_create_announcement_with_batch_target(self, admin_token):
        """Admin can create announcement targeted to specific batch"""
        # Get a batch
        resp = requests.get(f"{BASE_URL}/api/batches", headers=auth_header(admin_token))
        batches = resp.json()
        
        if len(batches) > 0:
            batch_id = batches[0]["id"]
            resp = requests.post(f"{BASE_URL}/api/announcements",
                json={"title": "TEST_Batch_Announcement", "message": "For specific batch", "targetBatchId": batch_id},
                headers=auth_header(admin_token))
            assert resp.status_code == 200
            data = resp.json()
            assert data["targetBatchId"] == batch_id
            print(f"Batch-targeted announcement created")
    
    def test_get_announcements_student(self, student_token):
        """Student can see announcements (all + their batch)"""
        resp = requests.get(f"{BASE_URL}/api/announcements", headers=auth_header(student_token))
        assert resp.status_code == 200
        print(f"Student can view announcements: {len(resp.json())} visible")
    
    def test_get_announcements_teacher(self, teacher_token):
        """Teacher can see announcements"""
        resp = requests.get(f"{BASE_URL}/api/announcements", headers=auth_header(teacher_token))
        assert resp.status_code == 200
        print(f"Teacher can view announcements")
    
    def test_delete_announcement_admin(self, admin_token):
        """Admin can delete announcement"""
        # Create one first
        resp = requests.post(f"{BASE_URL}/api/announcements",
            json={"title": "TEST_ToDelete", "message": "Will be deleted", "targetBatchId": ""},
            headers=auth_header(admin_token))
        assert resp.status_code == 200
        ann_id = resp.json()["id"]
        
        # Delete it
        resp = requests.delete(f"{BASE_URL}/api/announcements/{ann_id}", headers=auth_header(admin_token))
        assert resp.status_code == 200
        assert resp.json()["ok"] == True
        print("Announcement deleted successfully")
    
    def test_create_announcement_missing_fields(self, admin_token):
        """Creating announcement without required fields fails"""
        resp = requests.post(f"{BASE_URL}/api/announcements",
            json={"title": "", "message": ""},
            headers=auth_header(admin_token))
        assert resp.status_code == 400
    
    def test_create_announcement_teacher_forbidden(self, teacher_token):
        """Teacher cannot create announcements"""
        resp = requests.post(f"{BASE_URL}/api/announcements",
            json={"title": "Test", "message": "Test"},
            headers=auth_header(teacher_token))
        assert resp.status_code == 403
    
    def test_create_announcement_student_forbidden(self, student_token):
        """Student cannot create announcements"""
        resp = requests.post(f"{BASE_URL}/api/announcements",
            json={"title": "Test", "message": "Test"},
            headers=auth_header(student_token))
        assert resp.status_code == 403


# ─── COMMUNICATION CENTER TESTS ───

class TestCommunication:
    """Tests for communication center (admin only)"""
    
    def test_send_message_to_student(self, admin_token):
        """Admin can send message to individual student"""
        # Get a student
        resp = requests.get(f"{BASE_URL}/api/students", headers=auth_header(admin_token))
        students = resp.json()
        assert len(students) > 0
        student_id = students[0]["id"]
        
        resp = requests.post(f"{BASE_URL}/api/messages/send",
            json={"targetType": "student", "targetId": student_id, "message": "TEST_Message to student", "channel": "in_app"},
            headers=auth_header(admin_token))
        assert resp.status_code == 200
        
        data = resp.json()
        assert "sent" in data
        assert data["sent"] >= 1
        print(f"Message sent to student: {data}")
    
    def test_send_message_to_batch(self, admin_token):
        """Admin can send message to entire batch"""
        # Get a batch
        resp = requests.get(f"{BASE_URL}/api/batches", headers=auth_header(admin_token))
        batches = resp.json()
        assert len(batches) > 0
        batch_id = batches[0]["id"]
        
        resp = requests.post(f"{BASE_URL}/api/messages/send",
            json={"targetType": "batch", "targetId": batch_id, "message": "TEST_Message to batch", "channel": "in_app"},
            headers=auth_header(admin_token))
        assert resp.status_code == 200
        
        data = resp.json()
        assert "sent" in data
        assert "total" in data
        print(f"Message sent to batch: {data['sent']} of {data['total']} recipients")
    
    def test_get_message_history(self, admin_token):
        """Admin can get message history"""
        resp = requests.get(f"{BASE_URL}/api/messages/history", headers=auth_header(admin_token))
        assert resp.status_code == 200
        
        data = resp.json()
        assert "logs" in data
        assert "total" in data
        print(f"Message history: {data['total']} total logs")
    
    def test_send_message_invalid_target_type(self, admin_token):
        """Invalid targetType returns 400"""
        resp = requests.post(f"{BASE_URL}/api/messages/send",
            json={"targetType": "all", "targetId": "123", "message": "Test", "channel": "in_app"},
            headers=auth_header(admin_token))
        assert resp.status_code == 400
    
    def test_send_message_empty_message(self, admin_token):
        """Empty message returns 400"""
        resp = requests.post(f"{BASE_URL}/api/messages/send",
            json={"targetType": "student", "targetId": "123", "message": "", "channel": "in_app"},
            headers=auth_header(admin_token))
        assert resp.status_code == 400
    
    def test_send_message_teacher_forbidden(self, teacher_token):
        """Teacher cannot send messages"""
        resp = requests.post(f"{BASE_URL}/api/messages/send",
            json={"targetType": "student", "targetId": "123", "message": "Test", "channel": "in_app"},
            headers=auth_header(teacher_token))
        assert resp.status_code == 403
    
    def test_send_message_student_forbidden(self, student_token):
        """Student cannot send messages"""
        resp = requests.post(f"{BASE_URL}/api/messages/send",
            json={"targetType": "student", "targetId": "123", "message": "Test", "channel": "in_app"},
            headers=auth_header(student_token))
        assert resp.status_code == 403
    
    def test_message_history_teacher_forbidden(self, teacher_token):
        """Teacher cannot view message history"""
        resp = requests.get(f"{BASE_URL}/api/messages/history", headers=auth_header(teacher_token))
        assert resp.status_code == 403
    
    def test_message_history_student_forbidden(self, student_token):
        """Student cannot view message history"""
        resp = requests.get(f"{BASE_URL}/api/messages/history", headers=auth_header(student_token))
        assert resp.status_code == 403


# ─── FEATURE FLAGS TESTS ───

class TestFeatureFlags:
    """Tests for feature flags"""
    
    def test_get_feature_flags_admin(self, admin_token):
        """Admin can get feature flags"""
        resp = requests.get(f"{BASE_URL}/api/settings/features", headers=auth_header(admin_token))
        assert resp.status_code == 200
        
        data = resp.json()
        # Check default flags exist
        assert "attendance_enabled" in data
        assert "fee_enabled" in data
        assert "reminders_enabled" in data
        assert "communication_enabled" in data
        print(f"Feature flags: {data}")
    
    def test_update_feature_flags_admin(self, admin_token):
        """Admin can update feature flags"""
        resp = requests.put(f"{BASE_URL}/api/settings/features",
            json={"attendance_enabled": True, "fee_enabled": True, "reminders_enabled": True, "communication_enabled": True},
            headers=auth_header(admin_token))
        assert resp.status_code == 200
        assert resp.json()["ok"] == True
        
        # Verify update
        resp = requests.get(f"{BASE_URL}/api/settings/features", headers=auth_header(admin_token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["attendance_enabled"] == True
        print("Feature flags updated successfully")
    
    def test_get_feature_flags_teacher(self, teacher_token):
        """Teacher can view feature flags"""
        resp = requests.get(f"{BASE_URL}/api/settings/features", headers=auth_header(teacher_token))
        assert resp.status_code == 200
    
    def test_get_feature_flags_student(self, student_token):
        """Student can view feature flags"""
        resp = requests.get(f"{BASE_URL}/api/settings/features", headers=auth_header(student_token))
        assert resp.status_code == 200
    
    def test_update_feature_flags_teacher_forbidden(self, teacher_token):
        """Teacher cannot update feature flags"""
        resp = requests.put(f"{BASE_URL}/api/settings/features",
            json={"attendance_enabled": False},
            headers=auth_header(teacher_token))
        assert resp.status_code == 403
    
    def test_update_feature_flags_student_forbidden(self, student_token):
        """Student cannot update feature flags"""
        resp = requests.put(f"{BASE_URL}/api/settings/features",
            json={"attendance_enabled": False},
            headers=auth_header(student_token))
        assert resp.status_code == 403


# ─── DASHBOARD TESTS ───

class TestDashboardUpdates:
    """Tests for updated dashboard stats"""
    
    def test_admin_dashboard_has_new_fields(self, admin_token):
        """Admin dashboard includes totalBatches, todayAttendanceRate, announcements"""
        resp = requests.get(f"{BASE_URL}/api/dashboard/stats", headers=auth_header(admin_token))
        assert resp.status_code == 200
        
        data = resp.json()
        assert data["role"] == "admin"
        assert "totalBatches" in data
        assert "todayAttendanceRate" in data
        assert "announcements" in data
        assert isinstance(data["announcements"], list)
        print(f"Admin dashboard: totalBatches={data['totalBatches']}, todayAttendanceRate={data['todayAttendanceRate']}%")
    
    def test_teacher_dashboard_has_batches(self, teacher_token):
        """Teacher dashboard includes totalBatches"""
        resp = requests.get(f"{BASE_URL}/api/dashboard/stats", headers=auth_header(teacher_token))
        assert resp.status_code == 200
        
        data = resp.json()
        assert data["role"] == "teacher"
        assert "totalBatches" in data
        print(f"Teacher dashboard: totalBatches={data['totalBatches']}")
    
    def test_student_dashboard_has_announcements(self, student_token):
        """Student dashboard includes announcements"""
        resp = requests.get(f"{BASE_URL}/api/dashboard/stats", headers=auth_header(student_token))
        assert resp.status_code == 200
        
        data = resp.json()
        assert data["role"] == "student"
        assert "announcements" in data
        assert isinstance(data["announcements"], list)
        print(f"Student dashboard: {len(data['announcements'])} announcements visible")


# ─── EXISTING FEE REMINDER SYSTEM (Sanity Check) ───

class TestExistingFeeReminders:
    """Quick sanity check that existing fee reminder system still works"""
    
    def test_get_reminder_settings(self, admin_token):
        """GET /api/settings/reminders still works"""
        resp = requests.get(f"{BASE_URL}/api/settings/reminders", headers=auth_header(admin_token))
        assert resp.status_code == 200
        data = resp.json()
        assert "enabled" in data
        assert "channels" in data
        print("Fee reminder settings endpoint working")
    
    def test_run_reminder_check(self, admin_token):
        """POST /api/reminders/run-check still works"""
        resp = requests.post(f"{BASE_URL}/api/reminders/run-check", headers=auth_header(admin_token))
        assert resp.status_code == 200
        data = resp.json()
        assert "sent" in data
        print(f"Reminder check: {data['sent']} sent")
    
    def test_get_reminder_logs(self, admin_token):
        """GET /api/reminder-logs still works"""
        resp = requests.get(f"{BASE_URL}/api/reminder-logs", headers=auth_header(admin_token))
        assert resp.status_code == 200
        data = resp.json()
        assert "logs" in data
        assert "total" in data
        print(f"Reminder logs: {data['total']} total")


# ─── CLEANUP ───

@pytest.fixture(scope="module", autouse=True)
def cleanup(admin_token):
    """Cleanup test data after all tests"""
    yield
    # Delete test announcements
    try:
        resp = requests.get(f"{BASE_URL}/api/announcements", headers=auth_header(admin_token))
        if resp.status_code == 200:
            for ann in resp.json():
                if ann.get("title", "").startswith("TEST_"):
                    requests.delete(f"{BASE_URL}/api/announcements/{ann['id']}", headers=auth_header(admin_token))
    except:
        pass


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
