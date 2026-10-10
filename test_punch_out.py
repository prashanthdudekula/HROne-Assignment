"""Test punch-out validation and edge cases against isolated test database."""
import os
import sys
import unittest
from datetime import datetime, timezone, timedelta
import pymongo

# Configure isolated test database before importing app.main
os.environ["MONGO_DB"] = "audit_temp_punch_out_test"
os.environ["MONGO_URI"] = os.getenv("MONGO_URI", "mongodb://localhost:27017")
sys.path.insert(0, os.path.dirname(__file__))

import app.main as app_main
from app.main import PunchInIn, PunchOutIn, EmployeeIn
from fastapi import HTTPException

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

def ts(dt_str_ist):
    dt = datetime.fromisoformat(dt_str_ist).replace(tzinfo=IST)
    return int(dt.astimezone(UTC).timestamp() * 1000)

class TestPunchOut(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = pymongo.MongoClient(os.environ["MONGO_URI"])
        cls.db = cls.client[os.environ["MONGO_DB"]]
        # Initialize indexes
        app_main.create_indexes()

    @classmethod
    def tearDownClass(cls):
        # Drop the temporary test database cleanly
        cls.client.drop_database(os.environ["MONGO_DB"])

    def setUp(self):
        self.db.employees.delete_many({})
        self.db.attendance_logs.delete_many({})
        # Create standard test employee
        app_main.create_employee(EmployeeIn(
            emp_code="EMP0001",
            name="Test Employee",
            email="test@example.com",
            department="Engineering",
            shift_start="09:30",
            shift_end="18:30",
            joined_on="2026-01-01"
        ))

    def test_unknown_employee_returns_404(self):
        with self.assertRaises(HTTPException) as ctx:
            app_main.punch_out(PunchOutIn(emp_code="EMP9999", punched_at=ts("2026-07-08T18:30:00")))
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail, "Employee not found")

    def test_no_matching_attendance_record_returns_404(self):
        with self.assertRaises(HTTPException) as ctx:
            app_main.punch_out(PunchOutIn(emp_code="EMP0001", punched_at=ts("2026-07-08T18:30:00")))
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail, "No punch-in found")

    def test_punch_out_before_punch_in_returns_422(self):
        # Punch-in at 09:30:00
        t_in = ts("2026-07-08T09:30:00")
        app_main.punch_in(PunchInIn(emp_code="EMP0001", punched_at=t_in))

        # Punch-out at 09:20:00 (10 minutes before punch-in)
        t_out = ts("2026-07-08T09:20:00")
        with self.assertRaises(HTTPException) as ctx:
            app_main.punch_out(PunchOutIn(emp_code="EMP0001", punched_at=t_out))
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("punch_out must be after punch_in", ctx.exception.detail)

    def test_punch_out_equal_to_punch_in_returns_422(self):
        # Punch-in at 09:30:00
        t_in = ts("2026-07-08T09:30:00")
        app_main.punch_in(PunchInIn(emp_code="EMP0001", punched_at=t_in))

        # Punch-out at exactly 09:30:00 (equal timestamp)
        with self.assertRaises(HTTPException) as ctx:
            app_main.punch_out(PunchOutIn(emp_code="EMP0001", punched_at=t_in))
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("punch_out must be after punch_in", ctx.exception.detail)

    def test_punch_out_more_than_24_hours_returns_422(self):
        # Punch-in at 09:30:00
        t_in = ts("2026-07-08T09:30:00")
        app_main.punch_in(PunchInIn(emp_code="EMP0001", punched_at=t_in))

        # Punch-out at 09:30:01 next day (24 hours + 1 second)
        t_out = ts("2026-07-09T09:30:01")
        with self.assertRaises(HTTPException) as ctx:
            app_main.punch_out(PunchOutIn(emp_code="EMP0001", punched_at=t_out))
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("within 24 hours", ctx.exception.detail)

    def test_already_punched_out_returns_409(self):
        # Punch-in at 09:30:00, punch-out at 18:30:00
        t_in = ts("2026-07-08T09:30:00")
        t_out = ts("2026-07-08T18:30:00")
        app_main.punch_in(PunchInIn(emp_code="EMP0001", punched_at=t_in))
        res = app_main.punch_out(PunchOutIn(emp_code="EMP0001", punched_at=t_out))
        self.assertEqual(res["punch_out"], t_out)

        # Attempt to punch out again
        with self.assertRaises(HTTPException) as ctx:
            app_main.punch_out(PunchOutIn(emp_code="EMP0001", punched_at=ts("2026-07-08T19:00:00")))
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail, "Already punched out")

    def test_valid_punch_out_returns_200(self):
        t_in = ts("2026-07-08T09:30:00")
        t_out = ts("2026-07-08T18:30:00")
        app_main.punch_in(PunchInIn(emp_code="EMP0001", punched_at=t_in))
        record = app_main.punch_out(PunchOutIn(emp_code="EMP0001", punched_at=t_out))
        self.assertEqual(record["punch_out"], t_out)
        self.assertEqual(record["work_hours"], 9.0)
        self.assertFalse(record["half_day"])

    def test_earlier_eligible_and_later_future_punch_in_updates_earlier(self):
        # Earlier eligible punch-in on 2026-07-07 09:30:00
        t_in_earlier = ts("2026-07-07T09:30:00")
        app_main.punch_in(PunchInIn(emp_code="EMP0001", punched_at=t_in_earlier))

        # Later future punch-in on 2026-07-08 09:30:00
        t_in_future = ts("2026-07-08T09:30:00")
        app_main.punch_in(PunchInIn(emp_code="EMP0001", punched_at=t_in_future))

        # Punch-out for earlier day: 2026-07-07 18:30:00
        t_out_earlier = ts("2026-07-07T18:30:00")
        res = app_main.punch_out(PunchOutIn(emp_code="EMP0001", punched_at=t_out_earlier))

        # Must update the earlier record (date 2026-07-07)
        self.assertEqual(res["date"], "2026-07-07")
        self.assertEqual(res["punch_out"], t_out_earlier)
        self.assertEqual(res["work_hours"], 9.0)

        # Future record (date 2026-07-08) must remain open (punch_out is None)
        future_record = self.db.attendance_logs.find_one({"emp_code": "EMP0001", "date": "2026-07-08"})
        self.assertIsNotNone(future_record)
        self.assertIsNone(future_record.get("punch_out"))

if __name__ == "__main__":
    unittest.main()

