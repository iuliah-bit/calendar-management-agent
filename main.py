from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateparser.search import search_dates
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError


# Changing scopes requires deleting token.json and authenticating again.
SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.events.freebusy",
]

YES = {"yes", "y", "da", "d"}
NO = {"no", "n", "nu"}
EMAIL_RE = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
    re.IGNORECASE,
)

WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
    "luni": 0,
    "marți": 1,
    "marti": 1,
    "miercuri": 2,
    "joi": 3,
    "vineri": 4,
    "sâmbătă": 5,
    "sambata": 5,
    "duminică": 6,
    "duminica": 6,
}

TIMEZONE_ALIASES = {
    "bucharest": "Europe/Bucharest",
    "romania": "Europe/Bucharest",
    "london": "Europe/London",
    "uk": "Europe/London",
    "paris": "Europe/Paris",
    "berlin": "Europe/Berlin",
    "madrid": "Europe/Madrid",
    "rome": "Europe/Rome",
    "new york": "America/New_York",
    "eastern": "America/New_York",
    "chicago": "America/Chicago",
    "central": "America/Chicago",
    "denver": "America/Denver",
    "mountain": "America/Denver",
    "los angeles": "America/Los_Angeles",
    "pacific": "America/Los_Angeles",
    "tokyo": "Asia/Tokyo",
    "dubai": "Asia/Dubai",
    "utc": "UTC",
}

DURATION_PATTERNS = (
    (
        r"\b(?:for|duration(?: of)?|durata(?: de)?|timp de|with\s+a)\s*"
        r"(\d+)\s*[- ]?\s*(?:minutes?|minute)\b",
        1,
    ),
    (r"\b(\d+)\s*[- ]minute(?:\s+duration)?\b", 1),
    (
        r"\b(?:for|duration(?: of)?|durata(?: de)?|timp de|with\s+a)\s*"
        r"(\d+)\s*(?:hours?|ore?)\b",
        60,
    ),
)

REMINDER_DAY_BEFORE_RE = re.compile(
    r"\b(?:day\s+before|one\s+day\s+before|cu\s+o\s+zi\s+înainte)\b",
    re.IGNORECASE,
)

