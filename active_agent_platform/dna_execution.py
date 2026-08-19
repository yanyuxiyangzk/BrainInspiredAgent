"""Governed Organization-to-Agent-to-Workflow DNA execution identities."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from active_agent_platform.storage import SQLiteDatabase
from active_agent_platform.workflow import WorkflowValidator


class DnaExecutionError(ValueError):
    """A governed DNA identity or delegation cannot be admitted."""


@dataclass(frozen=True, slots=True)
class DnaIdentity:
    dna_id: str
    version: str
    content_digest: str

    def __post_init__(self) -> None:
        if (not self.dna_id or re.fullmatch(r"\d+\.\d+\.\d+", self.version) is None
                or re.fullmatch(r"sha256:[0-9a-f]{64}", self.content_digest) is None):
            raise DnaExecutionError("DNA execution identity is invalid")

    def to_document(self) -> dict[str, str]:
        return {"dna_id": self.dna_id, "version": self.version,
                "content_digest": self.content_digest}


@dataclass(frozen=True, slots=True)
class DnaExecutionIdentity:
    organization: DnaIdentity
    organization_role: str
    agent: DnaIdentity
    workflow: DnaIdentity

    def __post_init__(self) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_]{1,63}", self.organization_role) is None:
            raise DnaExecutionError("Organization role is invalid")

    def to_document(self) -> dict[str, object]:
        return {"organization": self.organization.to_document(),
                "organization_role": self.organization_role,
                "agent": self.agent.to_document(), "workflow": self.workflow.to_document()}

    @property
    def digest(self) -> str:
        encoded = json.dumps(self.to_document(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()


async def verify_execution_identity(database: SQLiteDatabase, identity: DnaExecutionIdentity,
                                    *, workflow_id: str, workflow_version: str,
                                    workflow_digest: str) -> None:
    """Rebuild every reference from durable facts; caller strings are never trusted."""
    org_row = await database.fetch_one(
        "SELECT document_json,status,content_digest FROM organization_dna_definition "
        "WHERE dna_id=? AND version=?", (identity.organization.dna_id, identity.organization.version),
    )
    agent_row = await database.fetch_one(
        "SELECT document_json,status,content_digest FROM agent_dna_definition "
        "WHERE dna_id=? AND version=?", (identity.agent.dna_id, identity.agent.version),
    )
    workflow_row = await database.fetch_one(
        "SELECT document_json,status,content_digest FROM dna_definition WHERE dna_id=? AND version=?",
        (identity.workflow.dna_id, identity.workflow.version),
    )
    if org_row is None or agent_row is None or workflow_row is None:
        raise DnaExecutionError("DNA execution identity references missing durable facts")
    if any(str(row["status"]) != "ACTIVE" for row in (org_row, agent_row, workflow_row)):
        raise DnaExecutionError("DNA execution identity requires active versions")
    actual = (str(org_row["content_digest"]), str(agent_row["content_digest"]),
              str(workflow_row["content_digest"]))
    expected = (identity.organization.content_digest, identity.agent.content_digest,
                identity.workflow.content_digest)
    if actual != expected:
        raise DnaExecutionError("DNA execution identity digest mismatch")
    organization = json.loads(str(org_row["document_json"]))
    agent = json.loads(str(agent_row["document_json"]))
    workflow = json.loads(str(workflow_row["document_json"]))
    for document, digest in zip((organization, agent, workflow), actual, strict=True):
        if document.get("content_digest") != digest or _content_digest(document) != digest:
            raise DnaExecutionError("stored DNA execution document digest mismatch")
    members = [item for item in organization["members"]
               if item["role"] == identity.organization_role]
    if len(members) != 1 or any(members[0][key] != value for key, value in (
        ("agent_dna_id", identity.agent.dna_id), ("agent_version", identity.agent.version),
        ("agent_content_digest", identity.agent.content_digest),
    )):
        raise DnaExecutionError("Organization role does not bind the supplied Agent DNA")
    if not any(ref["dna_id"] == identity.workflow.dna_id
               and ref["version"] == identity.workflow.version
               and ref["content_digest"] == identity.workflow.content_digest
               for ref in agent["workflow_dna"]):
        raise DnaExecutionError("Agent DNA does not bind the supplied Workflow DNA")
    validation = WorkflowValidator().validate(workflow["workflow"])
    if ((validation.workflow_id, validation.version, validation.digest)
            != (workflow_id, workflow_version, workflow_digest)):
        raise DnaExecutionError("planned Workflow does not match the frozen Workflow DNA")


def _content_digest(document: dict[str, object]) -> str:
    kind = str(document.get("kind"))
    field = {"ORGANIZATION": "members", "AGENT": "workflow_dna", "WORKFLOW": "workflow"}.get(kind)
    if field is None:
        raise DnaExecutionError("stored DNA execution document kind is unsupported")
    content = {key: document[key] for key in ("dna_spec_version", "dna_id", "version", "kind")}
    if kind in {"ORGANIZATION", "AGENT"}:
        content["profile"] = document["profile"]
    content[field] = document[field]
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode()).hexdigest()
