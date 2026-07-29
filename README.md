# Calendar Management Agent

A Python agent that manages Google Calendar through natural-language commands.

## Features

- Create meetings and reminders
- List events for today, tomorrow, next week, or a weekday
- Cancel events
- Reschedule events
- Rename events
- Change duration
- Add or remove guests
- Add a location and description
- Create Google Meet links
- Create recurring events
- Validate invalid times such as `25:00`
- Confirm ambiguous dates such as `Monday`
- Check calendar conflicts
- Check guest availability when permissions allow it
- Suggest three alternative time slots
- Respect configured working hours
- Write operations and errors to `calendar_agent.log`

## Google Cloud setup

1. Create a Google Cloud project.
2. Enable Google Calendar API.
3. Configure Google Auth Platform as External and add your Gmail address as a test user.
4. Create an OAuth Client ID of type Desktop app.
5. Download the JSON file.
6. Rename it to `credentials.json`.
7. Put it beside `main.py`.

## Installation on Windows

Open the project folder in Visual Studio Code, then run:

```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
python main.py
```

If you previously used an older version of this agent, delete `token.json` once.
The browser will open again so Google can authorize the new permissions.

## Important files

- `main.py` — application
- `.env.example` — safe configuration template
- `requirements.txt` — Python packages
- `.gitignore` — prevents secrets from being uploaded to Git
- `tests/test_basic.py` — basic parser tests
- `credentials.json` — you must add this file yourself
- `token.json` — generated automatically after authentication

## Example commands

```text
Create a meeting titled "Project Review" next Monday at 10 AM for 30 minutes
Create a reminder titled "Submit report" tomorrow at 5 PM
Show my events tomorrow
List my events next week
Cancel "Project Review"
Reschedule "Project Review" to next Tuesday at 3 PM
Rename "Project Review" to "Client Review"
Add john@example.com to "Client Review"
Remove john@example.com from "Client Review"
Create an online meeting titled "Weekly Sync" every Monday at 10 AM
Create a meeting titled "London Call" tomorrow at 3 PM in London time
Create a meeting titled "Sprint Planning" next Friday at 9 AM location "Room A" description "Prepare the next sprint"
```

## Running tests

```powershell
pytest
```

## Security

Never upload these files to GitHub:

```text
.env
credentials.json
token.json
```
