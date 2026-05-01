#!/usr/bin/env python3
"""
Tests for CSV Bulk Upload and Reports Module (Iteration 3)
Features tested:
- POST /api/students/bulk-upload: CSV student upload with batch name validation
- POST /api/teachers/bulk-upload: CSV teacher upload with name validation
- GET /api/reports/attendance: Batch-wise attendance, monthly trend, student data
- GET /api/reports/fees: Summary cards, batch breakdown, overdue payments, collection trend
- GET /api/reports/performance: Test results, top students ranking
"""

import pytest
import requests
import os
import tempfile

BASE_URL = os.environ.get('REACT_APP_BACKEND_URL', '').rstrip('/')


class TestCSVUploadStudents:
    """Tests for student CSV bulk upload endpoint"""
    
    @pytest.fixture(autouse=True)
    def setup(self, auth_token):
        self.token = auth_token
        self.headers = {
            'Authorization': f'Bearer {auth_token}'
        }
    
    def test_student_csv_upload_valid_batch(self, auth_token):
        """Test uploading students with valid batch names"""
        csv_content = """name,phone,parentPhone,email,batch
CSV Test Student 1,9876543210,9876543211,csvtest1@test.com,JEE Advanced 2026
CSV Test Student 2,9876543212,9876543213,csvtest2@test.com,NEET 2026
"""
        # Create temp CSV file
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            temp_path = f.name
        
        try:
            with open(temp_path, 'rb') as f:
                files = {'file': ('students.csv', f, 'text/csv')}
                response = requests.post(
                    f'{BASE_URL}/api/students/bulk-upload',
                    headers={'Authorization': f'Bearer {auth_token}'},
                    files=files
                )
            
            assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
            data = response.json()
            
            # Validate response structure
            assert 'summary' in data
            assert 'success' in data
            assert 'failed' in data
            
            # Validate summary fields
            assert 'total' in data['summary']
            assert 'success' in data['summary']
            assert 'failed' in data['summary']
            assert data['summary']['total'] >= 2
            
            print(f"CSV Upload Result: {data['summary']}")
        finally:
            os.unlink(temp_path)
    
    def test_student_csv_upload_invalid_batch(self, auth_token):
        """Test uploading students with invalid batch name - should fail with validation error"""
        csv_content = """name,phone,parentPhone,email,batch
Invalid Batch Student,9876543220,9876543221,invalidbatch@test.com,NonExistent Batch ABC
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            temp_path = f.name
        
        try:
            with open(temp_path, 'rb') as f:
                files = {'file': ('students.csv', f, 'text/csv')}
                response = requests.post(
                    f'{BASE_URL}/api/students/bulk-upload',
                    headers={'Authorization': f'Bearer {auth_token}'},
                    files=files
                )
            
            assert response.status_code == 200
            data = response.json()
            
            # Should have 1 failed record due to invalid batch
            assert data['summary']['failed'] >= 1
            assert len(data['failed']) >= 1
            
            # Verify error message mentions batch not found
            found_batch_error = any("not found" in str(e.get('errors', [])).lower() for e in data['failed'])
            assert found_batch_error, "Expected 'batch not found' error in failed records"
            
            print(f"Correctly rejected invalid batch: {data['failed']}")
        finally:
            os.unlink(temp_path)
    
    def test_student_csv_upload_empty_name(self, auth_token):
        """Test uploading student with empty name - should fail validation"""
        csv_content = """name,phone,parentPhone,email,batch
,9876543230,9876543231,emptyname@test.com,JEE Advanced 2026
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            temp_path = f.name
        
        try:
            with open(temp_path, 'rb') as f:
                files = {'file': ('students.csv', f, 'text/csv')}
                response = requests.post(
                    f'{BASE_URL}/api/students/bulk-upload',
                    headers={'Authorization': f'Bearer {auth_token}'},
                    files=files
                )
            
            assert response.status_code == 200
            data = response.json()
            
            # Should fail because name is empty
            assert data['summary']['failed'] >= 1
            
            # Verify error message mentions name required
            found_name_error = any("name" in str(e.get('errors', [])).lower() for e in data['failed'])
            assert found_name_error, "Expected 'Name is required' error"
            
            print(f"Correctly rejected empty name: {data['failed']}")
        finally:
            os.unlink(temp_path)


class TestCSVUploadTeachers:
    """Tests for teacher CSV bulk upload endpoint"""
    
    def test_teacher_csv_upload_valid(self, auth_token):
        """Test uploading teachers with valid data"""
        csv_content = """name,phone,email,subject
CSV Test Teacher 1,9876543300,csvteacher1@test.com,Mathematics
CSV Test Teacher 2,9876543301,csvteacher2@test.com,English
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            temp_path = f.name
        
        try:
            with open(temp_path, 'rb') as f:
                files = {'file': ('teachers.csv', f, 'text/csv')}
                response = requests.post(
                    f'{BASE_URL}/api/teachers/bulk-upload',
                    headers={'Authorization': f'Bearer {auth_token}'},
                    files=files
                )
            
            assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
            data = response.json()
            
            # Validate response structure
            assert 'summary' in data
            assert 'success' in data
            assert 'failed' in data
            
            assert data['summary']['total'] >= 2
            print(f"Teacher CSV Upload Result: {data['summary']}")
        finally:
            os.unlink(temp_path)
    
    def test_teacher_csv_upload_empty_name(self, auth_token):
        """Test uploading teacher with empty name - should fail validation"""
        csv_content = """name,phone,email,subject
