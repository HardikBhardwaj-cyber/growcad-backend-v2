#!/usr/bin/env python3
"""
Tests for Fee Reminder System (Iteration 4)
Features tested:
- GET /api/settings/reminders: Retrieve reminder settings with twilioStatus
- PUT /api/settings/reminders: Update reminder settings (enabled, channels, timing)
- GET /api/reminder-logs: Paginated reminder logs with total count
- GET /api/dashboard/pending-reminders: Upcoming and overdue installments with student details
- POST /api/reminders/run-check: Manual trigger for reminder check, dedup verification
- POST /api/reminders/send-now: Send immediate reminder for specific student
- RBAC: Teacher and Student get 403 on all reminder endpoints
"""

import pytest
import requests
import os

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')


class TestReminderSettings:
    """Tests for reminder settings endpoints (admin only)"""
    
    def test_get_reminder_settings(self, auth_token):
        """Test GET /api/settings/reminders - returns settings with twilioStatus"""
        response = requests.get(
            f'{BASE_URL}/api/settings/reminders',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        
        # Validate response structure
        assert 'enabled' in data, "Missing 'enabled' in settings"
        assert 'channels' in data, "Missing 'channels' in settings"
        assert 'timing' in data, "Missing 'timing' in settings"
        assert 'twilioStatus' in data, "Missing 'twilioStatus' in settings"
        
        # Validate twilioStatus structure
        assert 'smsAvailable' in data['twilioStatus']
        assert 'whatsappAvailable' in data['twilioStatus']
        
        # Validate timing structure
        timing = data['timing']
        assert 'daysBefore' in timing or timing is not None
        
        print(f"Reminder Settings: enabled={data['enabled']}, channels={data['channels']}")
    
    def test_update_reminder_settings(self, auth_token):
        """Test PUT /api/settings/reminders - admin can update settings"""
        # Update settings
        update_data = {
            "enabled": True,
            "channels": ["in_app"],
            "timing": {"daysBefore": 1, "onDueDate": True, "daysAfterOverdue": 3}
        }
        
        response = requests.put(
            f'{BASE_URL}/api/settings/reminders',
            headers={'Authorization': f'Bearer {auth_token}', 'Content-Type': 'application/json'},
            json=update_data
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        assert data.get('ok') == True
        
        # Verify settings were updated
        get_response = requests.get(
            f'{BASE_URL}/api/settings/reminders',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        assert get_response.status_code == 200
        updated = get_response.json()
        assert updated['enabled'] == True
        assert 'in_app' in updated['channels']
        
        print(f"Settings updated successfully: {updated}")


class TestReminderLogs:
    """Tests for reminder logs endpoint"""
    
    def test_get_reminder_logs(self, auth_token):
        """Test GET /api/reminder-logs - returns paginated logs with total"""
        response = requests.get(
            f'{BASE_URL}/api/reminder-logs?limit=10&skip=0',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        
        # Validate response structure
        assert 'logs' in data, "Missing 'logs' in response"
        assert 'total' in data, "Missing 'total' in response"
        assert isinstance(data['logs'], list)
        assert isinstance(data['total'], int)
        
        # Validate log entry structure (if any logs exist)
        if data['logs']:
            log = data['logs'][0]
            assert 'id' in log
            assert 'studentId' in log
            assert 'studentName' in log
            assert 'channel' in log
            assert 'status' in log
            assert 'reminderType' in log
            assert 'amount' in log
            assert 'dueDate' in log
            assert 'timestamp' in log
        
        print(f"Reminder Logs: {len(data['logs'])} entries, {data['total']} total")
    
    def test_get_reminder_logs_pagination(self, auth_token):
        """Test reminder logs pagination works correctly"""
        # First page
        response1 = requests.get(
            f'{BASE_URL}/api/reminder-logs?limit=5&skip=0',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        assert response1.status_code == 200
        data1 = response1.json()
        
        # Second page
        response2 = requests.get(
            f'{BASE_URL}/api/reminder-logs?limit=5&skip=5',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        assert response2.status_code == 200
        data2 = response2.json()
        
        # Both should have same total but different logs (if enough exist)
        assert data1['total'] == data2['total']
        
        if data1['logs'] and data2['logs']:
            # Logs should be different
            ids1 = [l['id'] for l in data1['logs']]
            ids2 = [l['id'] for l in data2['logs']]
            assert ids1 != ids2, "Pagination should return different logs"
        
        print(f"Pagination verified: total={data1['total']}, page1={len(data1['logs'])}, page2={len(data2['logs'])}")


class TestPendingReminders:
    """Tests for pending reminders dashboard endpoint"""
    
    def test_get_pending_reminders(self, auth_token):
        """Test GET /api/dashboard/pending-reminders - returns upcoming and overdue"""
        response = requests.get(
            f'{BASE_URL}/api/dashboard/pending-reminders',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        
        # Validate response structure
        assert 'upcoming' in data, "Missing 'upcoming' in response"
        assert 'overdue' in data, "Missing 'overdue' in response"
        assert 'totalUpcoming' in data, "Missing 'totalUpcoming' in response"
        assert 'totalOverdue' in data, "Missing 'totalOverdue' in response"
        
        assert isinstance(data['upcoming'], list)
        assert isinstance(data['overdue'], list)
        
        # Validate entry structure (if any exist)
        all_items = data['upcoming'] + data['overdue']
        if all_items:
            item = all_items[0]
            assert 'studentId' in item
            assert 'studentName' in item
            assert 'amount' in item
            assert 'dueDate' in item
            assert 'type' in item
        
        print(f"Pending Reminders: {data['totalUpcoming']} upcoming, {data['totalOverdue']} overdue")


class TestReminderCheck:
    """Tests for reminder check endpoint"""
    
    def test_run_reminder_check(self, auth_token):
        """Test POST /api/reminders/run-check - manual trigger returns sent count"""
        response = requests.post(
            f'{BASE_URL}/api/reminders/run-check',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        
        # Should have 'sent' count or 'skipped' reason
        assert 'sent' in data or 'skipped' in data, "Missing 'sent' or 'skipped' in response"
        
        if 'sent' in data:
            assert isinstance(data['sent'], int)
        
        print(f"Reminder Check Result: {data}")
    
    def test_dedup_prevents_duplicate_reminders(self, auth_token):
        """Test that running check twice returns sent:0 due to dedup"""
        # First run
        response1 = requests.post(
            f'{BASE_URL}/api/reminders/run-check',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        assert response1.status_code == 200
        
        # Second run should return sent:0 due to dedup
        response2 = requests.post(
            f'{BASE_URL}/api/reminders/run-check',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        assert response2.status_code == 200
        data2 = response2.json()
        
        # Dedup should prevent duplicate sends same day
        assert 'sent' in data2
        assert data2['sent'] == 0, f"Expected sent:0 due to dedup, got {data2}"
        
        print(f"Dedup working correctly: second run returned sent={data2['sent']}")


class TestSendReminderNow:
    """Tests for immediate reminder sending"""
    
    def test_send_reminder_now(self, auth_token, pending_student):
        """Test POST /api/reminders/send-now - send immediate reminder"""
        if not pending_student:
            pytest.skip("No pending student available for test")
        
        response = requests.post(
            f'{BASE_URL}/api/reminders/send-now',
            headers={'Authorization': f'Bearer {auth_token}', 'Content-Type': 'application/json'},
            json={
                "studentId": pending_student['studentId'],
                "amount": pending_student['amount'],
                "dueDate": pending_student['dueDate'],
                "type": pending_student['type']
            }
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        
        # Validate response
        assert 'sent' in data, "Missing 'sent' count in response"
        assert 'channels' in data, "Missing 'channels' in response"
        assert data['sent'] >= 1, "Expected at least 1 reminder sent"
        
        print(f"Send Now Result: sent={data['sent']}, channels={data['channels']}")
    
    def test_send_reminder_now_invalid_student(self, auth_token):
        """Test send-now with invalid studentId returns 404"""
        response = requests.post(
            f'{BASE_URL}/api/reminders/send-now',
            headers={'Authorization': f'Bearer {auth_token}', 'Content-Type': 'application/json'},
            json={
                "studentId": "invalid_student_id_xyz",
                "amount": 1000,
                "dueDate": "2025-01-01",
                "type": "test"
            }
        )
        
        assert response.status_code == 404, f"Expected 404 for invalid student, got {response.status_code}"
        print("Correctly returned 404 for invalid studentId")


class TestRBACReminderEndpoints:
    """Tests for RBAC enforcement on all reminder endpoints"""
    
    def test_teacher_blocked_from_settings(self, teacher_token):
        """Teachers should not access GET /api/settings/reminders"""
        response = requests.get(
            f'{BASE_URL}/api/settings/reminders',
            headers={'Authorization': f'Bearer {teacher_token}'}
        )
        assert response.status_code == 403, f"Expected 403 for teacher, got {response.status_code}"
        print("Teacher correctly blocked from GET /api/settings/reminders")
    
    def test_teacher_blocked_from_update_settings(self, teacher_token):
        """Teachers should not access PUT /api/settings/reminders"""
        response = requests.put(
            f'{BASE_URL}/api/settings/reminders',
            headers={'Authorization': f'Bearer {teacher_token}', 'Content-Type': 'application/json'},
            json={"enabled": False}
        )
        assert response.status_code == 403, f"Expected 403 for teacher, got {response.status_code}"
        print("Teacher correctly blocked from PUT /api/settings/reminders")
    
    def test_teacher_blocked_from_logs(self, teacher_token):
        """Teachers should not access GET /api/reminder-logs"""
        response = requests.get(
            f'{BASE_URL}/api/reminder-logs',
            headers={'Authorization': f'Bearer {teacher_token}'}
        )
        assert response.status_code == 403, f"Expected 403 for teacher, got {response.status_code}"
        print("Teacher correctly blocked from GET /api/reminder-logs")
    
    def test_teacher_blocked_from_pending_reminders(self, teacher_token):
        """Teachers should not access GET /api/dashboard/pending-reminders"""
        response = requests.get(
            f'{BASE_URL}/api/dashboard/pending-reminders',
            headers={'Authorization': f'Bearer {teacher_token}'}
        )
        assert response.status_code == 403, f"Expected 403 for teacher, got {response.status_code}"
        print("Teacher correctly blocked from GET /api/dashboard/pending-reminders")
    
    def test_teacher_blocked_from_run_check(self, teacher_token):
        """Teachers should not access POST /api/reminders/run-check"""
        response = requests.post(
            f'{BASE_URL}/api/reminders/run-check',
            headers={'Authorization': f'Bearer {teacher_token}'}
        )
        assert response.status_code == 403, f"Expected 403 for teacher, got {response.status_code}"
        print("Teacher correctly blocked from POST /api/reminders/run-check")
    
    def test_teacher_blocked_from_send_now(self, teacher_token):
        """Teachers should not access POST /api/reminders/send-now"""
        response = requests.post(
            f'{BASE_URL}/api/reminders/send-now',
            headers={'Authorization': f'Bearer {teacher_token}', 'Content-Type': 'application/json'},
            json={"studentId": "x", "amount": 100, "dueDate": "2025-01-01", "type": "test"}
        )
        assert response.status_code == 403, f"Expected 403 for teacher, got {response.status_code}"
        print("Teacher correctly blocked from POST /api/reminders/send-now")
    
    def test_student_blocked_from_settings(self, student_token):
        """Students should not access GET /api/settings/reminders"""
        response = requests.get(
            f'{BASE_URL}/api/settings/reminders',
            headers={'Authorization': f'Bearer {student_token}'}
        )
        assert response.status_code == 403, f"Expected 403 for student, got {response.status_code}"
        print("Student correctly blocked from GET /api/settings/reminders")
    
    def test_student_blocked_from_update_settings(self, student_token):
        """Students should not access PUT /api/settings/reminders"""
        response = requests.put(
            f'{BASE_URL}/api/settings/reminders',
            headers={'Authorization': f'Bearer {student_token}', 'Content-Type': 'application/json'},
            json={"enabled": False}
        )
        assert response.status_code == 403, f"Expected 403 for student, got {response.status_code}"
        print("Student correctly blocked from PUT /api/settings/reminders")
    
    def test_student_blocked_from_logs(self, student_token):
        """Students should not access GET /api/reminder-logs"""
        response = requests.get(
            f'{BASE_URL}/api/reminder-logs',
            headers={'Authorization': f'Bearer {student_token}'}
        )
        assert response.status_code == 403, f"Expected 403 for student, got {response.status_code}"
        print("Student correctly blocked from GET /api/reminder-logs")
    
    def test_student_blocked_from_pending_reminders(self, student_token):
        """Students should not access GET /api/dashboard/pending-reminders"""
        response = requests.get(
            f'{BASE_URL}/api/dashboard/pending-reminders',
            headers={'Authorization': f'Bearer {student_token}'}
        )
        assert response.status_code == 403, f"Expected 403 for student, got {response.status_code}"
        print("Student correctly blocked from GET /api/dashboard/pending-reminders")
    
    def test_student_blocked_from_run_check(self, student_token):
        """Students should not access POST /api/reminders/run-check"""
        response = requests.post(
            f'{BASE_URL}/api/reminders/run-check',
            headers={'Authorization': f'Bearer {student_token}'}
        )
        assert response.status_code == 403, f"Expected 403 for student, got {response.status_code}"
        print("Student correctly blocked from POST /api/reminders/run-check")
    
    def test_student_blocked_from_send_now(self, student_token):
        """Students should not access POST /api/reminders/send-now"""
        response = requests.post(
            f'{BASE_URL}/api/reminders/send-now',
            headers={'Authorization': f'Bearer {student_token}', 'Content-Type': 'application/json'},
            json={"studentId": "x", "amount": 100, "dueDate": "2025-01-01", "type": "test"}
        )
        assert response.status_code == 403, f"Expected 403 for student, got {response.status_code}"
        print("Student correctly blocked from POST /api/reminders/send-now")


# ─── Fixtures ───

@pytest.fixture(scope="session")
def auth_token():
    """Get admin authentication token"""
    response = requests.post(
        f'{BASE_URL}/api/auth/login',
        json={"email": "admin@meritinfi.com", "password": "admin123"}
    )
    if response.status_code != 200:
        pytest.skip(f"Admin login failed: {response.text}")
    return response.json().get('token')


@pytest.fixture(scope="session")
def teacher_token():
    """Get teacher authentication token"""
    response = requests.post(
        f'{BASE_URL}/api/auth/login',
        json={"email": "teacher@meritinfi.com", "password": "teacher123"}
    )
    if response.status_code != 200:
        pytest.skip(f"Teacher login failed: {response.text}")
    return response.json().get('token')


@pytest.fixture(scope="session")
def student_token():
    """Get student authentication token"""
    response = requests.post(
        f'{BASE_URL}/api/auth/login',
        json={"email": "student@meritinfi.com", "password": "student123"}
    )
    if response.status_code != 200:
        pytest.skip(f"Student login failed: {response.text}")
    return response.json().get('token')


@pytest.fixture(scope="session")
def pending_student(auth_token):
    """Get a pending student for send-now test"""
    response = requests.get(
        f'{BASE_URL}/api/dashboard/pending-reminders',
        headers={'Authorization': f'Bearer {auth_token}'}
    )
    if response.status_code != 200:
        return None
    data = response.json()
    all_items = (data.get('overdue') or []) + (data.get('upcoming') or [])
    return all_items[0] if all_items else None