TIME_RE = re.compile(
    r"\b(?:at|la)\s*(\d{1,2})(?::(\d{2}))?\s*"
    r"(a\.?m\.?|p\.?m\.?)?\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AgentConfig:
    timezone: str
    calendar_id: str
    workday_start: int
    workday_end: int
    working_days: tuple[int, ...]
    slot_step_minutes: int
    suggestion_days: int


@dataclass(frozen=True)
class CalendarRequest:
    entry_type: str
    title: str
    start: datetime
    end: datetime
    remind_day_before: bool = False
    attendees: tuple[str, ...] = ()
    ambiguous_weekday: bool = False
    location: str = ""
    description: str = ""
    add_google_meet: bool = False
    recurrence: tuple[str, ...] = ()
    source_timezone: str = ""


class UserInputError(ValueError):
    pass


def configure_logging() -> None:
    logging.basicConfig(
        filename="calendar_agent.log",
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        encoding="utf-8",
    )


def parse_hour_setting(value: str, name: str) -> int:
    match = re.fullmatch(r"(\d{1,2})(?::00)?", value.strip())
    if not match:
        raise RuntimeError(f"{name} must look like 09:00 or 17.")
    hour = int(match.group(1))
    if not 0 <= hour <= 23:
        raise RuntimeError(f"{name} must be between 00:00 and 23:00.")
    return hour


def load_config() -> AgentConfig:
    timezone_name = os.getenv("TIMEZONE", "Europe/Bucharest")
    get_timezone(timezone_name)

    days_text = os.getenv(
        "WORKING_DAYS",
        "Monday,Tuesday,Wednesday,Thursday,Friday",
    )
    days: list[int] = []
    for item in days_text.split(","):
        key = item.strip().casefold()
        if key not in WEEKDAYS:
            raise RuntimeError(f"Invalid working day in .env: {item.strip()}")
        days.append(WEEKDAYS[key])

    return AgentConfig(
        timezone=timezone_name,
        calendar_id=os.getenv("GOOGLE_CALENDAR_ID", "primary"),
        workday_start=parse_hour_setting(
            os.getenv("WORKDAY_START", "09:00"),
            "WORKDAY_START",
        ),
        workday_end=parse_hour_setting(
            os.getenv("WORKDAY_END", "17:00"),
            "WORKDAY_END",
        ),
        working_days=tuple(dict.fromkeys(days)),
        slot_step_minutes=int(os.getenv("SLOT_STEP_MINUTES", "30")),
        suggestion_days=int(os.getenv("SUGGESTION_DAYS", "14")),
    )


def get_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise RuntimeError(f"Invalid timezone: {name}") from exc


def extract_source_timezone(text: str, default_timezone: str) -> str:
    lowered = text.casefold()

    explicit = re.search(
        r"\b(?:in|using)\s+([A-Za-z_]+/[A-Za-z_]+)\s+(?:time|timezone)\b",
        text,
        re.IGNORECASE,
    )
    if explicit:
        get_timezone(explicit.group(1))
        return explicit.group(1)

    for alias, zone in sorted(
        TIMEZONE_ALIASES.items(),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        if re.search(
            rf"\b{re.escape(alias)}\s+time\b",
            lowered,
            re.IGNORECASE,
        ):
            return zone

    return default_timezone


def parse_time(text: str) -> tuple[int, int]:
    invalid_colon = re.search(r"\b(\d{1,3}):(\d{2,3})\b", text)
    if invalid_colon:
        raw_hour = int(invalid_colon.group(1))
        raw_minute = int(invalid_colon.group(2))
        if raw_hour > 23 or raw_minute > 59:
            raise UserInputError(
                "Invalid time. Use a value such as 09:30 or 5:00 PM."
            )

    match = TIME_RE.search(text)
    if not match:
        match = re.search(
            r"\b(\d{1,2}):(\d{2})\s*"
            r"(a\.?m\.?|p\.?m\.?)?\b",
            text,
            re.IGNORECASE,
        )

    if not match:
        raise UserInputError(
            "Please specify a clear time, for example 'at 10:00 AM'."
        )

    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").replace(".", "").casefold()

    if meridiem:
        if not 1 <= hour <= 12:
            raise UserInputError(
                "For AM/PM format, the hour must be between 1 and 12."
            )
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0

    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise UserInputError(
            "Invalid time. Use a value such as 09:30 or 5:00 PM."
        )

    return hour, minute


def parse_duration(text: str, entry_type: str) -> int:
    for pattern, multiplier in DURATION_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            minutes = int(match.group(1)) * multiplier
            if not 1 <= minutes <= 1440:
                raise UserInputError(
                    "Duration must be between 1 minute and 24 hours."
                )
            return minutes

    return 15 if entry_type == "reminder" else 30


def parse_weekday(
    text: str,
    timezone_name: str,
    hour: int,
    minute: int,
) -> tuple[datetime | None, bool]:
    tz = get_timezone(timezone_name)
    names = "|".join(map(re.escape, WEEKDAYS))
    match = re.search(
        rf"\b(?:(next|următor(?:ul|ea)?|urmator(?:ul|ea)?)\s+)?"
        rf"({names})\b",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None, False

    explicit_next = bool(match.group(1))
    weekday = WEEKDAYS[match.group(2).casefold()]
    now = datetime.now(tz)
    days_ahead = (weekday - now.weekday()) % 7

    if days_ahead == 0:
        days_ahead = 7

    start = (now + timedelta(days=days_ahead)).replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )
    return start, not explicit_next


def strip_non_date_parts(text: str) -> str:
    cleaned = text

    for pattern, _ in DURATION_PATTERNS:
        cleaned = re.sub(pattern, " ", cleaned, flags=re.IGNORECASE)

    cleaned = REMINDER_DAY_BEFORE_RE.sub(" ", cleaned)
    cleaned = EMAIL_RE.sub(" ", cleaned)

    cleaned = re.sub(
        r'\b(?:with\s+(?:the\s+)?title|title|titled|called|named|'
        r'cu\s+titlul)\s*[:=-]?\s*"[^"]+"',
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(?:with\s+(?:the\s+)?title|title|titled|called|named|"
        r"cu\s+titlul)\s*[:=-]?\s*'[^']+'",
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )

    cleaned = re.sub(
        r"\b(?:location|in room|at location|locația|locatia)\s*"
        r'[:=-]?\s*"[^"]+"',
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"\b(?:description|details|descriere)\s*"
        r'[:=-]?\s*"[^"]+"',
        " ",
        cleaned,
        flags=re.IGNORECASE,
    )

    return re.sub(r"\s+", " ", cleaned).strip(" ,.-")


def parse_datetime(
    text: str,
    default_timezone: str,
) -> tuple[datetime, bool, str]:
    source_timezone = extract_source_timezone(text, default_timezone)
    source_tz = get_timezone(source_timezone)
    target_tz = get_timezone(default_timezone)
    hour, minute = parse_time(text)

    weekday_date, ambiguous = parse_weekday(
        text,
        source_timezone,
        hour,
        minute,
    )
    if weekday_date:
        return weekday_date.astimezone(target_tz), ambiguous, source_timezone

    found = search_dates(
        strip_non_date_parts(text),
        languages=["en", "ro"],
        settings={
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": datetime.now(source_tz),
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TIMEZONE": source_timezone,
            "TO_TIMEZONE": source_timezone,
        },
    )

    if not found:
        raise UserInputError(
            "No date was found. Try 'next Monday at 10:00 AM'."
        )

    parsed = found[0][1]
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=source_tz)
    else:
        parsed = parsed.astimezone(source_tz)

    parsed = parsed.replace(
        hour=hour,
        minute=minute,
        second=0,
        microsecond=0,
    )
    return parsed.astimezone(target_tz), False, source_timezone


def classify_entry_type(text: str) -> str:
    lowered = text.casefold()
    meeting = any(
        value in lowered
        for value in (
            "meeting",
            "ședință",
            "sedinta",
            "întâlnire",
            "intalnire",
        )
    )
    reminder = any(
        value in lowered
        for value in (
            "reminder",
            "remind me",
            "memento",
            "adu-mi aminte",
            "reamintește-mi",
        )
    )
    return "reminder" if reminder and not meeting else "meeting"


def extract_title(text: str, entry_type: str) -> str:
    markers = (
        r"(?:with\s+(?:the\s+)?title|title|titled|called|named|"
        r"cu\s+titlul)"
    )

    for pattern in (
        rf'\b{markers}\s*[:=-]?\s*"([^"]+)"',
        rf"\b{markers}\s*[:=-]?\s*'([^']+)'",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match and match.group(1).strip():
            return match.group(1).strip(" ,.-")

    match = re.search(
        rf"\b{markers}\s*[:=-]?\s*(.+?)(?="
        r"\s+(?:next\s+)?"
        r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
        r"|\s+(?:tomorrow|today|on|at|la|pe)\b"
        r"|\s+for\s+\d"
        r"|\s+with\s+[\w.+-]+@"
        r"|\s+(?:location|description|in room)\b"
        r"|$)",
        text,
        re.IGNORECASE,
    )
    if match and match.group(1).strip():
        return match.group(1).strip(' "\' ,.-')

    return "Reminder" if entry_type == "reminder" else "Meeting"


def extract_attendees(text: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            email.casefold()
            for email in EMAIL_RE.findall(text)
        )
    )


def extract_quoted_field(text: str, names: tuple[str, ...]) -> str:
    marker = "|".join(re.escape(name) for name in names)
    for quote in ('"', "'"):
        pattern = (
            rf"\b(?:{marker})\s*[:=-]?\s*"
            rf"{re.escape(quote)}(.+?){re.escape(quote)}"
        )
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return ""


def extract_location(text: str) -> str:
    quoted = extract_quoted_field(
        text,
        ("location", "at location", "in room", "locația", "locatia"),
    )
    if quoted:
        return quoted

    match = re.search(
        r"\b(?:in\s+room|at\s+location|location)\s+"
        r"(.+?)(?=\s+(?:on|at|next|tomorrow|for|with|and)\b|$)",
        text,
        re.IGNORECASE,
    )
    return match.group(1).strip(" ,.-") if match else ""


def extract_description(text: str) -> str:
    return extract_quoted_field(
        text,
        ("description", "details", "descriere"),
    )


def wants_google_meet(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:google\s+meet|meet\s+link|online\s+meeting|"
            r"video\s+meeting|video\s+call)\b",
            text,
            re.IGNORECASE,
        )
    )


def parse_recurrence(text: str, start: datetime) -> tuple[str, ...]:
    lowered = text.casefold()

    if not re.search(
        r"\b(?:every|each|daily|weekly|monthly|yearly|"
        r"în fiecare|in fiecare|zilnic|săptămânal|saptamanal|lunar)\b",
        lowered,
    ):
        return ()

    rule = ""

    if re.search(r"\b(?:every day|daily|zilnic)\b", lowered):
        rule = "RRULE:FREQ=DAILY"
    elif re.search(
        r"\b(?:every weekday|each weekday|weekdays|"
        r"în fiecare zi lucrătoare|in fiecare zi lucratoare)\b",
        lowered,
    ):
        rule = "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
    elif re.search(r"\b(?:every week|weekly|săptămânal|saptamanal)\b", lowered):
        day_codes = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
        rule = f"RRULE:FREQ=WEEKLY;BYDAY={day_codes[start.weekday()]}"
    elif re.search(r"\b(?:every month|monthly|lunar)\b", lowered):
        rule = f"RRULE:FREQ=MONTHLY;BYMONTHDAY={start.day}"
    elif re.search(r"\b(?:every year|yearly|annually)\b", lowered):
        rule = (
            f"RRULE:FREQ=YEARLY;BYMONTH={start.month};"
            f"BYMONTHDAY={start.day}"
        )
    else:
        weekday_match = re.search(
            r"\bevery\s+"
            r"(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
            lowered,
        )
        if weekday_match:
            codes = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
            rule = (
                "RRULE:FREQ=WEEKLY;BYDAY="
                + codes[WEEKDAYS[weekday_match.group(1)]]
            )

    if not rule:
        raise UserInputError(
            "I recognized a recurrence request but not its frequency."
        )

    count_match = re.search(
        r"\b(?:for)\s+(\d+)\s+(?:times?|occurrences?|weeks?|months?)\b",
        lowered,
    )
    until_match = re.search(
        r"\buntil\s+(.+)$",
        text,
        re.IGNORECASE,
    )

    if count_match:
        count = int(count_match.group(1))
        if not 1 <= count <= 365:
            raise UserInputError(
                "Recurrence count must be between 1 and 365."
            )
        rule += f";COUNT={count}"
    elif until_match:
        tz = start.tzinfo or ZoneInfo("UTC")
        found = search_dates(
            until_match.group(1),
            languages=["en", "ro"],
            settings={
                "PREFER_DATES_FROM": "future",
                "RELATIVE_BASE": start,
            },
        )
        if not found:
            raise UserInputError(
                "I could not understand the recurrence end date."
            )
        until = found[0][1]
        if until.tzinfo is None:
            until = until.replace(tzinfo=tz)
        until_utc = until.astimezone(ZoneInfo("UTC"))
        rule += f";UNTIL={until_utc:%Y%m%dT%H%M%SZ}"

    return (rule,)


def parse_create_request(
    text: str,
    config: AgentConfig,
) -> CalendarRequest:
    entry_type = classify_entry_type(text)
    start, ambiguous, source_timezone = parse_datetime(
        text,
        config.timezone,
    )
    duration = parse_duration(text, entry_type)

    return CalendarRequest(
        entry_type=entry_type,
        title=extract_title(text, entry_type),
        start=start,
        end=start + timedelta(minutes=duration),
        remind_day_before=bool(REMINDER_DAY_BEFORE_RE.search(text)),
        attendees=extract_attendees(text),
        ambiguous_weekday=ambiguous,
        location=extract_location(text),
        description=extract_description(text),
        add_google_meet=wants_google_meet(text),
        recurrence=parse_recurrence(text, start),
        source_timezone=source_timezone,
    )


def get_calendar_service() -> Any:
    client_file = os.getenv("GOOGLE_CLIENT_SECRET_FILE")
    token_file = os.getenv("GOOGLE_TOKEN_FILE", "token.json")

    if not client_file:
        raise RuntimeError(
            "GOOGLE_CLIENT_SECRET_FILE is missing from .env."
        )
    if not Path(client_file).exists():
        raise RuntimeError(
            f"OAuth credentials file does not exist: {client_file}"
        )

    credentials: Credentials | None = None

    if Path(token_file).exists():
        try:
            credentials = Credentials.from_authorized_user_file(
                token_file,
                SCOPES,
            )
        except (ValueError, OSError):
            credentials = None

    if not credentials or not credentials.valid:
        if (
            credentials
            and credentials.expired
            and credentials.refresh_token
        ):
            credentials.refresh(Request())
        else:
            credentials = (
                InstalledAppFlow.from_client_secrets_file(
                    client_file,
                    SCOPES,
                )
                .run_local_server(port=0)
            )

        Path(token_file).write_text(
            credentials.to_json(),
            encoding="utf-8",
        )

    return build("calendar", "v3", credentials=credentials)


def event_datetime(
    event: dict[str, Any],
    key: str,
    timezone_name: str,
) -> datetime | None:
    value = event.get(key, {}).get("dateTime")
    if not value:
        return None

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(get_timezone(timezone_name))


def event_date_text(
    event: dict[str, Any],
    timezone_name: str,
) -> str:
    start = event_datetime(event, "start", timezone_name)
    if start:
        end = event_datetime(event, "end", timezone_name)
        end_text = end.strftime("%H:%M") if end else "?"
        return f"{start:%d.%m.%Y %H:%M}–{end_text}"

    all_day = event.get("start", {}).get("date")
    return f"{all_day} (all day)" if all_day else "unknown date"


def search_events(
    service: Any,
    calendar_id: str,
    title: str,
    timezone_name: str,
    past_days: int = 30,
    future_days: int = 730,
) -> list[dict[str, Any]]:
    tz = get_timezone(timezone_name)
    now = datetime.now(tz)

    response = (
        service.events()
        .list(
            calendarId=calendar_id,
            q=title,
            timeMin=(now - timedelta(days=past_days)).isoformat(),
            timeMax=(now + timedelta(days=future_days)).isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=50,
        )
        .execute()
    )

    return [
        event
        for event in response.get("items", [])
        if event.get("status") != "cancelled"
        and title.casefold()
        in event.get("summary", "").casefold()
    ]


def choose_event(
    events: list[dict[str, Any]],
    timezone_name: str,
) -> dict[str, Any] | None:
    if not events:
        return None

    if len(events) == 1:
        return events[0]

    print("Agent: I found several matching events:")
    for index, event in enumerate(events, start=1):
        print(
            f"  {index}. {event.get('summary', 'Untitled')} — "
            f"{event_date_text(event, timezone_name)}"
        )

    answer = input(
        "Choose the event number or type 'cancel': "
    ).strip().casefold()

    if answer == "cancel":
        return None
    if not answer.isdigit():
        raise UserInputError("Invalid event selection.")

    index = int(answer)
    if not 1 <= index <= len(events):
        raise UserInputError("Invalid event selection.")

    return events[index - 1]


def build_event_body(
    request: CalendarRequest,
    timezone_name: str,
) -> dict[str, Any]:
    description = request.description or (
        "Created by Calendar Management Agent. "
        f"Entry type: {request.entry_type}."
    )

    body: dict[str, Any] = {
        "summary": request.title,
        "description": description,
        "start": {
            "dateTime": request.start.isoformat(),
            "timeZone": timezone_name,
        },
        "end": {
            "dateTime": request.end.isoformat(),
            "timeZone": timezone_name,
        },
    }

    if request.location:
        body["location"] = request.location

    if request.attendees:
        body["attendees"] = [
            {"email": email}
            for email in request.attendees
        ]

    if request.remind_day_before:
        body["reminders"] = {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 1440},
            ],
        }

    if request.recurrence:
        body["recurrence"] = list(request.recurrence)

    if request.add_google_meet:
        body["conferenceData"] = {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {
                    "type": "hangoutsMeet",
                },
            }
        }

    return body


