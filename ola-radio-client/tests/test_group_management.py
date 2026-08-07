"""Offline tests for the recovered EU group-management request surface."""

import unittest
from unittest.mock import Mock

from rest_client import EuRestApiClient


class GroupManagementRequestTests(unittest.TestCase):
    def setUp(self):
        self.client = EuRestApiClient(token="offline")
        self.client.get = Mock(return_value={"code": 0})
        self.client.post = Mock(return_value={"code": 0})

    def test_create_delete_and_join_payloads(self):
        self.client.create_group("  JC Dream  ")
        self.client.post.assert_called_with(
            "/api/app/group/create", data={"groupName": "JC Dream"}
        )

        self.client.delete_group("16092")
        self.client.post.assert_called_with(
            "/api/app/group/delete", data={"groupId": 16092}
        )

        self.client.join_group(16092)
        self.client.post.assert_called_with(
            "/api/app/group/join", data={"groupId": 16092}
        )

    def test_code_and_member_queries(self):
        self.client.join_group_with_code("  ABC123  ")
        self.client.post.assert_called_with(
            "/api/app/group/join/withcode", data={"inviteCode": "ABC123"}
        )

        self.client.get_group_code(16092)
        self.client.get.assert_called_with(
            "/api/app/group/code", params={"groupId": 16092}
        )

        self.client.list_group_members(16092)
        self.client.get.assert_called_with(
            "/api/app/group/members", params={"groupId": 16092}
        )

    def test_member_change_normalizes_ids_without_inventing_operation_values(self):
        self.client.change_group_members(16092, ["42", 7, 42], " add ")
        self.client.post.assert_called_with(
            "/api/app/group/members/change",
            data={"groupId": 16092, "members": [42, 7], "operation": "add"},
        )

    def test_rename_payload(self):
        self.client.rename_group(16092, "  Radio Lab  ")
        self.client.post.assert_called_with(
            "/api/app/group/name/modify",
            data={"groupId": 16092, "groupName": "Radio Lab"},
        )

    def test_rejects_unsafe_empty_mutations(self):
        for call in (
            lambda: self.client.create_group(" "),
            lambda: self.client.join_group(0),
            lambda: self.client.join_group_with_code(""),
            lambda: self.client.change_group_members(1, [], "add"),
            lambda: self.client.change_group_members(1, [2], ""),
        ):
            with self.subTest(call=call):
                with self.assertRaises(ValueError):
                    call()

    def test_leave_is_explicitly_unimplemented(self):
        with self.assertRaisesRegex(NotImplementedError, "No verified EU REST leave"):
            self.client.leave_group(16092)
        self.client.post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
