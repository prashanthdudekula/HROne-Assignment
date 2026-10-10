"""Employee Attendance & Analytics API — contract v2.0.

Run from the project root: uvicorn app.main:app --port 8000
Configuration: MONGO_URI and MONGO_DB. All analytics execute in MongoDB.
"""
import asyncio
import json
import os
import re
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from bson import Decimal128, ObjectId, json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
EpochMillis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
Limit = Annotated[int, Query(ge=1, le=50)]
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    serverSelectionTimeoutMS=5000, connectTimeoutMS=5000, tz_aware=True,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def create_indexes():
    # Named hints below also apply to the real queries, not only explain.
    db.employees.create_index([("emp_code", 1)], unique=True, name="emp_code_1")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="department_code")
    db.employees.create_index([("joined_on", 1), ("department", 1)], name="joined_department")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="department_joined")
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="emp_code_1_date_1")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="date_employee")
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)], name="status_date_employee")
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="employee_punch")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(create_indexes)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# ---------------------------- Validation and time ----------------------------
def invalid(message):
    raise HTTPException(422, detail=message)


def calendar_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Use YYYY-MM-DD")
    Date.fromisoformat(value)
    return value


def checked_date(value):
    try:
        return calendar_date(value)
    except ValueError:
        invalid("Invalid date; use a real YYYY-MM-DD date")


def month_bounds(month):
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", month):
        invalid("Invalid month; use YYYY-MM")
    try:
        year, number = map(int, month.split("-"))
        first = Date(year, number, 1)
        last = Date(year, number, monthrange(year, number)[1])
    except ValueError:
        invalid("Invalid calendar month")
    return first.isoformat(), last.isoformat()


def now():
    return datetime.now(UTC).replace(microsecond=0)


def instant(milliseconds):
    return datetime.fromtimestamp(milliseconds // 1000, UTC)


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def half_up(value, places=2):
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def json_value(value):
    if isinstance(value, datetime):
        return int(aware(value).timestamp()) * 1000
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, ObjectId):
        return str(value)  # Only raw explain can contain internal BSON IDs.
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_value(v) for v in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    # Atlas/replica-set explain output may contain Timestamp or Binary metadata.
    return json.loads(json_util.dumps(value))


EMPLOYEE_FIELDS = ("emp_code", "name", "email", "department", "shift_start", "shift_end", "joined_on", "created_at")
CHANGE_FIELDS = ("status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day")
DEFAULTS = {"punch_in": None, "punch_out": None, "work_hours": None,
            "late_minutes": 0, "overtime_minutes": 0, "half_day": False, "history": []}


def employee_json(doc):
    return json_value({k: doc[k] for k in EMPLOYEE_FIELDS})


def attendance_json(doc):
    fields = ("emp_code", "date", *CHANGE_FIELDS, "history")
    return json_value({k: doc.get(k, DEFAULTS.get(k)) for k in fields})


def employee_or_404(code):
    employee = db.employees.find_one({"emp_code": code})
    if employee is None:
        raise HTTPException(404, "Employee not found")
    return employee


def attendance_day(punch, employee):
    local = aware(punch).astimezone(IST)
    day = local.date()
    end_hour, end_minute = map(int, employee["shift_end"].split(":"))
    if employee["shift_end"] <= employee["shift_start"]:
        if (local.hour, local.minute, local.second) < (end_hour, end_minute, 0):
            day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(day, employee):
    start = datetime.fromisoformat(day + "T" + employee["shift_start"]).replace(tzinfo=IST)
    end = datetime.fromisoformat(day + "T" + employee["shift_end"]).replace(tzinfo=IST)
    if employee["shift_end"] <= employee["shift_start"]:
        end += timedelta(days=1)
    return start, end