def create_event(
    service: Any,
    request: CalendarRequest,
    calendar_id: str,
    timezone_name: str,
) -> dict[str, Any]:
    return (
        service.events()
        .insert(
            calendarId=calendar_id,
            body=build_event_body(request, timezone_name),
            sendUpdates="all",
            conferenceDataVersion=1,
        )
        .execute()
    )


def get_busy_periods(
    service: Any,
    calendar_ids: tuple[str, ...],
    start: datetime,
    end: datetime,
    timezone_name: str,
) -> tuple[list[tuple[datetime, datetime]], dict[str, str]]:
    if not calendar_ids:
        return [], {}

    body = {
        "timeMin": start.isoformat(),
        "timeMax": end.isoformat(),
        "timeZone": timezone_name,
        "items": [{"id": item} for item in calendar_ids],
    }

    response = service.freebusy().query(body=body).execute()
    busy: list[tuple[datetime, datetime]] = []
    unavailable: dict[str, str] = {}

    for calendar_id, data in response.get("calendars", {}).items():
        errors = data.get("errors", [])
        if errors:
            unavailable[calendar_id] = errors[0].get(
                "reason",
                "availability unavailable",
            )
            continue

        for period in data.get("busy", []):
            period_start = datetime.fromisoformat(
                period["start"].replace("Z", "+00:00")
            )
            period_end = datetime.fromisoformat(
                period["end"].replace("Z", "+00:00")
            )
            busy.append((period_start, period_end))

    return busy, unavailable


