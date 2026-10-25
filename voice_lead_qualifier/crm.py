"""CRM boundary.

`CRMClient` is the small interface the pipeline needs. `MockCRM` records every
write in memory so tests can assert exactly what would reach GoHighLevel,
HubSpot, Airtable or another CRM. A real adapter implements the same five
methods with the CRM's API and keeps credentials in environment variables.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


class CRMClient(Protocol):
    def upsert_contact(self, lead_id: str, fields: dict) -> None: ...
    def add_note(self, lead_id: str, note: str) -> None: ...
    def set_tags(self, lead_id: str, tags: list[str]) -> None: ...
    def create_task(self, lead_id: str, title: str, due: datetime) -> None: ...
    def has_processed(self, call_id: str) -> bool: ...
    def mark_processed(self, call_id: str) -> None: ...


@dataclass
class MockCRM:
    contacts: dict[str, dict] = field(default_factory=dict)
    notes: dict[str, list[str]] = field(default_factory=dict)
    tags: dict[str, list[str]] = field(default_factory=dict)
    tasks: list[tuple[str, str, datetime]] = field(default_factory=list)
    processed_calls: set[str] = field(default_factory=set)
    write_log: list[str] = field(default_factory=list)

    def upsert_contact(self, lead_id: str, fields: dict) -> None:
        self.contacts.setdefault(lead_id, {}).update(fields)
        self.write_log.append(f"upsert:{lead_id}")

    def add_note(self, lead_id: str, note: str) -> None:
        self.notes.setdefault(lead_id, []).append(note)
        self.write_log.append(f"note:{lead_id}")

    def set_tags(self, lead_id: str, tags: list[str]) -> None:
        self.tags[lead_id] = sorted(set(tags))
        self.write_log.append(f"tags:{lead_id}")

    def create_task(self, lead_id: str, title: str, due: datetime) -> None:
        self.tasks.append((lead_id, title, due))
        self.write_log.append(f"task:{lead_id}")

    def has_processed(self, call_id: str) -> bool:
        return call_id in self.processed_calls

    def mark_processed(self, call_id: str) -> None:
        self.processed_calls.add(call_id)
