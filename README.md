# MACTrack
> [!IMPORTANT]
> This project has no official affiliation with and is not endorsed by McMaster University and any related entities.

## Functionality
MACTrack fetches data from Avenue to Learn, Simple Syllabus, Crowdmark, and Childsmath so that McMaster students have an easy to parse feed of assignments, quizzes, tests, and exams, with their respective due dates, status, weight, grade, and any additional information.

Data is stored locally, and is not sent to any remote server.

One of the ways to view said data is through the [web interface](https://github.com/spirit-of-light/MACTrack-web).

## Installation
`python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`

## Operation
Start the server

`.venv/bin/python server.py`

On the first run a `token.txt` file will be generated.

Load the firefox extension by going to `about:debugging#/runtime/this-firefox` and loading a temporary add-on, picking `extension/manifest.json`. Paste the token from `token.txt` into the extension configuration, and ensure that it has all permissions enabled. Click the extension icon to sync data. The extension also automatically syncs every 3 hours.

Temporary add-ons are removed after firefox restarts, so this process will have to be repeated.

> [!NOTE]
> The extension is a temporary solution, and will be replaced soon.