def periods_overlap(
    start_a: datetime,
    end_a: datetime,
    start_b: datetime,
    end_b: datetime,
) -> bool:
    return start_a < end_b and start_b < end_a


def interval_conflicts(
    service: Any,
    config: AgentConfig,
    start: datetime,
    end: datetime,
    attendees: tuple[str, ...] = (),
    ignore_event_id: str | None = None,
) -> tuple[list[str], dict[str, str]]:
    conflicts: list[str] = []

    owner_events = (
        service.events()
        .list(
            calendarId=config.calendar_id,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
        )
        .execute()
        .get("items", [])
    )

    for event in owner_events:
        if event.get("status") == "cancelled":
            continue
        if event.get("id") == ignore_event_id:
            continue
        if "dateTime" not in event.get("start", {}):
            continue
        conflicts.append(event.get("summary", "Busy event"))

    attendee_ids = tuple(dict.fromkeys(attendees))
    unavailable: dict[str, str] = {}

    if attendee_ids:
        busy_periods, unavailable = get_busy_periods(
            service,
            attendee_ids,
            start,
            end,
            config.timezone,
        )
        if busy_periods:
            conflicts.append("One or more guests are busy")

    return conflicts, unavailable


def next_working_time(
    value: datetime,
    config: AgentConfig,
) -> datetime:
    candidate = value.replace(second=0, microsecond=0)

    while candidate.weekday() not in config.working_days:
        candidate = (candidate + timedelta(days=1)).replace(
            hour=config.workday_start,
            minute=0,
        )

    day_start = candidate.replace(
        hour=config.workday_start,
        minute=0,
    )
    day_end = candidate.replace(
        hour=config.workday_end,
        minute=0,
    )

    if candidate < day_start:
        return day_start
    if candidate >= day_end:
        return next_working_time(
            (candidate + timedelta(days=1)).replace(
                hour=config.workday_start,
                minute=0,
            ),
            config,
        )

    return candidate


