from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import main


def test_invalid_time():
    with pytest.raises(main.UserInputError):
        main.parse_time("Create a meeting tomorrow at 25:00")


def test_valid_24_hour_time():
    assert main.parse_time("Create a meeting tomorrow at 14:30") == (14, 30)


def test_valid_pm_time():
    assert main.parse_time("Create a meeting tomorrow at 5:00 PM") == (17, 0)


def test_title_with_quotes():
    title = main.extract_title(
        'Create a meeting titled "Project Review" next Monday at 10 AM',
        "meeting",
    )
    assert title == "Project Review"


def test_reminder_classification():
    assert main.classify_entry_type(
        "Create a reminder tomorrow at 5 PM"
    ) == "reminder"


def test_meeting_with_attached_reminder_stays_meeting():
    assert main.classify_entry_type(
        "Create a meeting tomorrow at 5 PM and add a reminder one day before"
    ) == "meeting"


def test_attendees():
    assert main.extract_attendees(
        "Invite JOHN@example.com and mary@example.com"
    ) == ("john@example.com", "mary@example.com")
