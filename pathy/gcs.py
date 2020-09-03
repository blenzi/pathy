from dataclasses import dataclass
from typing import Generator, List, Optional

from .base import PurePathy
from .client import Blob, Bucket, BucketClient, BucketEntry, ClientError

try:
    from google.api_core import exceptions as gcs_errors
    from google.auth.exceptions import DefaultCredentialsError
    from google.cloud import storage

    has_gcs = True
except ImportError:
    storage = None
    has_gcs = False


class BucketEntryGCS(BucketEntry["BucketGCS", "storage.Blob"]):
    ...


@dataclass
class BlobGCS(Blob["BucketGCS", "storage.Blob"]):
    def delete(self) -> None:
        self.raw.delete()

    def exists(self) -> bool:
        return self.raw.exists()


@dataclass
class BucketGCS(Bucket):
    name: str
    bucket: "storage.Bucket"

    def get_blob(self, blob_name: str) -> Optional[BlobGCS]:
        assert isinstance(
            blob_name, str
        ), f"expected str blob name, but found: {type(blob_name)}"
        native_blob = None
        try:
            native_blob = self.bucket.get_blob(blob_name)
        except gcs_errors.ClientError:
            pass
        if native_blob is None:
            return None
        return BlobGCS(
            bucket=self.bucket,
            owner=native_blob.owner,
            name=native_blob.name,
            raw=native_blob,
            size=native_blob.size,
            updated=native_blob.updated.timestamp(),
        )

    def copy_blob(
        self, blob: BlobGCS, target: "BucketGCS", name: str
    ) -> Optional[BlobGCS]:
        assert blob.raw is not None, "raw storage.Blob instance required"
        native_blob = self.bucket.copy_blob(blob.raw, target.bucket, name)
        if native_blob is None:
            return None
        return BlobGCS(
            bucket=self.bucket,
            owner=native_blob.owner,
            name=native_blob.name,
            raw=native_blob,
            size=native_blob.size,
            updated=native_blob.updated.timestamp(),
        )

    def delete_blob(self, blob: BlobGCS) -> None:
        return self.bucket.delete_blob(blob.name)

    def delete_blobs(self, blobs: List[BlobGCS]) -> None:
        return self.bucket.delete_blobs(blobs)

    def exists(self) -> bool:
        try:
            return self.bucket.exists()
        except gcs_errors.ClientError:
            return False


class BucketClientGCS(BucketClient):
    client: Optional["storage.Client"]

    def __init__(self, client: Optional["storage.Client"] = None):
        try:
            self.client = storage.Client() if storage else None
        except (BaseException, DefaultCredentialsError):
            self.client = None

    def make_uri(self, path: PurePathy) -> str:
        return str(path)

    def create_bucket(self, path: PurePathy) -> Bucket:
        assert self.client is not None
        return self.client.create_bucket(path.root)

    def delete_bucket(self, path: PurePathy) -> None:
        assert self.client is not None
        bucket = self.client.get_bucket(path.root)
        bucket.delete()

    def exists(self, path: PurePathy) -> bool:
        # Because we want all the parents of a valid blob (e.g. "directory" in
        # "directory/foo.file") to return True, we enumerate the blobs with a prefix
        # and compare the object names to see if they match a substring of the path
        key_name = str(path.key)
        try:
            for obj in self.list_blobs(path):
                if obj.name == key_name:
                    return True
                if obj.name.startswith(key_name + path._flavour.sep):
                    return True
        except gcs_errors.ClientError:
            return False
        return False

    def lookup_bucket(self, path: PurePathy) -> Optional[BucketGCS]:
        assert self.client is not None
        try:
            native_bucket = self.client.bucket(path.root)
            if native_bucket is not None:
                return BucketGCS(str(path.root), bucket=native_bucket)
        except gcs_errors.ClientError as err:
            print(err)

        return None

    def get_bucket(self, path: PurePathy) -> BucketGCS:
        assert self.client is not None
        try:
            native_bucket = self.client.bucket(path.root)
            if native_bucket is not None:
                return BucketGCS(str(path.root), bucket=native_bucket)
            raise FileNotFoundError(f"Bucket {path.root} does not exist!")
        except gcs_errors.ClientError as e:
            raise ClientError(message=e.message, code=e.code)

    def list_buckets(self, **kwargs) -> Generator[Bucket, None, None]:
        assert self.client is not None
        return self.client.list_buckets(**kwargs)

    def scandir(
        self,
        path: Optional[PurePathy] = None,
        prefix: Optional[str] = None,
        delimiter: Optional[str] = None,
        include_raw: bool = False,
    ) -> Generator[BucketEntryGCS, None, None]:
        assert self.client is not None
        continuation_token = None
        if path is None or not path.root:
            for bucket in self.list_buckets():
                yield BucketEntryGCS(bucket.name, is_dir=True, raw=None)
            return
        sep = path._flavour.sep
        bucket = self.lookup_bucket(path)
        if bucket is None:
            return
        while True:
            if continuation_token:
                response = self.client.list_blobs(
                    bucket.name,
                    prefix=prefix,
                    delimiter=sep,
                    page_token=continuation_token,
                )
            else:
                response = self.client.list_blobs(
                    bucket.name, prefix=prefix, delimiter=sep
                )
            for page in response.pages:
                for folder in list(page.prefixes):
                    full_name = folder[:-1] if folder.endswith(sep) else folder
                    name = full_name.split(sep)[-1]
                    if name:
                        yield BucketEntryGCS(name, is_dir=True, raw=None)
                for item in page:
                    name = item.name.split(sep)[-1]
                    if name:
                        yield BucketEntryGCS(
                            name=name,
                            is_dir=False,
                            size=item.size,
                            last_modified=item.updated.timestamp(),
                            raw=item,
                        )
            if response.next_page_token is None:
                break
            continuation_token = response.next_page_token

    def list_blobs(
        self,
        path: PurePathy,
        prefix: Optional[str] = None,
        delimiter: Optional[str] = None,
        include_dirs: bool = False,
    ) -> Generator[BlobGCS, None, None]:
        assert self.client is not None
        continuation_token = None
        bucket = self.lookup_bucket(path)
        if bucket is None:
            return
        while True:
            if continuation_token:
                response = self.client.list_blobs(
                    path.root,
                    prefix=prefix,
                    delimiter=delimiter,
                    page_token=continuation_token,
                )
            else:
                response = self.client.list_blobs(
                    path.root, prefix=prefix, delimiter=delimiter
                )
            for page in response.pages:
                for item in page:
                    yield BlobGCS(
                        bucket=bucket,
                        owner=item.owner,
                        name=item.name,
                        raw=item,
                        size=item.size,
                        updated=item.updated.timestamp(),
                    )
            if response.next_page_token is None:
                break
            continuation_token = response.next_page_token