def find_available_suggestions(
    service: Any,
    config: AgentConfig,
    requested_start: datetime,
    duration: timedelta,
    attendees: tuple[str, ...],
    ignore_event_id: str | None = None,
    count: int = 3,
) -> list[tuple[datetime, datetime]]:
    suggestions: list[tuple[datetime, datetime]] = []
    step = timedelta(minutes=config.slot_step_minutes)
    candidate = next_working_time(requested_start + step, config)
    limit = requested_start + timedelta(days=config.suggestion_days)

    while candidate < limit and len(suggestions) < count:
        candidate = next_working_time(candidate, config)
        candidate_end = candidate + duration
        day_end = candidate.replace(
            hour=config.workday_end,
            minute=0,
        )

        if candidate_end > day_end:
            candidate = next_working_time(
                (candidate + timedelta(days=1)).replace(
                    hour=config.workday_start,
                    minute=0,
                ),
                config,
            )
            continue

        conflicts, _ = interval_conflicts(
            service,
            config,
            candidate,
            candidate_end,
            attendees,
            ignore_event_id,
        )

        if not conflicts:
            suggestions.append((candidate, candidate_end))

        candidate += step

    return suggestions


def select_suggestion(
    suggestions: list[tuple[datetime, datetime]],
) -> tuple[datetime, datetime] | None:
    if not suggestions:
        return None

    print("Agent: Available alternatives:")
    for index, (start, end) in enumerate(suggestions, start=1):
        print(
            f"  {index}. {start:%A, %d %B %Y at %H:%M}"
            f"–{end:%H:%M}"
        )

    answer = input(
        "Choose 1, 2, 3 or type 'no': "
    ).strip().casefold()

    if answer in NO:
        return None
    if not answer.isdigit():
        raise UserInputError("Invalid alternative selection.")

    index = int(answer)
    if not 1 <= index <= len(suggestions):
        raise UserInputError("Invalid alternative selection.")

    return suggestions[index - 1]