def derived(doc, employee):
    if doc["status"] not in PRESENCE:
        return {"punch_in": None, "punch_out": None, "work_hours": None,
                "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    pin = doc.get("punch_in")
    if pin is None:
        invalid("A presence status requires punch_in")
    pin = aware(pin).replace(microsecond=0)
    if attendance_day(pin, employee) != doc["date"]:
        invalid("punch_in must stay on the record's attendance date")
    start, end = shift_bounds(doc["date"], employee)
    seconds_late = int((pin - start).total_seconds())
    result = {"punch_in": pin, "punch_out": None, "work_hours": None,
              "late_minutes": seconds_late // 60 if seconds_late > 600 else 0,
              "overtime_minutes": 0, "half_day": False}
    if doc.get("punch_out") is not None:
        pout = aware(doc["punch_out"]).replace(microsecond=0)
        seconds = int((pout - pin).total_seconds())
        if seconds <= 0 or seconds > 86400:
            invalid("punch_out must be after punch_in and within 24 hours")
        hours = half_up(Decimal(seconds) / Decimal(3600))
        overtime = int((pout - end).total_seconds()) // 60
        result.update(punch_out=pout, work_hours=hours,
                      overtime_minutes=overtime if overtime >= 30 else 0,
                      half_day=hours < 4.5)
    return result


class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    shift_end: str = Field(default="18:30", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined(cls, value):
        return calendar_date(value)

    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    # A missing timestamp uses now; explicitly supplied null is invalid.
    punched_at: EpochMillis = Field(default_factory=lambda: int(now().timestamp()) * 1000)


class PunchInIn(PunchOutIn):
    status: PresenceStatus = "PRESENT"


class CorrectionIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Status | None = None
    punch_in: EpochMillis | None = None
    punch_out: EpochMillis | None = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null(cls, values):
        if isinstance(values, dict):
            for key in ("status", "punch_in", "punch_out"):
                if key in values and values[key] is None:
                    raise ValueError(f"{key} cannot be null when supplied")
        return values


# ----------------------------- CRUD and corrections -------------------------
@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_json(doc)


@app.get("/employees")
def list_employees(department: str | None = None, page: Page = 1, page_size: PageSize = 20):
    query = {} if department is None else {"department": department}
    hint = "emp_code_1" if department is None else "department_code"
    total = db.employees.count_documents(query, hint=hint)
    cursor = db.employees.find(query).hint(hint).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_json(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    employee = employee_or_404(body.emp_code)
    pin = instant(body.punched_at)
    doc = {"emp_code": body.emp_code, "date": attendance_day(pin, employee),
           "status": body.status, "punch_in": pin, "punch_out": None, "history": []}
    doc.update(derived(doc, employee))
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date")
    return attendance_json(doc)


def snapshot_filter(doc):
    # Optimistic compare-and-swap: include history to detect every correction,
    # including one which changed a value and then restored its previous value.
    query = {"_id": doc["_id"]}
    for field in (*CHANGE_FIELDS, "history"):
        query[field] = {"$eq": doc[field], "$exists": True} if field in doc else {"$exists": False}
    return query


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = employee_or_404(body.emp_code)
    pout = instant(body.punched_at)
    original = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$type": "date", "$lte": pout}},
        sort=[("punch_in", -1)], hint="employee_punch",
    )
    if original is None:
        latest = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_in": {"$type": "date"}},
            sort=[("punch_in", -1)], hint="employee_punch",
        )
        if latest is not None and pout <= aware(latest["punch_in"]).replace(microsecond=0):
            invalid("punch_out must be after punch_in and within 24 hours")
        raise HTTPException(404, "No punch-in found")
    if original.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")
    final = {**original, "punch_out": pout}
    values = derived(final, employee)
    updated = db.attendance_logs.find_one_and_update(
        snapshot_filter(original), {"$set": values}, return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "Record changed concurrently; read it again")
    return attendance_json(updated)


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: CorrectionIn):
    checked_date(date)
    employee = employee_or_404(emp_code)
    original = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if original is None:
        raise HTTPException(404, "Attendance record not found")
    fields = body.model_fields_set
    final = {**DEFAULTS, **original}
    if "status" in fields:
        final["status"] = body.status
    if final["status"] not in PRESENCE and ({"punch_in", "punch_out"} & fields):
        invalid("ABSENT and LEAVE cannot include punch times")
    for field in ("punch_in", "punch_out"):
        if field in fields:
            final[field] = instant(getattr(body, field))
    final.update(derived(final, employee))
    changes = {}
    for field in CHANGE_FIELDS:
        before = original.get(field, DEFAULTS.get(field))
        if before != final[field]:
            changes[field] = {"from": before, "to": final[field]}
    if not changes:
        invalid("Correction changes nothing")
    entry = {"at": now(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    updated = db.attendance_logs.find_one_and_update(
        snapshot_filter(original),
        {"$set": {k: final[k] for k in CHANGE_FIELDS}, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "Record changed concurrently; read it again")
    return attendance_json(updated)


def attendance_query(emp_code=None, date_from=None, date_to=None, status=None):
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None:
        checked_date(date_from)
    if date_to is not None:
        checked_date(date_to)
    if date_from and date_to and date_from > date_to:
        invalid("date_from must not exceed date_to")
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status is not None:
        query["status"] = status
    hint = "emp_code_1_date_1" if emp_code is not None else "status_date_employee" if status is not None else "date_employee"
    return query, hint


def attendance_command(emp_code=None, date_from=None, date_to=None, status=None, page=1, page_size=20):
    query, hint = attendance_query(emp_code, date_from, date_to, status)
    return {"find": "attendance_logs", "filter": query, "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size, "limit": page_size, "hint": hint}


@app.get("/attendance")
def list_attendance(emp_code: str | None = None, date_from: str | None = None,
                    date_to: str | None = None, status: Status | None = None,
                    page: Page = 1, page_size: PageSize = 20):
    command = attendance_command(emp_code, date_from, date_to, status, page, page_size)
    cursor = db.attendance_logs.find(command["filter"]).hint(command["hint"]).sort(list(command["sort"].items()))
    cursor = cursor.skip(command["skip"]).limit(command["limit"])
    total = db.attendance_logs.count_documents(command["filter"], hint=command["hint"])
    return {"items": [attendance_json(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# ------------------------------ Pipeline building ---------------------------
def decimal_expr(value):
    return {"$convert": {"input": value, "to": "decimal", "onNull": Decimal128("0")}}


def rounded(value, places=2):
    """Half-up for nonnegative attendance metrics (Mongo $round uses half-even)."""
    scale = 10 ** places
    return {"$cond": [{"$eq": [value, None]}, None,
        {"$toDouble": {"$divide": [
            {"$floor": {"$add": [{"$multiply": [decimal_expr(value), scale]}, Decimal128("0.5")]}}, scale]}}]}


def date_expr(value):
    return {"$dateFromString": {"dateString": value, "format": "%Y-%m-%d", "timezone": "+05:30"}}


def weekday(value):
    return {"$lte": [{"$isoDayOfWeek": {"date": value, "timezone": "+05:30"}}, 5]}


def weight():
    return {"$cond": [{"$in": ["$status", PRESENCE]},
                     {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}


def stats_group():
    present_weekday = {"$and": [{"$in": ["$status", PRESENCE]}, weekday(date_expr("$date"))]}
    has_hours = {"$and": [{"$in": ["$status", PRESENCE]}, {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]}
    return {"$group": {
        "_id": None,
        "present_days": {"$sum": {"$cond": [present_weekday, weight(), 0]}},
        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        "hours_sum": {"$sum": {"$cond": [has_hours, decimal_expr("$work_hours"), 0]}},
        "hours_count": {"$sum": {"$cond": [has_hours, 1, 0]}},
    }}


def stats_lookup(first, last):
    return {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
            "pipeline": [{"$match": {"date": {"$gte": first, "$lte": last}}}, stats_group()], "as": "stats"}}


def working_days(first, last):
    count = (Date.fromisoformat(last) - Date.fromisoformat(first)).days + 1
    # Generate the month's calendar and count eligible weekdays inside MongoDB.
    return {"$size": {"$filter": {
        "input": {"$map": {"input": {"$range": [0, count]}, "as": "offset",
            "in": {"$dateAdd": {"startDate": date_expr(first), "unit": "day", "amount": "$$offset"}}}},
        "as": "day", "cond": {"$and": [weekday("$$day"), {"$gte": ["$$day", date_expr("$joined_on")]}]},
    }}}


def aggregate_command(collection, pipeline, hint):
    return {"aggregate": collection, "pipeline": pipeline, "cursor": {}, "hint": hint, "allowDiskUse": True}


def monthly_command(code, month):
    first, last = month_bounds(month)
    employee_or_404(code)
    fields = ("present_days", "leave_days", "late_count", "total_late_minutes", "total_overtime_minutes")
    pipeline = [{"$match": {"emp_code": code}}, stats_lookup(first, last),
        {"$set": {"stats": {"$arrayElemAt": ["$stats", 0]}, "working_days": working_days(first, last)}},
        {"$set": {k: {"$ifNull": ["$stats." + k, 0]} for k in fields}},
        {"$project": {"_id": 0, "emp_code": 1, "month": {"$literal": month}, "working_days": 1,
            **{k: 1 for k in fields},
            "attendance_pct": {"$cond": [{"$gt": ["$working_days", 0]},
                rounded({"$divide": [{"$multiply": [decimal_expr("$present_days"), 100]}, "$working_days"]}), None]}}}]
    return aggregate_command("employees", pipeline, "emp_code_1")


def department_command(month, department=None):
    first, last = month_bounds(month)
    query = {"joined_on": {"$lte": last}}
    hint = "joined_department"
    if department is not None:
        query["department"] = department
        hint = "department_joined"
    counters = ("present_days", "late_count", "total_late_minutes", "on_duty_count", "hours_sum", "hours_count")
    pipeline = [{"$match": query}, stats_lookup(first, last),
        {"$set": {"stats": {"$arrayElemAt": ["$stats", 0]}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
            **{k: {"$sum": {"$ifNull": ["$stats." + k, 0]}} for k in counters},
            "leave_count": {"$sum": {"$ifNull": ["$stats.leave_days", 0]}}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1,
            **{k: 1 for k in counters if k not in ("hours_sum", "hours_count")}, "leave_count": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$hours_count", 0]},
                rounded({"$divide": ["$hours_sum", "$hours_count"]}), None]}}},
        {"$sort": {"department": 1}}]
    return aggregate_command("employees", pipeline, hint)


def leaderboard_command(month, limit=10, department=None):
    first, last = month_bounds(month)
    employee_pipeline = [] if department is None else [{"$match": {"department": department}}]
    pipeline = [{"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code",
                     "pipeline": employee_pipeline, "as": "employee"}},
        {"$unwind": "$employee"},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$project": {"_id": 0, "emp_code": "$_id", "name": "$employee.name", "department": "$employee.department",
                      "rank": 1, "total_late_minutes": 1, "late_count": 1}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}]
    return aggregate_command("attendance_logs", pipeline, "date_employee")


def trend_command(department, first, last):
    checked_date(first)
    checked_date(last)
    days = (Date.fromisoformat(last) - Date.fromisoformat(first)).days + 1
    if days < 1 or days > 92:
        invalid("Trend range must contain between 1 and 92 calendar days")
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "Department not found")
    # Indexed department seed -> DB-generated calendar -> indexed daily lookups.
    # $range/$map is intentional: unlike an unseeded $densify it also works when
    # the entire requested range has zero logs, including future joiners only.
    pipeline = [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "join_dates": {"$push": "$joined_on"}}},
        {"$set": {"days": {"$map": {"input": {"$range": [0, days]}, "as": "offset",
            "in": {"$dateAdd": {"startDate": date_expr(first), "unit": "day", "amount": "$$offset"}}}}}},
        {"$unwind": "$days"},
        {"$set": {"date": {"$dateToString": {"date": "$days", "format": "%Y-%m-%d", "timezone": "+05:30"}},
                   "is_working_day": weekday("$days")}},
        {"$set": {"headcount": {"$size": {"$filter": {"input": "$join_dates", "as": "joined",
                    "cond": {"$lte": ["$$joined", "$date"]}}}}}},
        {"$lookup": {"from": "attendance_logs", "localField": "date", "foreignField": "date",
            "pipeline": [
                {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code",
                    "pipeline": [{"$match": {"department": department}}], "as": "employee"}},
                {"$match": {"employee.0": {"$exists": True}}},
                {"$group": {"_id": None, "present_count": {"$sum": weight()},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
            ], "as": "stats"}},
        {"$set": {"stats": {"$arrayElemAt": ["$stats", 0]}}},
        {"$set": {"present_count": {"$ifNull": ["$stats.present_count", 0]},
                  "late_count": {"$ifNull": ["$stats.late_count", 0]}}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            rounded({"$divide": [decimal_expr("$present_count"), "$headcount"]}, 4), None]}}},
        {"$set": {"rate_decimal": {"$cond": [{"$eq": ["$attendance_rate", None]}, None, decimal_expr("$attendance_rate")]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {
            "moving_avg_7d": {"$avg": "$rate_decimal", "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
            "late_count": 1, "attendance_rate": 1, "moving_avg_7d": rounded("$moving_avg_7d", 4)}},
        {"$sort": {"date": 1}},
    ]
    return aggregate_command("employees", pipeline, "department_joined")


def run_aggregate(command):
    # Python only serializes final result rows; grouping, counting, gap filling,
    # ranking, rounding and moving averages are all performed in MongoDB.
    return json_value(list(db[command["aggregate"]].aggregate(
        command["pipeline"], hint=command["hint"], allowDiskUse=command["allowDiskUse"])))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    return run_aggregate(monthly_command(emp_code, month))[0]


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: str | None = None):
    return {"month": month, "items": run_aggregate(department_command(month, department))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: Limit = 10, department: str | None = None):
    return {"month": month, "items": run_aggregate(leaderboard_command(month, limit, department))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, date_from: Annotated[str, Query(alias="from")],
                     date_to: Annotated[str, Query(alias="to")]):
    return {"department": department, "items": run_aggregate(trend_command(department, date_from, date_to))}


ExplainEndpoint = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(endpoint: ExplainEndpoint, emp_code: str | None = None, month: str | None = None,
                     department: str | None = None, limit: Limit = 10, date_from: str | None = None,
                     date_to: str | None = None, status: Status | None = None,
                     from_date: Annotated[str | None, Query(alias="from")] = None,
                     to_date: Annotated[str | None, Query(alias="to")] = None,
                     page: Page = 1, page_size: PageSize = 20):
    if endpoint == "attendance_list":
        command = attendance_command(emp_code, date_from, date_to, status, page, page_size)
    elif endpoint == "department_trend":
        if department is None or from_date is None or to_date is None:
            invalid("department, from and to are required")
        command = trend_command(department, from_date, to_date)
    else:
        if month is None:
            invalid("month is required")
        if endpoint == "employee_monthly":
            if emp_code is None:
                invalid("emp_code is required")
            command = monthly_command(emp_code, month)
        elif endpoint == "department_summary":
            command = department_command(month, department)
        else:
            command = leaderboard_command(month, limit, department)
    explanation = db.command("explain", command, verbosity="executionStats")
    return {"endpoint": endpoint, "collection": command.get("aggregate", command.get("find")),
            "explain": json_value(explanation)}
