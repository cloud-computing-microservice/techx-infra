"""Persist positive storefront creation evidence before a cleanup DELETE.

An empty API read cannot resolve a lost create response. Conversely, a lost
delete response is recoverable after this immutable observation is committed.
"""
import re

from cloudflare_dns import HOST, validate_shop_intent
from cleanup_execution import remove_owned_shop
from plan_approval import version
from plan_review_store import checksum, immutable_json, read_json


def cleanup_shop_dns(s3, bucket, session, private, client):
    intent = validate_shop_intent(private["shopDnsIntent"])
    if intent["phase"] != "PREPARED" and private.get("dnsMutationAttempted") is not True:
        raise ValueError("Storefront mutation intent is inconsistent with its private snapshot")
    context = {key: value for key, value in intent.items() if key not in ("phase", "id")}
    identity = dict(schemaVersion=1, sessionId=session, intentSha256=checksum(context))
    key = f"cleanup/{session}/shop-dns-observed.json"
    observed = None
    try:
        proof, object_version = read_json(s3, bucket, key)
    except s3.exceptions.NoSuchKey:
        pass
    else:
        version(object_version)
        if (not isinstance(proof, dict) or set(proof) != set(identity) | {"ownership"}
                or any(proof.get(k) != v for k, v in identity.items())):
            raise ValueError("Storefront cleanup observation belongs to another intent")
        observed = proof["ownership"]
        if (not isinstance(observed, dict) or set(observed) != {"id", "name", "content", "sessionId"}
                or observed.get("name") != HOST or observed.get("content") != intent["target"]
                or observed.get("sessionId") != session or not re.fullmatch(r"[a-f0-9]{32}", observed.get("id", ""))
                or intent["phase"] == "OBSERVED" and observed["id"] != intent["id"]):
            raise ValueError("Invalid positive storefront DNS creation proof")

    def persist(ownership):
        ref = immutable_json(s3, bucket, key, dict(identity, ownership=ownership))
        written, _ = read_json(s3, bucket, key, ref["versionId"])
        if written != dict(identity, ownership=ownership): raise ValueError("DNS creation proof was not persisted")

    remove_owned_shop(client, session, intent=intent, observed=observed, persist_observation=persist)