def resolve_conflict(
    service: Any,
    config: AgentConfig,
    request: CalendarRequest,
    ignore_event_id: str | None = None,
) -> CalendarRequest | None:
    conflicts, unavailable = interval_conflicts(
        service,
        config,
        request.start,
        request.end,
        request.attendees,
        ignore_event_id,
    )

    for email, reason in unavailable.items():
        print(
            f"Agent: Availability for {email} could not be checked "
            f"({reason})."
        )

    if not conflicts:
        print("Agent: The requested interval is available.")
        return request

    print("Agent: The requested interval is occupied:")
    for conflict in conflicts[:5]:
        print(f"  - {conflict}")

    suggestions = find_available_suggestions(
        service,
        config,
        request.start,
        request.end - request.start,
        request.attendees,
        ignore_event_id,
    )

    selected = select_suggestion(suggestions)
    if selected is None:
        print("Agent: No alternative was selected.")
        return None

    new_start, new_end = selected
    return replace(
        request,
        start=new_start,
        end=new_end,
        ambiguous_weekday=False,
    )


def cancel_event(
    service: Any,
    calendar_id: str,
    event: dict[str, Any],
) -> None:
    (
        service.events()
        .delete(
            calendarId=calendar_id,
            eventId=event["id"],
            sendUpdates="all",
        )
        .execute()
    )


def update_event_resource(
    service: Any,
    calendar_id: str,
    event: dict[str, Any],
) -> dict[str, Any]:
    return (
        service.events()
        .update(
            calendarId=calendar_id,
            eventId=event["id"],
            body=event,
            sendUpdates="all",
            conferenceDataVersion=1,
        )
        .execute()
    )


def reschedule_event(
    service: Any,
    config: AgentConfig,
    event: dict[str, Any],
    new_start: datetime,
) -> dict[str, Any] | None:
    old_start = event_datetime(
        event,
        "start",
        config.timezone,
    )
    old_end = event_datetime(
        event,
        "end",
        config.timezone,
    )

    if not old_start or not old_end:
        raise UserInputError(
            "All-day events cannot be rescheduled by this command."
        )

    request = CalendarRequest(
        entry_type="meeting",
        title=event.get("summary", "Meeting"),
        start=new_start,
        end=new_start + (old_end - old_start),
        attendees=tuple(
            attendee.get("email", "")
            for attendee in event.get("attendees", [])
            if attendee.get("email")
        ),
    )

    resolved = resolve_conflict(
        service,
        config,
        request,
        event.get("id"),
    )
    if resolved is None:
        return None

    updated = (
        service.events()
        .get(
            calendarId=config.calendar_id,
            eventId=event["id"],
        )
        .execute()
    )
    updated["start"] = {
        "dateTime": resolved.start.isoformat(),
        "timeZone": config.timezone,
    }
    updated["end"] = {
        "dateTime": resolved.end.isoformat(),
        "timeZone": config.timezone,
    }

    return update_event_resource(
        service,
        config.calendar_id,
        updated,
    )


def extract_action_title(text: str) -> str:
    quoted = re.search(r'["\']([^"\']+)["\']', text)
    if quoted:
        return quoted.group(1).strip()

    match = re.search(
        r"\b(?:cancel|delete|remove|reschedule|move|reprogram|"
        r"rename|modify|edit|update|anuleaz[ăa]|șterge|sterge|"
        r"reprogrameaz[ăa]|modific[ăa])\s+"
        r"(?:the\s+)?(?:meeting|reminder|event|întâlnirea|"
        r"intalnirea|memento)?\s*"
        r"(?:called|named|titled|with\s+title)?\s*(.+?)"
        r"(?=\s+(?:to|on|at|next|tomorrow|today|pe|la|"
        r"add|remove|change|set)\b|$)",
        text,
        re.IGNORECASE,
    )

    if not match or not match.group(1).strip(" ,.-"):
        raise UserInputError(
            'Please specify the event title, preferably in quotes.'
        )

    return match.group(1).strip(' "\' ,.-')


def list_events(
    service: Any,
    config: AgentConfig,
    text: str,
) -> None:
    tz = get_timezone(config.timezone)
    now = datetime.now(tz)

    if re.search(r"\btoday\b", text, re.IGNORECASE):
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif re.search(r"\btomorrow\b", text, re.IGNORECASE):
        start = (
            now + timedelta(days=1)
        ).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
    elif re.search(r"\bnext week\b", text, re.IGNORECASE):
        days_until_monday = (7 - now.weekday()) % 7 or 7
        start = (
            now + timedelta(days=days_until_monday)
        ).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=7)
    else:
        names = "|".join(map(re.escape, WEEKDAYS))
        weekday_match = re.search(
            rf"\b(?:next\s+)?({names})\b",
            text,
            re.IGNORECASE,
        )
        if weekday_match:
            weekday = WEEKDAYS[weekday_match.group(1).casefold()]
            days_ahead = (weekday - now.weekday()) % 7
            if days_ahead == 0:
                days_ahead = 7
            start = (
                now + timedelta(days=days_ahead)
            ).replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=1)
        else:
            start = now
            end = now + timedelta(days=7)

    items = (
        service.events()
        .list(
            calendarId=config.calendar_id,
            timeMin=start.isoformat(),
            timeMax=end.isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=100,
        )
        .execute()
        .get("items", [])
    )

    items = [
        event
        for event in items
        if event.get("status") != "cancelled"
    ]

    if not items:
        print("Agent: No events were found in that period.\n")
        return

    print("Agent: Events found:")
    for event in items:
        print(
            f"  - {event.get('summary', 'Untitled')} — "
            f"{event_date_text(event, config.timezone)}"
        )
    print()


