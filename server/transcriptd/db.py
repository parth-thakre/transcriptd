"""SQLite storage via SQLModel. Single-user for now; every table carries user_id=1."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Column, Text, event, literal
from sqlalchemy import insert as sa_insert
from sqlalchemy import select as sa_select
from sqlmodel import Field, Session, SQLModel, create_engine, select

from .text import normalize_key

log = logging.getLogger(__name__)

# Clients hold on to these ids after the request that made them (Android posts a take's edit up
# to 90 s later; the agent uploads a meeting's audio when it ends), so a deleted id must never
# come back: plain INTEGER PRIMARY KEY hands the newest deleted id to the next row. Databases
# created before this change keep SQLite's default rowid reuse (no automatic migration).
_NO_REUSE = {"sqlite_autoincrement": True}


def now() -> datetime:
    return datetime.now(UTC)


class VocabTerm(SQLModel, table=True):
    __table_args__ = _NO_REUSE  # a correction holds a term's id across a delete

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = 1
    term: str = Field(index=True)
    aliases_json: str = Field(default="[]", sa_column=Column(Text))
    heard_as_json: str = Field(default="[]", sa_column=Column(Text))
    language: str | None = None
    added_from: str = "manual"  # manual | overlay_edit | correction_hotkey | low_confidence | import
    use_count: int = 0
    created_at: datetime = Field(default_factory=now)

    @property
    def aliases(self) -> list[str]:
        return json.loads(self.aliases_json)

    @aliases.setter
    def aliases(self, v: list[str]) -> None:
        self.aliases_json = json.dumps(v, ensure_ascii=False)

    @property
    def heard_as(self) -> list[str]:
        return json.loads(self.heard_as_json)

    @heard_as.setter
    def heard_as(self, v: list[str]) -> None:
        self.heard_as_json = json.dumps(v, ensure_ascii=False)

    def to_dict(self) -> dict:
        return {"id": self.id, "term": self.term, "aliases": self.aliases, "heard_as": self.heard_as,
                "language": self.language, "added_from": self.added_from, "use_count": self.use_count,
                "created_at": self.created_at.isoformat()}


class SuppressedSuggestion(SQLModel, table=True):
    """Words the user said 'never ask again' for."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = 1
    key: str = Field(index=True)


class Dictation(SQLModel, table=True):
    __table_args__ = _NO_REUSE

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = 1
    created_at: datetime = Field(default_factory=now, index=True)
    profile: str = ""
    language: str | None = None
    raw_text: str = Field(default="", sa_column=Column(Text))
    text: str = Field(default="", sa_column=Column(Text))
    result_json: str = Field(default="{}", sa_column=Column(Text))
    duration_ms: int = 0
    app_hint: str | None = None
    audio_path: str | None = None


