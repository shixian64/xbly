"""Shared durable authorization query for one-to-one Web messaging.

The query is intentionally independent from FastAPI and the legacy protocol
runtime so text, media and future local transports can enforce the same rule
inside the transaction that creates a canonical message.
"""

from __future__ import annotations

import uuid

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import aliased

from bbw_prod.models import Conversation, Relationship


LEGACY_RELATIONSHIP_PROVIDER = "beibeiwu"
LOCAL_RELATIONSHIP_PROVIDER = "web-local"
MESSAGE_POLICY_PROVIDER = "web-policy"
MESSAGE_POLICY_MATCH_KIND = "match"
MESSAGE_POLICY_CONVERSATION_KIND = "message_peer"
FRIEND_KIND = "friend"
BLACKLIST_KIND = "blacklist"
BLACKLISTED_BY_KIND = "blacklisted_by"


def private_message_permission_query(
    *,
    sender_user_id: uuid.UUID,
    sender_upstream_uid: str,
    recipient_user_id: object,
    recipient_upstream_uid: str,
    proactive_private_message: bool,
):
    """Return a scalar SELECT that authorizes one local private-message send.

    Local relationship rows are authoritative overrides, including inactive
    tombstones.  Legacy rows are accepted only while no corresponding local
    override exists.  A server-owned match/history grant, an effective friend
    relationship or an already-authorized Web-local conversation permits the
    send.  The administrator capability may bypass the grant requirement but
    never either direction of the blacklist.
    """

    own_block_override = aliased(Relationship, name="policy_own_block_override")
    peer_block_override = aliased(Relationship, name="policy_peer_block_override")
    own_has_local_block = (
        select(own_block_override.id)
        .where(
            own_block_override.owner_user_id == sender_user_id,
            own_block_override.provider == LOCAL_RELATIONSHIP_PROVIDER,
            own_block_override.subject_upstream_uid == recipient_upstream_uid,
            own_block_override.kind == BLACKLIST_KIND,
        )
        .exists()
    )
    peer_has_local_block = (
        select(peer_block_override.id)
        .where(
            peer_block_override.owner_user_id == recipient_user_id,
            peer_block_override.provider == LOCAL_RELATIONSHIP_PROVIDER,
            peer_block_override.subject_upstream_uid == sender_upstream_uid,
            peer_block_override.kind == BLACKLIST_KIND,
        )
        .exists()
    )
    blocked_exists = (
        select(Relationship.id)
        .where(
            Relationship.provider.in_(
                (LEGACY_RELATIONSHIP_PROVIDER, LOCAL_RELATIONSHIP_PROVIDER)
            ),
            Relationship.status == "active",
            Relationship.ended_at.is_(None),
            or_(
                and_(
                    Relationship.owner_user_id == sender_user_id,
                    Relationship.provider == LOCAL_RELATIONSHIP_PROVIDER,
                    Relationship.subject_upstream_uid == recipient_upstream_uid,
                    Relationship.kind == BLACKLIST_KIND,
                ),
                and_(
                    Relationship.owner_user_id == recipient_user_id,
                    Relationship.provider == LOCAL_RELATIONSHIP_PROVIDER,
                    Relationship.subject_upstream_uid == sender_upstream_uid,
                    Relationship.kind == BLACKLIST_KIND,
                ),
                and_(
                    Relationship.owner_user_id == sender_user_id,
                    Relationship.provider == LEGACY_RELATIONSHIP_PROVIDER,
                    Relationship.subject_upstream_uid == recipient_upstream_uid,
                    Relationship.kind == BLACKLIST_KIND,
                    ~own_has_local_block,
                ),
                and_(
                    Relationship.owner_user_id == sender_user_id,
                    Relationship.provider == LEGACY_RELATIONSHIP_PROVIDER,
                    Relationship.subject_upstream_uid == recipient_upstream_uid,
                    Relationship.kind == BLACKLISTED_BY_KIND,
                    ~peer_has_local_block,
                ),
                # 收件人名下的 legacy 快照同样是「任一方向」的一部分：
                # 收件人拉黑发送人（blacklist），或收件人快照记录了发送人
                # 拉黑收件人（blacklisted_by）。与 messaging.block_between_query
                # 的双向语义保持一致，local override 豁免方向也相同。
                and_(
                    Relationship.owner_user_id == recipient_user_id,
                    Relationship.provider == LEGACY_RELATIONSHIP_PROVIDER,
                    Relationship.subject_upstream_uid == sender_upstream_uid,
                    Relationship.kind == BLACKLIST_KIND,
                    ~peer_has_local_block,
                ),
                and_(
                    Relationship.owner_user_id == recipient_user_id,
                    Relationship.provider == LEGACY_RELATIONSHIP_PROVIDER,
                    Relationship.subject_upstream_uid == sender_upstream_uid,
                    Relationship.kind == BLACKLISTED_BY_KIND,
                    ~own_has_local_block,
                ),
            ),
        )
        .exists()
    )
    if proactive_private_message:
        return select(~blocked_exists)

    friend_override = aliased(Relationship, name="policy_friend_override")
    peer_friend_override = aliased(
        Relationship, name="policy_peer_friend_override"
    )
    local_friend_override = (
        select(friend_override.id)
        .where(
            friend_override.owner_user_id == sender_user_id,
            friend_override.provider == LOCAL_RELATIONSHIP_PROVIDER,
            friend_override.subject_upstream_uid == recipient_upstream_uid,
            friend_override.kind == FRIEND_KIND,
        )
        .exists()
    )
    peer_friend_tombstone = (
        select(peer_friend_override.id)
        .where(
            peer_friend_override.owner_user_id == recipient_user_id,
            peer_friend_override.provider == LOCAL_RELATIONSHIP_PROVIDER,
            peer_friend_override.subject_upstream_uid == sender_upstream_uid,
            peer_friend_override.kind == FRIEND_KIND,
            or_(
                peer_friend_override.status != "active",
                peer_friend_override.ended_at.is_not(None),
            ),
        )
        .exists()
    )
    grant_exists = (
        select(Relationship.id)
        .where(
            or_(
                and_(
                    Relationship.owner_user_id == sender_user_id,
                    Relationship.subject_upstream_uid == recipient_upstream_uid,
                    Relationship.provider == MESSAGE_POLICY_PROVIDER,
                    Relationship.kind.in_(
                        (
                            MESSAGE_POLICY_MATCH_KIND,
                            MESSAGE_POLICY_CONVERSATION_KIND,
                        )
                    ),
                ),
                and_(
                    Relationship.owner_user_id == sender_user_id,
                    Relationship.subject_upstream_uid == recipient_upstream_uid,
                    Relationship.kind == FRIEND_KIND,
                    Relationship.provider == LOCAL_RELATIONSHIP_PROVIDER,
                    ~peer_friend_tombstone,
                ),
                and_(
                    Relationship.provider == LEGACY_RELATIONSHIP_PROVIDER,
                    Relationship.kind == FRIEND_KIND,
                    ~local_friend_override,
                    ~peer_friend_tombstone,
                    or_(
                        and_(
                            Relationship.owner_user_id == sender_user_id,
                            Relationship.subject_upstream_uid
                            == recipient_upstream_uid,
                        ),
                        and_(
                            Relationship.owner_user_id == recipient_user_id,
                            Relationship.subject_upstream_uid
                            == sender_upstream_uid,
                        ),
                    ),
                ),
            ),
            Relationship.status == "active",
            Relationship.ended_at.is_(None),
        )
        .exists()
    )
    conversation_exists = (
        select(Conversation.id)
        .where(
            Conversation.owner_user_id == sender_user_id,
            Conversation.provider == LOCAL_RELATIONSHIP_PROVIDER,
            Conversation.peer_upstream_uid == recipient_upstream_uid,
            Conversation.kind == "direct",
        )
        .exists()
    )
    return select(and_(~blocked_exists, or_(grant_exists, conversation_exists)))