def modify_event(
    service: Any,
    config: AgentConfig,
    event: dict[str, Any],
    text: str,
) -> dict[str, Any]:
    updated = (
        service.events()
        .get(
            calendarId=config.calendar_id,
            eventId=event["id"],
        )
        .execute()
    )

    changed = False

    new_title_match = re.search(
        r"\b(?:rename|change\s+(?:the\s+)?title|set\s+(?:the\s+)?title)"
        r".*?\bto\s+[\"']([^\"']+)[\"']",
        text,
        re.IGNORECASE,
    )
    if new_title_match:
        updated["summary"] = new_title_match.group(1).strip()
        changed = True

    description = extract_description(text)
    if description:
        updated["description"] = description
        changed = True

    location = extract_location(text)
    if location:
        updated["location"] = location
        changed = True

    emails = extract_attendees(text)
    if emails:
        existing = {
            item.get("email", "").casefold(): item
            for item in updated.get("attendees", [])
            if item.get("email")
        }

        if re.search(
            r"\bremove\s+(?:guest|attendee)?\b",
            text,
            re.IGNORECASE,
        ):
            for email in emails:
                existing.pop(email, None)
        else:
            for email in emails:
                existing[email] = {"email": email}

        updated["attendees"] = list(existing.values())
        changed = True

    duration_match = None
    for pattern, multiplier in DURATION_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            duration_match = int(match.group(1)) * multiplier
            break

    if duration_match:
        start = event_datetime(
            updated,
            "start",
            config.timezone,
        )
        if not start:
            raise UserInputError(
                "The duration of an all-day event cannot be changed."
            )
        updated["end"] = {
            "dateTime": (
                start + timedelta(minutes=duration_match)
            ).isoformat(),
            "timeZone": config.timezone,
        }
        changed = True

    if wants_google_meet(text) and not updated.get("conferenceData"):
        updated["conferenceData"] = {
            "createRequest": {
                "requestId": str(uuid.uuid4()),
                "conferenceSolutionKey": {
                    "type": "hangoutsMeet",
                },
            }
        }
        changed = True

    if not changed:
        raise UserInputError(
            "No supported change was found. You can change the title, "
            "duration, guests, location, description, or add Google Meet."
        )

    return update_event_resource(
        service,
        config.calendar_id,
        updated,
    )


def show_request(request: CalendarRequest) -> None:
    print("\nAgent: I understood the request as follows:")
    print(f"  Type: {request.entry_type}")
    print(f"  Title: {request.title}")
    print(f"  Start: {request.start:%d.%m.%Y %H:%M}")
    print(f"  End: {request.end:%d.%m.%Y %H:%M}")
    print(
        "  Reminder one day before: "
        f"{'yes' if request.remind_day_before else 'no'}"
    )
    print(
        "  Guests: "
        f"{', '.join(request.attendees) if request.attendees else 'none'}"
    )
    print(f"  Location: {request.location or 'none'}")
    print(f"  Description: {request.description or 'none'}")
    print(
        "  Google Meet: "
        f"{'yes' if request.add_google_meet else 'no'}"
    )
    print(
        "  Recurrence: "
        f"{request.recurrence[0] if request.recurrence else 'none'}"
    )


def detect_action(text: str) -> str:
    lowered = text.casefold()

    if re.search(
        r"\b(?:show|list|display|what\s+do\s+i\s+have|"
        r"arată|arata|listeaz[ăa])\b",
        lowered,
    ):
        return "list"

    if re.search(
        r"\b(?:cancel|delete|remove\s+the\s+event|"
        r"anuleaz[ăa]|șterge|sterge)\b",
        lowered,
    ):
        return "cancel"

    if re.search(
        r"\b(?:reschedule|move|reprogram|reprogrameaz[ăa])\b",
        lowered,
    ):
        return "reschedule"

    if re.search(
        r"\b(?:rename|modify|edit|update|change\s+the|"
        r"add\s+guest|remove\s+guest|modific[ăa])\b",
        lowered,
    ):
        return "modify"

    return "create"