class Correction(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    user_id: int = 1
    dictation_id: int | None = Field(default=None, foreign_key="dictation.id")
    heard: str
    meant: str
    source: str = "overlay_edit"
    created_at: datetime = Field(default_factory=now)


class DictationEdit(SQLModel, table=True):
    """What the user did to a dictation after it was inserted: a window of the field's text when
    they were done with it (sent, moved on, or a minute later), and the part of it that was the
    dictation. Deleted with the dictation."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = 1
    dictation_id: int = Field(foreign_key="dictation.id", index=True)
    created_at: datetime = Field(default_factory=now, index=True)
    app: str | None = None
    reason: str | None = None  # sent | focus_lost | replaced | timeout (client-side)
    inserted_text: str = Field(default="", sa_column=Column(Text))
    final_text: str = Field(default="", sa_column=Column(Text))  # a window of the field around the dictation; "" = unchanged
    edited_text: str = Field(default="", sa_column=Column(Text))  # the dictation's part of it
    changed: bool = False


class ClientReport(SQLModel, table=True):
    """Telemetry a client sends about one take: device, mic, timings, how the text went in.
    `dictation_id` is None when the take never reached the server."""

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = 1
    dictation_id: int | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=now, index=True)
    client: str = ""
    data_json: str = Field(default="{}", sa_column=Column(Text))


class Meeting(SQLModel, table=True):
    __table_args__ = _NO_REUSE

    id: int | None = Field(default=None, primary_key=True)
    user_id: int = 1
    title: str = ""
    started_at: datetime = Field(default_factory=now, index=True)
    ended_at: datetime | None = None
    status: str = "recording"  # recording | processing | done | failed
    profile: str = ""
    audio_path: str | None = None
    summary: str | None = Field(default=None, sa_column=Column(Text))
    notes: str = Field(default="", sa_column=Column(Text))


class MeetingSegment(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    meeting_id: int = Field(foreign_key="meeting.id", index=True)
    channel: str  # "me" | "them"
    speaker: str | None = None
    start_ms: int = 0
    end_ms: int = 0
    text: str = Field(default="", sa_column=Column(Text))
    words_json: str = Field(default="[]", sa_column=Column(Text))
    final: bool = False  # False = live pass, True = post-meeting re-pass


def _on_connect(dbapi_conn, _record) -> None:
    # A deleted dictation or meeting is overwritten with zeros, not left in free pages where
    # anyone with the file could read it back.
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA secure_delete = ON")
    cur.close()


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        # Owner-only (0600) before SQLite opens it; its -wal/-shm/-journal files take the same
        # mode. An existing database readable by others is tightened.
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
        if path.stat().st_mode & 0o077:
            path.chmod(0o600)
        self.engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
        event.listen(self.engine, "connect", _on_connect)
        SQLModel.metadata.create_all(self.engine)

    def session(self) -> Session:
        return Session(self.engine)

    def insert_if_exists(self, row: SQLModel, parent: type[SQLModel], parent_id: int) -> int | None:
        """Insert `row` only while `parent` row `parent_id` exists; its new id, or None if that row
        is gone. One INSERT ... SELECT ... WHERE EXISTS, which SQLite runs atomically: the foreign
        keys aren't enforced (no PRAGMA foreign_keys), so this is what keeps a child row from
        outliving a parent deleted at the same moment (the delete must remove the children in the
        same write transaction as the parent)."""
        table = row.__table__
        values = row.model_dump(exclude={"id"})
        cols = [table.c[k] for k in values]
        alive = sa_select(parent.id).where(parent.id == parent_id).exists()
        stmt = sa_insert(table).from_select(
            [c.name for c in cols], sa_select(*[literal(values[c.name], type_=c.type) for c in cols]).where(alive))
        with self.session() as s:
            res = s.exec(stmt)
            new_id = res.lastrowid if res.rowcount == 1 else None
            s.commit()
        return new_id

    @contextmanager
    def write_session(self) -> Iterator[Session]:
        """A session whose reads and writes are one write transaction (BEGIN IMMEDIATE): no
        other writer can change the database between what it reads and what it writes. pysqlite
        only opens a transaction at the first write, so reads before it would see a state another
        request could change before the write lands. The caller commits; closing rolls back."""
        with self.session() as s:
            s.connection().exec_driver_sql("BEGIN IMMEDIATE")
            yield s

    def add_heard_as(self, term_id: int, key: str, heard: str, s: Session | None = None) -> bool:
        """Add `heard` (a normalized key) to the heard_as of vocab term `term_id`, if it is still
        the term with normalized key `key`; False if it is gone. Never creates a term: a
        correction read before a delete must not bring the term back, and an id alone could
        name another term. With `s` (a `write_session`) it joins that transaction, uncommitted."""
        if s is None:
            with self.write_session() as own:
                ok = self.add_heard_as(term_id, key, heard, own)
                own.commit()
            return ok
        t = s.get(VocabTerm, term_id)
        if t is None or normalize_key(t.term) != key:
            return False
        t.heard_as = sorted(set(t.heard_as) | {heard})
        s.add(t)
        s.flush()
        return True

    def vocab_terms(self) -> list[VocabTerm]:
        with self.session() as s:
            return list(s.exec(select(VocabTerm).order_by(VocabTerm.term)).all())

    def suppressed_keys(self) -> set[str]:
        with self.session() as s:
            return {r.key for r in s.exec(select(SuppressedSuggestion)).all()}
