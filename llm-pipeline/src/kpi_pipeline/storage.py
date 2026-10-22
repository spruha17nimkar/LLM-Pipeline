"""Storage backends: local filesystem for dev, S3 in AWS. Same key layout in both.

Key layout:
    filings/raw/{cik}/{accession}.htm         incoming filing (S3 trigger prefix)
    filings/raw/{cik}/{accession}.meta.json   optional metadata sidecar
    xbrl/{cik}.json                           cached companyfacts
    results/staged/{filing_id}.json           extracted, awaiting review
    results/approved/{filing_id}.json         cleared for downstream use
    review/pending/{filing_id}/{item}.json    human-review queue
    review/resolved/{filing_id}/{item}.json
    history/{cik}.json                        approved values, for QoQ trends
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol


class Storage(Protocol):
    def read_text(self, key: str) -> str: ...
    def write_text(self, key: str, text: str) -> None: ...
    def exists(self, key: str) -> bool: ...
    def list(self, prefix: str) -> list[str]: ...
    def delete(self, key: str) -> None: ...


def read_json(store: Storage, key: str, default: Any = None) -> Any:
    if not store.exists(key):
        return default
    return json.loads(store.read_text(key))


def write_json(store: Storage, key: str, obj: Any) -> None:
    store.write_text(key, json.dumps(obj, indent=2, default=str))


class LocalStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _p(self, key: str) -> Path:
        return self.root / key

    def read_text(self, key: str) -> str:
        return self._p(key).read_text(encoding="utf-8")

    def write_text(self, key: str, text: str) -> None:
        p = self._p(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def exists(self, key: str) -> bool:
        return self._p(key).exists()

    def list(self, prefix: str) -> list[str]:
        base = self.root
        if not base.exists():
            return []
        return sorted(
            str(p.relative_to(base)).replace("\\", "/")
            for p in base.rglob("*")
            if p.is_file() and str(p.relative_to(base)).replace("\\", "/").startswith(prefix)
        )

    def delete(self, key: str) -> None:
        p = self._p(key)
        if p.exists():
            p.unlink()


class S3Storage:
    def __init__(self, bucket: str, prefix: str = "", client=None):
        import boto3

        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        self.s3 = client or boto3.client("s3")

    def _k(self, key: str) -> str:
        return self.prefix + key

    def read_text(self, key: str) -> str:
        return self.s3.get_object(Bucket=self.bucket, Key=self._k(key))["Body"].read().decode("utf-8")

    def write_text(self, key: str, text: str) -> None:
        ctype = "application/json" if key.endswith(".json") else "text/html"
        self.s3.put_object(Bucket=self.bucket, Key=self._k(key), Body=text.encode("utf-8"), ContentType=ctype)

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.s3.head_object(Bucket=self.bucket, Key=self._k(key))
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def list(self, prefix: str) -> list[str]:
        keys: list[str] = []
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self._k(prefix)):
            for obj in page.get("Contents", []):
                keys.append(obj["Key"][len(self.prefix):])
        return sorted(keys)

    def delete(self, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=self._k(key))