,9876543310,emptyteacher@test.com,Physics
"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            temp_path = f.name
        
        try:
            with open(temp_path, 'rb') as f:
                files = {'file': ('teachers.csv', f, 'text/csv')}
                response = requests.post(
                    f'{BASE_URL}/api/teachers/bulk-upload',
                    headers={'Authorization': f'Bearer {auth_token}'},
                    files=files
                )
            
            assert response.status_code == 200
            data = response.json()
            
            # Should fail because name is empty
            assert data['summary']['failed'] >= 1
            found_name_error = any("name" in str(e.get('errors', [])).lower() for e in data['failed'])
            assert found_name_error, "Expected 'Name is required' error"
            
            print(f"Correctly rejected empty name: {data['failed']}")
        finally:
            os.unlink(temp_path)


class TestReportsAttendance:
    """Tests for attendance report endpoint"""
    
    def test_attendance_report_basic(self, auth_token):
        """Test basic attendance report retrieval"""
        response = requests.get(
            f'{BASE_URL}/api/reports/attendance',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        
        # Validate response structure
        assert 'students' in data, "Missing 'students' in attendance report"
        assert 'batches' in data, "Missing 'batches' in attendance report"
        assert 'monthlyTrend' in data, "Missing 'monthlyTrend' in attendance report"
        
        # Validate students data structure
        if data['students']:
            student = data['students'][0]
            assert 'studentId' in student
            assert 'studentName' in student
            assert 'present' in student
            assert 'absent' in student
            assert 'total' in student
            assert 'rate' in student
        
        # Validate batches data structure
        if data['batches']:
            batch = data['batches'][0]
            assert 'batchId' in batch
            assert 'batchName' in batch
            assert 'rate' in batch
        
        # Validate monthly trend structure
        if data['monthlyTrend']:
            trend = data['monthlyTrend'][0]
            assert 'month' in trend
            assert 'rate' in trend
        
        print(f"Attendance Report: {len(data['students'])} students, {len(data['batches'])} batches, {len(data['monthlyTrend'])} months")
    
    def test_attendance_report_batch_filter(self, auth_token, batches):
        """Test attendance report with batch filter"""
        if not batches:
            pytest.skip("No batches available for filter test")
        
        batch_id = batches[0]['id']
        response = requests.get(
            f'{BASE_URL}/api/reports/attendance?batchId={batch_id}',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        assert response.status_code == 200
        data = response.json()
        
        # Should still have valid structure
        assert 'students' in data
        assert 'batches' in data
        
        print(f"Filtered Attendance Report: {len(data['students'])} students for batch {batch_id}")
    
    def test_attendance_report_date_range(self, auth_token):
        """Test attendance report with date range filter"""
        response = requests.get(
            f'{BASE_URL}/api/reports/attendance?startDate=2025-01-01&endDate=2025-12-31',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        assert response.status_code == 200
        data = response.json()
        
        assert 'students' in data
        assert 'monthlyTrend' in data
        
        print(f"Date Range Attendance Report: {len(data['students'])} students")


class TestReportsFees:
    """Tests for fees report endpoint"""
    
    def test_fees_report_basic(self, auth_token):
        """Test basic fees report retrieval"""
        response = requests.get(
            f'{BASE_URL}/api/reports/fees',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        
        # Validate response structure - summary fields
        assert 'totalCollected' in data, "Missing 'totalCollected'"
        assert 'totalPending' in data, "Missing 'totalPending'"
        assert 'totalFee' in data, "Missing 'totalFee'"
        assert 'batches' in data, "Missing 'batches'"
        assert 'overdue' in data, "Missing 'overdue'"
        assert 'collectionTrend' in data, "Missing 'collectionTrend'"
        
        # Validate data types
        assert isinstance(data['totalCollected'], (int, float))
        assert isinstance(data['totalPending'], (int, float))
        assert isinstance(data['totalFee'], (int, float))
        
        # Validate batch breakdown structure
        if data['batches']:
            batch = data['batches'][0]
            assert 'batchId' in batch
            assert 'batchName' in batch
            assert 'collected' in batch
            assert 'pending' in batch
        
        # Validate overdue structure
        if data['overdue']:
            overdue = data['overdue'][0]
            assert 'studentName' in overdue
            assert 'amount' in overdue
            assert 'dueDate' in overdue
        
        # Validate collection trend structure
        if data['collectionTrend']:
            trend = data['collectionTrend'][0]
            assert 'month' in trend
            assert 'collected' in trend
        
        print(f"Fees Report: Total={data['totalFee']}, Collected={data['totalCollected']}, Pending={data['totalPending']}")
    
    def test_fees_report_batch_filter(self, auth_token, batches):
        """Test fees report with batch filter"""
        if not batches:
            pytest.skip("No batches available for filter test")
        
        batch_id = batches[0]['id']
        response = requests.get(
            f'{BASE_URL}/api/reports/fees?batchId={batch_id}',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        assert response.status_code == 200
        data = response.json()
        
        assert 'totalCollected' in data
        assert 'batches' in data
        
        print(f"Filtered Fees Report: Total={data['totalFee']} for batch {batch_id}")


class TestReportsPerformance:
    """Tests for performance report endpoint"""
    
    def test_performance_report_basic(self, auth_token):
        """Test basic performance report retrieval"""
        response = requests.get(
            f'{BASE_URL}/api/reports/performance',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
        data = response.json()
        
        # Validate response structure
        assert 'tests' in data, "Missing 'tests' in performance report"
        assert 'topStudents' in data, "Missing 'topStudents' in performance report"
        
        # Validate tests data structure
        if data['tests']:
            test = data['tests'][0]
            assert 'testName' in test
            assert 'subject' in test
            assert 'batchName' in test
            assert 'average' in test
            assert 'highest' in test
            assert 'lowest' in test
            assert 'totalStudents' in test
            assert 'maximumMarks' in test
        
        # Validate top students structure
        if data['topStudents']:
            student = data['topStudents'][0]
            assert 'studentId' in student
            assert 'studentName' in student
            assert 'batchName' in student
            assert 'totalMarks' in student
            assert 'totalMax' in student
            assert 'percentage' in student
            assert 'testCount' in student
        
        print(f"Performance Report: {len(data['tests'])} tests, {len(data['topStudents'])} top students")
    
    def test_performance_report_batch_filter(self, auth_token, batches):
        """Test performance report with batch filter"""
        if not batches:
            pytest.skip("No batches available for filter test")
        
        batch_id = batches[0]['id']
        response = requests.get(
            f'{BASE_URL}/api/reports/performance?batchId={batch_id}',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        assert response.status_code == 200
        data = response.json()
        
        assert 'tests' in data
        assert 'topStudents' in data
        
        print(f"Filtered Performance Report: {len(data['tests'])} tests for batch {batch_id}")
    
    def test_performance_report_subject_filter(self, auth_token):
        """Test performance report with subject filter"""
        response = requests.get(
            f'{BASE_URL}/api/reports/performance?subject=Physics',
            headers={'Authorization': f'Bearer {auth_token}'}
        )
        
        assert response.status_code == 200
        data = response.json()
        
        assert 'tests' in data
        
        # All returned tests should be Physics related
        for test in data['tests']:
            assert 'physics' in test.get('subject', '').lower(), f"Expected Physics tests, got {test.get('subject')}"
        
        print(f"Subject Filtered Performance Report: {len(data['tests'])} Physics tests")


class TestRBACCSVUpload:
    """Tests for RBAC enforcement on CSV upload endpoints"""
    
    def test_teacher_cannot_upload_students(self, teacher_token):
        """Teachers should not be able to upload students CSV"""
        csv_content = "name,phone,parentPhone,email,batch\nTest,123,456,test@test.com,JEE Advanced 2026"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            temp_path = f.name
        
        try:
            with open(temp_path, 'rb') as f:
                files = {'file': ('students.csv', f, 'text/csv')}
                response = requests.post(
                    f'{BASE_URL}/api/students/bulk-upload',
                    headers={'Authorization': f'Bearer {teacher_token}'},
                    files=files
                )
            
            assert response.status_code == 403, f"Expected 403 Forbidden for teacher, got {response.status_code}"
            print("Teacher correctly blocked from student CSV upload")
        finally:
            os.unlink(temp_path)
    
    def test_student_cannot_upload_teachers(self, student_token):
        """Students should not be able to upload teachers CSV"""
        csv_content = "name,phone,email,subject\nTest Teacher,123,test@test.com,Math"
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False) as f:
            f.write(csv_content)
            temp_path = f.name
        
        try:
            with open(temp_path, 'rb') as f:
                files = {'file': ('teachers.csv', f, 'text/csv')}
                response = requests.post(
                    f'{BASE_URL}/api/teachers/bulk-upload',
                    headers={'Authorization': f'Bearer {student_token}'},
                    files=files
                )
            
            assert response.status_code == 403, f"Expected 403 Forbidden for student, got {response.status_code}"
            print("Student correctly blocked from teacher CSV upload")
        finally:
            os.unlink(temp_path)


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
def batches(auth_token):
    """Get list of batches for filtering tests"""
    response = requests.get(
        f'{BASE_URL}/api/batches',
        headers={'Authorization': f'Bearer {auth_token}'}
    )
    if response.status_code != 200:
        return []
    return response.json()


@pytest.fixture(autouse=True)
def cleanup_test_students(auth_token):
    """Cleanup CSV test students after tests"""
    yield
    # Note: In production, we'd delete TEST_ prefixed records
    # For now we just pass as seeded data should persist
    pass
