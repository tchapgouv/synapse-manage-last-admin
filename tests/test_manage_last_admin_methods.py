# -*- coding: utf-8 -*-
# Copyright 2021 The Matrix.org Foundation C.I.C.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# From Python 3.8 onwards, aiounittest.AsyncTestCase can be replaced by
# unittest.IsolatedAsyncioTestCase, so we'll be able to get rid of this dependency when
# we stop supporting Python < 3.8 in Synapse.
from abc import abstractmethod
from typing import Any, Dict

import aiounittest
from synapse.api.constants import EventTypes, Membership
from synapse.api.room_versions import RoomVersions
from synapse.events import EventBase, make_event_from_dict
from synapse.types import JsonDict, MutableStateMap
from synapse.util.stringutils import random_string

from manage_last_admin import ACCESS_RULES_TYPE, _filter_out_users_from_forbidden_domain
from tests import create_module



class TestFilterOutUsersFromForbiddenDomain(aiounittest.AsyncTestCase):

    def test_multiple_forbidden_domains(self):
        """Test filtering with multiple forbidden domains."""
        user_ids = [
            "@user1:domain1.com",
            "@user2:domain2.com",
            "@user3:domain3.com",
            "@user4:domain4.com"
        ]
        forbidden_domains = ["domain1.com", "domain3.com"]
        
        result = _filter_out_users_from_forbidden_domain(user_ids, forbidden_domains)
        self.assertEqual(result, ["@user2:domain2.com", "@user4:domain4.com"])

    def test_empty_list_of_domain(self):
        """Test filtering with empty params"""
        user_ids = ["@user1:domain1.com"]
        forbidden_domains = []
        result = _filter_out_users_from_forbidden_domain(user_ids, forbidden_domains)
        self.assertEqual(result, ["@user1:domain1.com"])