"""
IdentityMap — persisted old→new identity mapping for the run.

STUB ONLY (see PLAN.md §7). Serialised to identity_map.json in the run dir; consumed by
ACL/job remap during 04_Import.

Structure:
  sp_mapping     : {old_application_id: new_application_id}   # Databricks-managed SPs
  group_map      : {old_group_id: new_group_id}
  user_map       : {source_userName: target_userName}        # mostly identity
  manual_actions : [ {type, subject, reason, instructions} ]  # account-admin / customer IT
"""
from __future__ import annotations


class IdentityMap:
    def __init__(self) -> None:
        self.sp_mapping: dict = {}
        self.group_map: dict = {}
        self.user_map: dict = {}
        self.manual_actions: list = []

    def add_manual_action(self, type_: str, subject: str, reason: str, instructions: str) -> None:
        raise NotImplementedError

    def remap_principal(self, principal_id: str) -> str:
        """Return the target id for a source principal id (sp/group/user), or itself if stable."""
        raise NotImplementedError

    def to_json(self) -> dict:
        raise NotImplementedError

    @classmethod
    def from_json(cls, d: dict) -> "IdentityMap":
        raise NotImplementedError
