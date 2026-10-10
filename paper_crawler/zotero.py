from __future__ import annotations

import hashlib
import mimetypes
import secrets
from pathlib import Path
from typing import Any

import requests

from .models import Paper, ZoteroConfig
from .budget import RunBudget


class ZoteroError(RuntimeError):
    pass


class ZoteroClient:
    def __init__(self, config: ZoteroConfig, *, budget=None):
        self.config = config
        self.budget = budget or RunBudget()
        self.base_url = config.base_url.rstrip("/") + "/api/"
        self.prefix = config.user_or_group.strip("/") + "/"
        self.session = requests.Session()
        self.session.headers.update({"Zotero-API-Version": "3"})
        self.server_id = None
        if config.api_key:
            self.session.headers["Zotero-API-Key"] = config.api_key

    def _url(self, path: str) -> str:
        return self.base_url + path.lstrip("/")

    def connect(self) -> None:
        if self.server_id:
            return
        with self.session.get(self._url(""), timeout=self.budget.timeout(30)) as response:
            response.raise_for_status()
            server_id = response.headers.get("Zotero-Server-ID")
        if not server_id:
            raise ZoteroError("Local writes require Zotero 10+ with Zotero-Server-ID support")
        self.server_id = server_id
        self.session.headers["Zotero-Server-ID"] = server_id

    @property
    def scope(self) -> str:
        self.connect()
        return f"{self.server_id}/{self.prefix}"

    def close(self) -> None:
        self.session.close()

    def _authorize(self) -> None:
        with self.session.post(self._url("local/authorize"), json={"appName": "Paper Crawler"}, timeout=self.budget.timeout(120)) as response:
            if response.status_code == 403:
                raise ZoteroError("Zotero write authorization denied")
            response.raise_for_status()
            key = response.json().get("key")
        if not key:
            raise ZoteroError("Zotero returned no local authorization key")
        self.session.headers["Zotero-API-Key"] = key

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        self.budget.check()
        self.connect()
        writing = method in {"POST", "PUT", "PATCH", "DELETE"}
        if writing and path.endswith(("/items", "/collections")):
            headers = dict(kwargs.pop("headers", {}))
            headers.setdefault("Zotero-Write-Token", secrets.token_hex(16))
            kwargs["headers"] = headers
        for attempt in range(2):
            response = self.session.request(method, self._url(path), timeout=self.budget.timeout(30), **kwargs)
            if response.status_code == 401 and writing and attempt == 0:
                response.close()
                self._authorize()
                continue
            if response.status_code == 403:
                response.close()
                raise ZoteroError("Zotero local API disabled or authorization denied")
            try:
                response.raise_for_status()
            except requests.RequestException:
                response.close()
                raise
            return response
        raise ZoteroError("Zotero authorization failed")

    def _json(self, method, path, **kwargs):
        with self._request(method, path, **kwargs) as response:
            return response.json()

    def _existing(self, key):
        try:
            return self._json("GET", f"{self.prefix}items/{key}")
        except requests.HTTPError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    def _find_item(self, paper: Paper) -> str | None:
        items = self._json("GET", f"{self.prefix}items", params={
            "q": paper.title, "qmode": "titleCreatorYear", "itemType": "-attachment", "limit": 50})
        expected_title = paper.title.strip().casefold()
        expected_doi = paper.doi.strip().casefold()
        for item in items:
            data = item.get("data", item)
            if data.get("title", "").strip().casefold() != expected_title:
                continue
            if expected_doi and data.get("DOI", "").strip().casefold() != expected_doi:
                continue
            if not expected_doi and paper.landing_url and data.get("url", "") != paper.landing_url:
                continue
            return item.get("key") or data.get("key")
        return None

    def _find_attachment(self, parent_key: str, pdf_path: Path) -> str | None:
        children = self._json("GET", f"{self.prefix}items/{parent_key}/children")
        for item in children:
            data = item.get("data", item)
            if data.get("itemType") == "attachment" and data.get("filename") == pdf_path.name:
                return item.get("key") or data.get("key")
        return None

    def _library_version(self) -> str:
        with self._request("GET", f"{self.prefix}items", params={"limit": 1, "format": "versions"}) as response:
            version = response.headers.get("Last-Modified-Version")
        if version is None:
            raise ZoteroError("Zotero did not return the current library version")
        return version

    def _create_keyed_item(self, item: dict[str, Any]) -> dict[str, Any]:
        # A client-provided key makes retries recoverable, but Zotero's local API
        # requires the current library version as the creation precondition.
        headers = {"If-Unmodified-Since-Version": self._library_version()}
        return self._json("POST", f"{self.prefix}items", json=[item], headers=headers)

    @staticmethod
    def _key():
        return "".join(secrets.choice("23456789ABCDEFGHIJKLMNPQRSTUVWXYZ") for _ in range(8))

    def legacy_item_matches(self, key: str, paper: Paper) -> bool:
        existing = self._existing(key)
        if not existing:
            return False
        data = existing.get("data", existing)
        # Old state has no server identity: never adopt an unrelated item key.
        return data.get("title", "").strip().casefold() == paper.title.strip().casefold() and (
            not paper.doi or data.get("DOI", "").strip().casefold() == paper.doi.strip().casefold())

    def ensure_collection(self, name: str, parent_key: str | None = None) -> str | None:
        offset = 0
        while True:
            params = {"limit": 100, "start": offset, "format": "json"}
            collections = self._json("GET", f"{self.prefix}collections", params=params)
            for collection in collections:
                data = collection.get("data", collection)
                if data.get("name") == name and data.get("parentCollection", False) == (parent_key or False):
                    return collection.get("key") or data.get("key")
            if len(collections) < 100:
                break
            offset += len(collections)
        payload = [{"name": name, "parentCollection": parent_key or False}]
        created = self._json("POST", f"{self.prefix}collections", json=payload)
        saved = created.get("success", created.get("successful", {}))
        item = saved.get("0", {})
        key = item if isinstance(item, str) else item.get("key")
        if not key:
            raise ZoteroError(f"Unable to create Zotero collection {name}: {created}")
        return key

    def target_collection(self, conference_name: str) -> str | None:
        root = self.ensure_collection(self.config.root_collection)
        if not self.config.create_subcollections:
            return root
        try:
            return self.ensure_collection(conference_name, root)
        except (requests.RequestException, ZoteroError):
            if self.config.fallback_to_root:
                return root
            raise

    def create_item(self, paper: Paper, collection_key: str | None, item_key: str | None = None) -> str:
        creators = []
        for author in paper.authors:
            parts = author.strip().split()
            if len(parts) > 1:
                creators.append(
                    {"creatorType": "author", "firstName": " ".join(parts[:-1]), "lastName": parts[-1]}
                )
            elif author.strip():
                creators.append({"creatorType": "author", "name": author.strip()})
        item = {
            "itemType": paper.item_type,
            "title": paper.title,
            "creators": creators,
            "abstractNote": paper.abstract,
            ("publicationTitle" if paper.item_type == "journalArticle" else "conferenceName"): paper.venue,
            "date": paper.metadata.get("date") or str(paper.year),
            "url": paper.landing_url,
            "DOI": paper.doi,
            "collections": [collection_key] if collection_key else [],
            "tags": [{"tag": self.config.root_collection}, {"tag": paper.track}],
            "relations": {},
        }
        if item_key:
            item["key"] = item_key
        if paper.item_type == "journalArticle":
            item["publicationTitle"] = paper.metadata.get("publicationTitle") or paper.venue
            item.update({field: paper.metadata[field] for field in ("volume", "issue", "ISSN") if paper.metadata.get(field)})
        result = self._create_keyed_item(item) if item_key else self._json("POST", f"{self.prefix}items", json=[item])
        saved = result.get("success", result.get("successful", {}))
        item_result = saved.get("0", {})
        key = item_result if isinstance(item_result, str) else item_result.get("key")
        if not key:
            raise ZoteroError(f"Unable to create Zotero item: {result}")
        return key

    def create_attachment(self, parent_key: str, pdf_path: Path, attachment_key: str | None = None) -> str:
        attachment = {
            "itemType": "attachment",
            "parentItem": parent_key,
            "linkMode": "imported_file",
            "title": pdf_path.name,
            "contentType": mimetypes.guess_type(pdf_path.name)[0] or "application/pdf",
            "filename": pdf_path.name,
            "tags": [],
            "relations": {},
        }
        if attachment_key:
            attachment["key"] = attachment_key
        result = self._create_keyed_item(attachment) if attachment_key else self._json("POST", f"{self.prefix}items", json=[attachment])
        saved = result.get("success", result.get("successful", {}))
        item_result = saved.get("0", {})
        key = item_result if isinstance(item_result, str) else item_result.get("key")
        if not key:
            raise ZoteroError(f"Unable to create Zotero attachment: {result}")
        return key

    def upload_attachment(self, attachment_key: str, pdf_path: Path) -> None:
        hasher = hashlib.md5()
        with pdf_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024*1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        existing = self._existing(attachment_key)
        if existing and existing.get("data", existing).get("md5") == digest:
            return
        stat = pdf_path.stat()
        path = f"{self.prefix}items/{attachment_key}/file"
        headers = {
            "If-None-Match": "*",
            "Content-Type": "application/x-www-form-urlencoded",
        }
        form = {"md5": digest, "filename": pdf_path.name, "filesize": str(stat.st_size), "mtime": str(int(stat.st_mtime * 1000))}
        init = self._json("POST", path, headers=headers, data=form)
        if init.get("exists") == 1:
            return
        upload_url = init.get("url")
        upload_key = init.get("uploadKey")
        if not upload_url or not upload_key:
            raise ZoteroError(f"Zotero did not return an upload target: {init}")
        with pdf_path.open("rb") as handle:
            upload = requests.post(
                upload_url,
                data=handle,
                headers={"Content-Type": init.get("contentType", "application/pdf")},
                timeout=self.budget.timeout(120),
            )
        try:
            upload.raise_for_status()
        finally:
            upload.close()
        self._request(
            "POST",
            path,
            headers={"If-None-Match": "*", "Content-Type": "application/x-www-form-urlencoded"},
            data={"upload": upload_key},
        ).close()

    def import_paper(self, paper: Paper, pdf_path: Path, collection_key: str | None,
                     progress: dict | None = None, checkpoint=None) -> str:
        progress = dict(progress or {})
        def save(**values):
            progress.update(values)
            if checkpoint:
                checkpoint(**values)
        item_key = progress.get("item_key")
        if not item_key or not self._existing(item_key):
            item_key = self._find_item(paper) or self.create_item(paper, collection_key)
            save(item_key=item_key)
        save(status="item_created")
        attachment_key = progress.get("attachment_key")
        if not attachment_key or not self._existing(attachment_key):
            attachment_key = self._find_attachment(item_key, pdf_path) or self.create_attachment(item_key, pdf_path)
            save(attachment_key=attachment_key)
        save(status="attachment_created")
        self.upload_attachment(attachment_key, pdf_path)
        save(status="imported")
        return item_key
