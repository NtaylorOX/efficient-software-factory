# notes

The notes board in `demo_app/`: a list of short notes and a form to add one.

This is a living spec. The factory changes it only through a change folder in `changes/`, in the same pull
request as the code, so merging the PR is what makes a requirement true.

## Requirements

### Requirement: List notes
The board SHALL show every note, oldest first, at `GET /api/notes` and on the page.

#### Scenario: Seed notes are listed
- **WHEN** a client requests `GET /api/notes` on a fresh board
- **THEN** the three seed notes are returned in order

### Requirement: Add a note
The board SHALL accept a note of 1 to 280 characters at `POST /api/notes` and reject anything else with 422.

#### Scenario: Empty note is rejected
- **WHEN** a client posts a note with empty text
- **THEN** the response status is 422 and no note is added
