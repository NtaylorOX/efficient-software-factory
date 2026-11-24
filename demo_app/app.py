"""Notes board: the demo project the factory works on. Small on purpose, so a diff fits on one screen."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Notes board")


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=280)


class Note(NoteIn):
    id: int


NOTES: list[Note] = [
    Note(id=1, text="Buy milk and eggs"),
    Note(id=2, text="Book dentist appointment"),
    Note(id=3, text="Read the factory design doc"),
]


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/notes")
def list_notes() -> list[Note]:
    return NOTES


@app.post("/api/notes", status_code=201)
def add_note(note: NoteIn) -> Note:
    created = Note(id=max((n.id for n in NOTES), default=0) + 1, text=note.text)
    NOTES.append(created)
    return created
