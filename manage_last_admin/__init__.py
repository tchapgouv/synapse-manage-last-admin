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
import copy
import logging
from typing import Any, Dict, Final, Iterable, Optional, Tuple, List

import attr
from synapse.api.constants import EventTypes, Membership
from synapse.api.room_versions import EventFormatVersions, RoomVersion
from synapse.events import EventBase
from synapse.module_api import ModuleApi, UserID
from synapse.types import StateMap
from synapse.util.stringutils import random_string

logger = logging.getLogger(__name__)


class RoomType:
    DIRECT: Final = "DIRECT"
    PUBLIC: Final = "PUBLIC"
    PRIVATE: Final = "PRIVATE"
    EXTERNAL: Final = "EXTERNAL"
    UNKNOWN: Final = "UNKNOWN"


ACCESS_RULES_TYPE = "im.vector.room.access_rules"


class AccessRules:
    RESTRICTED = "restricted"
    UNRESTRICTED = "unrestricted"


@attr.s(auto_attribs=True, frozen=True)
class ManageLastAdminConfig:
    promote_moderators: bool = False
    domains_forbidden_when_restricted: List[str] = []


class ManageLastAdmin:
    def __init__(self, config: ManageLastAdminConfig, api: ModuleApi):
        self._api = api
        self._config = config

        self._api.register_third_party_rules_callbacks(
            check_event_allowed=self.check_event_allowed
        )

    @staticmethod
    def parse_config(config: Dict[str, Any]) -> ManageLastAdminConfig:
        return ManageLastAdminConfig(
            config.get("promote_moderators", False),
            config.get("domains_forbidden_when_restricted", [])
        )

    async def check_event_allowed(
        self,
        event: EventBase,
        state_events: StateMap[EventBase],
    ) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Implements synapse.events.ThirdPartyEventRules.check_event_allowed.

        Checks the event's type and the current rule and calls the right function to
        determine whether the event can be allowed.

        Args:
            event: The event to check.
            state_events: A dict mapping (event type, state key) to state event.
                State events in the room the event originated from.

        Returns:
            True if the event should be allowed, False if it should be rejected. If the
            event should be allowed but with some of its data replaced (or its context
            needs to be recalculated, eg because the state of the room has changed), a
            dictionary might be returned in addition to the boolean.
        """

        # If the event is a leave membership update, check if the last admin is leaving
        # the room
        if (
            event.type == EventTypes.Member
            and event.membership == Membership.LEAVE
            and event.is_state()
        ):
            await self._on_room_leave(event, state_events)

        return True, None

    async def _on_room_leave(
        self,
        event: EventBase,
        state_events: StateMap[EventBase],
    ) -> None:
        """React to a m.room.member event with a "leave" membership.

        Checks if the user leaving the room is the last admin in the room. If so, checks
        if there are users with lower but non-default power levels that can be promoted
        to admins. If so, promotes them to admin if the configuration allows it,
        otherwise change admin rule of the room.

        Args:
            event: The event to check.
            state_events: The current state of the room.
        """
        # Check if the last admin is leaving the room.
        pl_content = _get_power_levels_content_from_state(state_events)
        if pl_content is None:
            return

        last_admin_leaving = _is_last_admin_leaving(event, pl_content, state_events)
        if not last_admin_leaving:
            return

        # If so, search for users to promote if the configuration allows it.
        if self._config.promote_moderators:
            # Look for users to promote.
            users_to_promote = _get_users_with_highest_nondefault_pl(
                pl_content["users"],
                pl_content.get("users_default", 0),
                state_events,
                ignore_user=event.state_key,
            )

            # If we found users to promote, update the power levels event in the room's
            # state.
            if users_to_promote:
                #avoid external users to be promoted
                users_to_promote = _filter_out_users_from_forbidden_domain(
                    users_to_promote, 
                    self._config.domains_forbidden_when_restricted)

                logger.info(
                    "Promoting users to admins in room %s: %s",
                    event.room_id,
                    users_to_promote,
                )
                await self._promote_to_admins(users_to_promote, pl_content, event)
                return

        room_type = _get_room_type(state_events)
        if room_type in [RoomType.PRIVATE, RoomType.PUBLIC]:
            # We make sure to change default permission only on public or private rooms
            # If not, we set the default power level as admin
            logger.info("Make admin as default level in room %s", event.room_id)
            await self._set_room_users_default_to_admin(event, state_events)
        elif room_type in [RoomType.UNKNOWN, RoomType.EXTERNAL]:
            # In case of External or Unknown Room
            # promote all users with default power levels except external users
            users_to_promote = _get_users_with_default_pl(pl_content["users"],
                state_events)
            
            #avoid external users to be promoted
            users_to_promote = _filter_out_users_from_forbidden_domain(
                    users_to_promote, 
                    self._config.domains_forbidden_when_restricted)
            
            logger.info("Make admin all non-external default power level users room %s: %s", event.room_id, ', '.join(users_to_promote))
            await self._promote_to_admins(users_to_promote, pl_content, event)

    async def _set_room_users_default_to_admin(
        self, event: EventBase, state_events: StateMap[EventBase]
    ) -> None:

        current_power_levels = state_events.get((EventTypes.PowerLevels, ""))
        # Make a deep copy of the content so we don't edit the "users" dict from
        # the event that's currently in the room's state.
        power_levels_content = (
            {}
            if current_power_levels is None
            else copy.deepcopy(current_power_levels.content)
        )
        # Send a new power levels event with a similar content to the previous one
        # except users_default is 100 to allow any user to be admin of the room.
        power_levels_content["users_default"] = 100
        # Just to be safe, also delete all users that don't have a power level of
        # 100, in order to prevent anyone from being unable to be admin the room.
        # Julien : I am not why it's needed
        users = {}
        for user, level in power_levels_content["users"].items():
            if level == 100:
                users[user] = level
        power_levels_content["users"] = users
        await self._api.create_and_send_event_into_room(
            {
                "room_id": event.room_id,
                "sender": event.sender,
                "type": EventTypes.PowerLevels,
                "content": power_levels_content,
                "state_key": "",
                **_maybe_get_event_id_dict_for_room_version(
                    event.room_version, self._api.server_name
                ),
            }
        )

    async def _promote_to_admins(
        self,
        users_to_promote: Iterable[str],
        pl_content: Dict[str, Any],
        event: EventBase,
    ) -> None:
        """Promotes a given list of users to admins.

        Args:
            users_to_promote: The users to promote.
            pl_content: The content of the m.room.power_levels event that's currently in
                the room state.
            event: The event we want to use the sender and room_id of to send the new
                power levels event.
        """
        # Make a deep copy of the content so we don't edit the "users" dict from
        # the event that's currently in the room's state.
        new_pl_content = copy.deepcopy(pl_content)
        for user in users_to_promote:
            new_pl_content["users"][user] = pl_content["users"][event.sender]

        try: 
            await self._api.create_and_send_event_into_room(
                {
                    "room_id": event.room_id,
                    "sender": event.sender,
                    "type": EventTypes.PowerLevels,
                    "content": new_pl_content,
                    "state_key": "",
                    **_maybe_get_event_id_dict_for_room_version(
                        event.room_version, self._api.server_name
                    ),
                }
            )
        except Exception as e:  # Catch all other exceptions
            # Generic handling if you don't know the exact type of the exception
            # if users_to_promote list if very very large, we might reach the event size limit of 65kb 
            # see : https://spec.matrix.org/v1.12/client-server-api/#size-limits
            logger.info("Cannot send promote event : %s", e)
 

def _maybe_get_event_id_dict_for_room_version(
    room_version: RoomVersion, server_name: str
) -> Dict[str, str]:
    """If this room version needs it, generate an event id"""
    if room_version.event_format != EventFormatVersions.ROOM_V1_V2:
        return {}

    random_id = random_string(43)
    return {"event_id": "!%s:%s" % (random_id, server_name)}


def _is_room_encrypted(
    state_events: StateMap[EventBase],
) -> bool:
    room_encryption = state_events.get((EventTypes.RoomEncryption, ""))
    if room_encryption:
        return True
    return False


def _get_access_rule_type(
    state_events: StateMap[EventBase],
) -> Optional[Any]:
    """
    TODO : This method is slightly different from this one:
    https://github.com/tchapgouv/synapse-room-access-rules/blob/3ade4c621ed874e2d2c6c9b12c6dd303350639c4/room_access_rules/__init__.py#L962
    """
    access_rule_type_event = state_events.get((ACCESS_RULES_TYPE, ""))
    if (access_rule_type_event is None
            or not hasattr(access_rule_type_event, 'content')
            or "rule" not in access_rule_type_event.content):
        return None
    return access_rule_type_event.content["rule"]


def _is_room_public_or_private(
    state_events: StateMap[EventBase],
) -> bool:
    """Checks if the room is public or private

    Args:
        state_events: The current state of the room, from which we can check the room's type.

    Returns:
        True if this room is public or private otherwise false.
    """
    room_type = _get_room_type(state_events)
    return room_type in [RoomType.PRIVATE, RoomType.PUBLIC]


def _get_room_type(
    state_events: StateMap[EventBase],
) -> str:
    is_room_encrypted = _is_room_encrypted(state_events)
    if not is_room_encrypted:
        return RoomType.PUBLIC
    access_rule_type = _get_access_rule_type(state_events)
    if access_rule_type == AccessRules.RESTRICTED:
        return RoomType.PRIVATE
    if access_rule_type == AccessRules.UNRESTRICTED:
        return RoomType.EXTERNAL
    return RoomType.UNKNOWN


def _is_last_admin_leaving(
    event: EventBase,
    power_level_content: Dict[str, Any],
    state_events: StateMap[EventBase],
) -> bool:
    """Checks if the provided leave event is the last admin in the room leaving it.

    Args:
        event: The leave event to check.
        power_level_content: The content of the power levels event that's currently in
            the room's state.
        state_events: The current state of the room, from which we can check the room's
            member list.

    Returns:
        Whether this event is the last admin leaving the room.
    """
    # Get every admin user defined in the room's state
    admin_users = {
        user
        for user, power_level in power_level_content["users"].items()
        if power_level >= 100
    }

    if event.sender not in admin_users:
        # This user is not an admin, ignore them
        return False

    if any(
        event_type == EventTypes.Member
        and state_event.membership in [Membership.JOIN, Membership.INVITE]
        and state_key in admin_users
        and state_key != event.sender
        for (event_type, state_key), state_event in state_events.items()
    ):
        # There's another admin user in, or invited to, the room
        return False

    return True


def _get_power_levels_content_from_state(
    state_events: StateMap[EventBase],
) -> Optional[Dict[str, Any]]:
    """Extracts the content of the power levels content from the provided set of state
    events. If the event has no "users" key, or there is no power levels event in the
    state of the room, None is returned instead.

    Args:
        state_events: The state events to extract power levels from.

    Returns:
        A dict representing the content of the power levels event, or None if no power
        levels event exist in the given state events or if one exists but its content is
        missing a "users" key.
    """
    power_level_state_event = state_events.get((EventTypes.PowerLevels, ""))
    if power_level_state_event is None:
        return None
    power_level_content = power_level_state_event.content

    # Do some validation checks on the power level state event
    if (
        not isinstance(power_level_content, dict)
        or "users" not in power_level_content
        or not isinstance(power_level_content["users"], dict)
    ):
        # We can't use this power level event to determine whether the room should be
        # frozen. Bail out.
        return None

    return power_level_content

def _get_members_in_room_from_state_events(state_events: StateMap[EventBase]
)-> list[str]: 
    members = []
    for (event_type, state_key), state_event in state_events.items():
        if (
            event_type == EventTypes.Member
            and state_event.membership == Membership.JOIN
            and state_event.is_state()
        ):
            members.append(state_key)  # state_key is the user ID
    return members


def _get_users_with_default_pl(
    users_pl_dict: Dict[str, Any],
    state_events: StateMap[EventBase]
)->  Iterable[str]:
    # Make a copy of the users dict so we don't modify the actual event content.
    users_dict_copy = users_pl_dict.copy()

    # If there's no more user to evaluate, return an empty tuple.
    if not users_dict_copy:
        return []
    
    # Figure out which users in still in the room :
    members_in_room = _get_members_in_room_from_state_events(state_events)

    users_with_default_pl = [user for user in members_in_room if user not in users_pl_dict]
    
    return users_with_default_pl


def _get_users_with_highest_nondefault_pl(
    users_dict: Dict[str, Any],
    users_default_pl: int,
    state_events: StateMap[EventBase],
    ignore_user: str,
) -> Iterable[str]:
    """Looks at the provided bits of power levels event content to figure out what the
    maximum user-specific non-default power level is with users still in the room (or
    invited to it) and which users have it.

    Args:
        users_dict: The "users" dictionary from the power levels event content.
        users_default_pl: The default power level for users who don't appear in the users
            dictionary.
        state_events: The current state of the room, from which we can check the room's
            member list.
        ignore_user: A user to ignore, i.e. to consider they've left the room even if the
            room's state says otherwise.

    Returns:
        A tuple of users with the highest non-default power level, or an empty set if no
        such users exist in the room.
    """
    # Make a copy of the users dict so we don't modify the actual event content.
    users_dict_copy = users_dict.copy()

    if ignore_user in users_dict_copy:
        del users_dict_copy[ignore_user]

    while True:
        # If there's no more user to evaluate, return an empty tuple.
        if not users_dict_copy:
            return []

        # Get the max power level in the dict.
        max_pl = max(users_dict_copy.values())

        # Bail out if the max power level is the default one (or is lower).
        if max_pl <= users_default_pl:
            return []

        # Figure out which users have that maximum power level.
        users_with_max_pl = [
            user_id for user_id, pl in users_dict_copy.items() if pl == max_pl
        ]

        # Among those users, figure out which ones are still in the room (or have a
        # pending invite to it): those are the users we need to promote.
        users_to_promote = [
            user_id
            for user_id in users_with_max_pl
            if (
                _get_membership(user_id, state_events)
                in [Membership.JOIN, Membership.INVITE]
            )
        ]

        # If we've got users in the room to promote, break out and return.
        if users_to_promote:
            return users_to_promote

        # Otherwise, remove the users we've considered and start again.
        for user_id in users_with_max_pl:
            del users_dict_copy[user_id]


def _get_membership(
    user_id: str,
    state_events: StateMap[EventBase],
) -> Optional[str]:
    evt: Optional[EventBase] = state_events.get((EventTypes.Member, user_id))

    if evt is None:
        return None

    return evt.membership

def _filter_out_users_from_forbidden_domain(user_ids: Iterable[str], forbidden_domains: List[str]
) -> List[str]:
    """Filters out any users that belong to forbidden domains.

    Args:
        user_ids: An iterable of user IDs to filter.
        forbidden_domains: A list of domain names that are forbidden.

    Returns:
        A list of user IDs with users from forbidden domains filtered out.
    """
    if user_ids is None:
        return None
    
    return [user_id for user_id in user_ids if UserID.from_string(user_id).domain not in forbidden_domains]