def main() -> None:
    load_dotenv()
    configure_logging()
    config = load_config()

    print(
        "Calendar Agent — create, list, edit, cancel, or "
        "reschedule events. Type 'help' for examples or 'exit'.\n"
    )

    while True:
        try:
            text = input("You: ").strip()

            if text.casefold() in {
                "exit",
                "quit",
                "ieșire",
                "iesire",
            }:
                print("Agent: Goodbye.")
                break

            if not text:
                continue

            if text.casefold() == "help":
                print(
                    "\nExamples:\n"
                    '  Create a meeting titled "Project Review" next '
                    "Monday at 10 AM for 30 minutes\n"
                    "  Show my events tomorrow\n"
                    '  Reschedule "Project Review" to next Tuesday '
                    "at 3 PM\n"
                    '  Cancel "Project Review"\n'
                    '  Rename "Project Review" to "Client Review"\n'
                    '  Add john@example.com to "Client Review"\n'
                    '  Create a weekly online meeting titled '
                    '"Team Sync" every Monday at 10 AM\n'
                )
                continue

            service = get_calendar_service()
            action = detect_action(text)
            logging.info("Action=%s Input=%s", action, text)

            if action == "list":
                list_events(service, config, text)
                continue

            if action == "cancel":
                title = extract_action_title(text)
                event = choose_event(
                    search_events(
                        service,
                        config.calendar_id,
                        title,
                        config.timezone,
                    ),
                    config.timezone,
                )
                if not event:
                    print("Agent: No matching event was found.\n")
                    continue

                print(
                    f"Agent: About to cancel "
                    f"'{event.get('summary', 'Untitled')}' — "
                    f"{event_date_text(event, config.timezone)}"
                )
                answer = input(
                    "Confirm cancellation? [yes/no]: "
                ).strip().casefold()

                if answer not in YES:
                    print("Agent: Cancellation aborted.\n")
                    continue

                cancel_event(
                    service,
                    config.calendar_id,
                    event,
                )
                logging.info(
                    "Cancelled event id=%s title=%s",
                    event.get("id"),
                    event.get("summary"),
                )
                print("Agent: Event cancelled successfully.\n")
                continue

            if action == "reschedule":
                title = extract_action_title(text)
                event = choose_event(
                    search_events(
                        service,
                        config.calendar_id,
                        title,
                        config.timezone,
                    ),
                    config.timezone,
                )
                if not event:
                    print("Agent: No matching event was found.\n")
                    continue

                new_start, ambiguous, _ = parse_datetime(
                    text,
                    config.timezone,
                )
                if ambiguous:
                    confirm = input(
                        f"I interpreted the date as "
                        f"{new_start:%A, %d %B %Y at %H:%M}. "
                        "Is that correct? [yes/no]: "
                    ).strip().casefold()
                    if confirm not in YES:
                        print(
                            "Agent: Please enter an explicit date.\n"
                        )
                        continue

                answer = input(
                    f"Move '{event.get('summary')}' to "
                    f"{new_start:%d.%m.%Y %H:%M}? [yes/no]: "
                ).strip().casefold()
                if answer not in YES:
                    print("Agent: Rescheduling aborted.\n")
                    continue

                updated = reschedule_event(
                    service,
                    config,
                    event,
                    new_start,
                )
                if updated:
                    logging.info(
                        "Rescheduled event id=%s",
                        event.get("id"),
                    )
                    print(
                        "Agent: Event rescheduled successfully: "
                        f"{updated.get('htmlLink', 'link unavailable')}\n"
                    )
                continue

            if action == "modify":
                title = extract_action_title(text)
                event = choose_event(
                    search_events(
                        service,
                        config.calendar_id,
                        title,
                        config.timezone,
                    ),
                    config.timezone,
                )
                if not event:
                    print("Agent: No matching event was found.\n")
                    continue

                print(
                    f"Agent: About to modify "
                    f"'{event.get('summary', 'Untitled')}' — "
                    f"{event_date_text(event, config.timezone)}"
                )
                answer = input(
                    "Apply the requested changes? [yes/no]: "
                ).strip().casefold()
                if answer not in YES:
                    print("Agent: Modification aborted.\n")
                    continue

                updated = modify_event(
                    service,
                    config,
                    event,
                    text,
                )
                logging.info(
                    "Modified event id=%s",
                    event.get("id"),
                )
                print(
                    "Agent: Event updated successfully: "
                    f"{updated.get('htmlLink', 'link unavailable')}\n"
                )
                continue

            request = parse_create_request(text, config)

            if request.ambiguous_weekday:
                answer = input(
                    f"I interpreted the date as "
                    f"{request.start:%A, %d %B %Y at %H:%M}. "
                    "Is that correct? [yes/no]: "
                ).strip().casefold()
                if answer not in YES:
                    print(
                        "Agent: Please use an explicit date or "
                        "'next Monday'.\n"
                    )
                    continue

            show_request(request)

            resolved = resolve_conflict(
                service,
                config,
                request,
            )
            if resolved is None:
                print("Agent: Event creation cancelled.\n")
                continue

            if resolved != request:
                print("\nAgent: Updated event details:")
                show_request(resolved)

            answer = input(
                "Create the event? [yes/no]: "
            ).strip().casefold()
            if answer not in YES:
                print("Agent: Cancelled.\n")
                continue

            event = create_event(
                service,
                resolved,
                config.calendar_id,
                config.timezone,
            )
            logging.info(
                "Created event id=%s title=%s",
                event.get("id"),
                event.get("summary"),
            )
            print(
                f"Agent: Event created successfully: "
                f"{event.get('summary')}"
            )
            print(
                f"Link: {event.get('htmlLink', 'unavailable')}\n"
            )

        except UserInputError as exc:
            logging.warning("User input error: %s", exc)
            print(f"Agent: I need clarification: {exc}\n")
        except HttpError as exc:
            logging.exception("Google Calendar API error")
            print(
                f"Agent: Google Calendar API error: {exc}\n"
            )
        except (EOFError, KeyboardInterrupt):
            print("\nAgent: Goodbye.")
            break
        except Exception as exc:
            logging.exception("Unhandled error")
            print(
                f"Agent: The operation could not be completed: "
                f"{exc}\n"
            )


if __name__ == "__main__":
    main()
