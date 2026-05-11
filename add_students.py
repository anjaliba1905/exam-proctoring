#!/usr/bin/env python3
"""
add_students.py — Bulk-add students to the local SQLite DB.
Run before distributing the student app.

Usage:
    python add_students.py
"""
from database import init_db, add_student, get_all_students

init_db()

# ── Edit this list ────────────────────────────────────────────────────────────
NEW_STUDENTS = [
    # (student_id,  name,              email,                password,    department)
    ("STU006", "Rahul Sharma",    "rahul@exam.com",     "pass123",  "Computer Science"),
    ("STU007", "Anjali Singh",    "anjali@exam.com",    "pass123",  "Mathematics"),
    ("STU008", "Vikram Nair",     "vikram@exam.com",    "pass123",  "Electronics"),
    ("STU009", "Meera Iyer",      "meera@exam.com",     "pass123",  "Information Technology"),
    ("STU010", "Arjun Rao",       "arjun@exam.com",     "pass123",  "Computer Science"),
]

print(f"{'ID':<10} {'Name':<20} {'Status'}")
print("-" * 50)
for args in NEW_STUDENTS:
    ok, msg = add_student(*args)
    status = "✅ Added" if ok else f"⚠  {msg}"
    print(f"{args[0]:<10} {args[1]:<20} {status}")

print()
print(f"Total students in DB: {len(get_all_students())}")
