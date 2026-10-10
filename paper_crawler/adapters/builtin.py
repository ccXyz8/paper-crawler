from __future__ import annotations

import logging
import hashlib
import json
import requests
import re
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from ..models import Paper
from ..keywords import matches_keywords
from .base import ConferenceAdapter

LOG = logging.getLogger(__name__)


def _text(node):
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def _meta(soup, name):
    return [node.get("content", "").strip() for node in soup.select(f'meta[name="{name}"]') if node.get("content")]


class HtmlAdapter(ConferenceAdapter):
    def __init__(self):
        self.errors = []
        self.store = None
        self.cache_scope = ""
        self.cache_ttl_hours = 24
        self.cache_hits = 0
        self.emit_failures = False
        self.denied_count = 0
        self.page_keys = {}

    def invalidate_pages(self, url=None):
        if self.store:
            for address, keys in self.page_keys.items():
                if url is None or address == url:
                    for key in keys:
                        self.store.delete_page(key)

    def failure(self, url, exc):
        self.invalidate_pages(url)
        response = getattr(exc, "response", None)
        status = response.status_code if response is not None else None
        kind = "access_denied" if status in {401, 403} else "not_found" if status == 404 else "network" if isinstance(exc, requests.RequestException) else "parse"
        self.errors.append({"url": url, "error": str(exc), "status": status, "kind": kind})
        self.denied_count = self.denied_count + 1 if status in {401,403} else 0
        if self.denied_count >= 3:
            raise RuntimeError("Repeated access denial; source disabled for this run, use an authorized alternate source")
        LOG.warning("Unable to parse %s: %s", url, exc)

    def read(self, client, url, params=None, as_json=False):
        key = hashlib.sha256(json.dumps([self.cache_scope, url, params, as_json], sort_keys=True).encode()).hexdigest()
        self.page_keys.setdefault(url, set()).add(key)
        if self.store:
            value = self.store.page(key, self.cache_ttl_hours)
            if value is not None:
                self.cache_hits += 1
                return value
        response = client.get(url, **({"params":params} if params is not None else {}))
        try:
            value = response.json() if as_json else response.text
        finally:
            response.close()
        if self.store:
            self.store.save_page(key, value)
        return value

    def soup(self, client, url):
        return BeautifulSoup(self.read(client, url), "html.parser")

    def detail(self, conference, client, url, year, track="main"):
        try:
            if self.store:
                cached = self.store.metadata(self.cache_scope, url, self.cache_ttl_hours)
                if cached:
                    self.cache_hits += 1
                    return cached
            document_url = url
            try:
                soup = self.soup(client, url)
            except requests.HTTPError as exc:
                alternate = conference.options.get("detail_url_map", {}).get(url)
                if exc.response.status_code != 404 or not alternate:
                    raise
                document_url = alternate
                soup = self.soup(client, alternate)
                LOG.info("Recovered detail page via configured URL: %s -> %s", url, alternate)
            title = next(iter(_meta(soup, "citation_title")), "") or _text(soup.select_one("#papertitle, h1"))
            pdf = next(iter(_meta(soup, "citation_pdf_url")), "")
            if not pdf:
                link = soup.select_one('a[href$=".pdf"], a[href*="/article/download/"]')
                pdf = link.get("href", "") if link else ""
            if not title or not pdf:
                raise ValueError("Missing paper title or PDF URL")
            abstract = _text(soup.select_one("#abstract, .acl-abstract, .abstract, .paper-abstract, section.item.abstract"))
            abstract = abstract or next(iter(_meta(soup, "citation_abstract")), "")
            if not abstract:
                heading = next((h for h in soup.select("h2, h3, h4") if _text(h).casefold() == "abstract"), None)
                abstract = _text(heading.find_next_sibling()) if heading else ""
            metadata = {}
            for field, tag in {"volume": "citation_volume", "issue": "citation_issue",
                               "ISSN": "citation_issn", "date": "citation_publication_date",
                               "publicationTitle": "citation_journal_title"}.items():
                metadata[field] = next(iter(_meta(soup, tag)), "")
            self.denied_count = 0
            return Paper(title=title, venue=conference.name, year=year,
                         pdf_url=urljoin(document_url, pdf), landing_url=url, abstract=abstract,
                         authors=_meta(soup, "citation_author"),
                         doi=next(iter(_meta(soup, "citation_doi")), ""), source_id=url,
                         metadata=metadata, track=track,
                         item_type=conference.options.get("item_type", "conferencePaper"))
        except Exception as exc:
            self.failure(url, exc)
            return None


class CvfAdapter(HtmlAdapter):
    def iter_papers(self, conference, client):
        for year in conference.years:
            base = conference.website or "https://openaccess.thecvf.com"
            index = f"{base.rstrip('/')}/{conference.name.upper()}{year}"
            pages, visited, papers = [index], set(), set()
            while pages:
                url = pages.pop(0)
                if url in visited:
                    continue
                visited.add(url)
                try:
                    soup = self.soup(client, url)
                except Exception as exc:
                    self.failure(url, exc)
                    continue
                for link in soup.select('a[href*="day="]'):
                    candidate = urljoin(url, link["href"])
                    if urlparse(candidate).netloc == urlparse(index).netloc and urlparse(candidate).path.rstrip('/') == urlparse(index).path:
                        if candidate not in visited:
                            pages.append(candidate)
                links = soup.select("dt.ptitle a[href]")
                if conference.options.get("prioritize_title_matches") and conference.keywords:
                    links.sort(key=lambda link: not matches_keywords(
                        Paper(_text(link), conference.name, year, ""), conference.keywords))
                    pages.sort(key=lambda page: "day=all" not in page)
                for link in links:
                    landing = urljoin(url, link["href"])
                    if landing in papers:
                        continue
                    papers.add(landing)
                    paper = self.detail(conference, client, landing, year)
                    if paper or self.emit_failures:
                        yield paper


class OpenReviewAdapter(HtmlAdapter):
    def iter_papers(self, conference, client):
        api = conference.options.get("api_url", "https://api2.openreview.net/notes")
        for year in conference.years:
            venue = conference.options.get("venue_id_template", "{name}.cc/{year}/Conference").format(name=conference.name, year=year)
            accepted_only = conference.options.get("accepted_only", True)
            filters = {"content.venueid": venue} if accepted_only else {}
            if not accepted_only:
                template = conference.options.get("invitation_template")
                if not template:
                    response = client.get(api.rsplit('/', 1)[0]+"/groups", params={"id": venue})
                    try:
                        value = response.json()["groups"][0]["content"]["submission_name"]
                        name = value.get("value") if isinstance(value, dict) else value
                    finally:
                        response.close()
                    filters["invitation"] = f"{venue}/-/{name}"
                else:
                    filters["invitation"] = template.format(name=conference.name, year=year)
            offset, seen = 0, set()
            while True:
                payload = self.read(client, api, params={**filters, "limit": 1000, "offset": offset}, as_json=True)
                if "notes" not in payload:
                    raise ValueError("OpenReview response missing notes")
                notes = payload["notes"]
                if not notes:
                    break
                new = 0
                for note in notes:
                    try:
                        content = {k: v.get("value", v) if isinstance(v, dict) else v for k, v in note.get("content", {}).items()}
                        note_id = note["id"]
                        if note_id in seen:
                            continue
                        seen.add(note_id)
                        new += 1
                        if accepted_only and content.get("venueid") != venue:
                            continue
                        if not content.get("title") or not content.get("pdf"):
                            raise ValueError("OpenReview note missing title or PDF")
                        authors = content.get("authors") or []
                        yield Paper(title=content["title"].strip(), venue=conference.name, year=year,
                                    pdf_url=urljoin("https://openreview.net", content["pdf"]),
                                    landing_url=f"https://openreview.net/forum?id={note_id}",
                                    abstract=content.get("abstract") or "", authors=[authors] if isinstance(authors, str) else authors,
                                    doi=content.get("doi") or "", source_id=f"openreview:{note_id}",
                                    track=content.get("venue") or "main",
                                    metadata={"acceptance_status": "accepted" if content.get("venueid") == venue else "other",
                                              "venueid": content.get("venueid"), "invitations": note.get("invitations", [])})
                    except Exception as exc:
                        self.failure(f"{api}?id={note.get('id', '')}", exc)
                        if self.emit_failures:
                            yield None
                if not new:
                    raise ValueError("OpenReview pagination repeated the same page")
                offset += len(notes)
                if payload.get("count") is not None and offset >= payload["count"]:
                    break
                # Continue until an empty page, even if the server caps page size.


class AclAnthologyAdapter(HtmlAdapter):
    def iter_papers(self, conference, client):
        for year in conference.years:
            url = conference.options.get("year_url_template", "https://aclanthology.org/events/acl-{year}/").format(year=year)
            soup = self.soup(client, url)
            allowed = conference.options.get("allowed_volumes", [f"{year}.acl-long", f"{year}.acl-short"])
            seen = set()
            for link in soup.select("a[href]"):
                landing = urljoin(url, link["href"])
                paper_id = urlparse(landing).path.strip('/')
                if not re.fullmatch(r"(?:20\d{2}\.[\w-]+\.\d+|P\d{2}-\d{4})", paper_id):
                    continue
                if paper_id.endswith(".0") or paper_id.endswith("-1000"):
                    continue
                volume = paper_id.rsplit('.', 1)[0]
                if allowed and volume not in allowed:
                    continue
                if landing in seen:
                    continue
                seen.add(landing)
                paper = self.detail(conference, client, landing, year, volume)
                if paper or self.emit_failures:
                    yield paper


class PmlrAdapter(HtmlAdapter):
    DEFAULT_VOLUMES = {2018:80, 2019:97, 2020:119, 2021:139, 2022:162, 2023:202, 2024:235, 2025:267}

    def iter_papers(self, conference, client):
        for year in conference.years:
            volume = conference.options.get("volumes", {}).get(str(year), self.DEFAULT_VOLUMES.get(year))
            if not volume:
                raise ValueError(f"No PMLR volume configured for {year}")
            url = f"https://proceedings.mlr.press/v{volume}/"
            soup = self.soup(client, url)
            seen = set()
            for link in soup.select(".paper a[href$='.html']"):
                landing = urljoin(url, link["href"])
                if landing in seen or not urlparse(landing).path.startswith(f"/v{volume}/"):
                    continue
                seen.add(landing)
                paper = self.detail(conference, client, landing, year)
                if paper or self.emit_failures:
                    yield paper


class AaaiAdapter(HtmlAdapter):
    def iter_papers(self, conference, client):
        archive_url = conference.options.get("archive_url", "https://ojs.aaai.org/index.php/AAAI/issue/archive")
        pages, visited, issues, papers = [archive_url], set(), set(), set()
        while pages:
            url = pages.pop(0)
            if url in visited:
                continue
            visited.add(url)
            try:
                archive = self.soup(client, url)
            except Exception as exc:
                self.failure(url, exc)
                continue
            for link in archive.select('a.next, a[rel="next"]'):
                candidate = urljoin(url, link.get("href", ""))
                if candidate not in visited and urlparse(candidate).netloc == urlparse(url).netloc:
                    pages.append(candidate)
            for link in archive.select('a[href*="/issue/view/"]'):
                issue_url = urljoin(url, link["href"])
                container = link.find_parent(class_="obj_issue_summary") or link
                years = [y for y in conference.years if str(y) in _text(container)]
                if not years or issue_url in issues:
                    continue
                issues.add(issue_url)
                try:
                    issue = self.soup(client, issue_url)
                except Exception as exc:
                    self.failure(issue_url, exc)
                    continue
                for paper_link in issue.select('h3 a[href*="/article/view/"], .title a[href*="/article/view/"]'):
                    landing = urljoin(issue_url, paper_link["href"])
                    if landing in papers:
                        continue
                    papers.add(landing)
                    paper = self.detail(conference, client, landing, years[0])
                    if paper or self.emit_failures:
                        yield paper


class GenericHtmlAdapter(HtmlAdapter):
    def iter_papers(self, conference, client):
        selectors = conference.options["selectors"]
        for year in conference.years:
            pages = [conference.options["year_url_template"].format(year=year)]
            visited, seen = set(), set()
            while pages:
                url = pages.pop(0)
                if url in visited:
                    continue
                visited.add(url)
                soup = self.soup(client, url)
                if selectors.get("next"):
                    for link in soup.select(selectors["next"]):
                        candidate = urljoin(url, link.get("href", ""))
                        if urlparse(candidate).netloc == urlparse(url).netloc and candidate not in visited:
                            pages.append(candidate)
                for item in soup.select(selectors["item"]):
                    try:
                        title = item.select_one(selectors["title"])
                        landing_node = item.select_one(selectors.get("landing", selectors["title"]))
                        if conference.options.get("detail_pages"):
                            if not landing_node or not landing_node.get("href"):
                                raise ValueError("Missing landing URL")
                            landing = urljoin(url, landing_node["href"])
                            if landing in seen:
                                continue
                            seen.add(landing)
                            paper = self.detail(conference, client, landing, year, conference.options.get("track", "main"))
                        else:
                            pdf = item.select_one(selectors["pdf"])
                            if not title or not pdf or not pdf.get("href"):
                                raise ValueError("Missing title or PDF")
                            pdf_url = urljoin(url, pdf["href"])
                            if pdf_url in seen:
                                continue
                            seen.add(pdf_url)
                            abstract = item.select_one(selectors["abstract"]) if selectors.get("abstract") else None
                            landing = urljoin(url, landing_node.get("href", "")) if landing_node else pdf_url
                            paper = Paper(title=_text(title), venue=conference.name, year=year,
                                          pdf_url=pdf_url, landing_url=landing, abstract=_text(abstract), source_id=pdf_url,
                                          item_type=conference.options.get("item_type", "conferencePaper"),
                                          track=conference.options.get("track", "main"))
                        if paper or self.emit_failures:
                            yield paper
                    except Exception as exc:
                        self.failure(url, exc)
                        if self.emit_failures:
                            yield None


class NeuripsAdapter(HtmlAdapter):
    """Published proceedings; separate main and datasets/benchmarks tracks."""
    def iter_papers(self, conference, client):
        for year in conference.years:
            url = conference.options.get("year_url_template", "https://papers.nips.cc/paper_files/paper/{year}").format(year=year)
            soup = self.soup(client, url)
            seen = set()
            links = soup.select('a[href*="-Abstract"]')
            if conference.keywords:
                links.sort(key=lambda a: not matches_keywords(Paper(_text(a), conference.name, year, ""), conference.keywords))
            for link in links:
                landing = urljoin(url, link["href"])
                if landing in seen:
                    continue
                seen.add(landing)
                track = "datasets_benchmarks" if "-Datasets_and_Benchmarks" in landing else "main"
                if track not in conference.options.get("allowed_tracks", ["main"]):
                    continue
                paper = self.detail(conference, client, landing, year, track)
                if paper:
                    paper.metadata["acceptance_status"] = "published"
                    paper.metadata["source"] = "neurips_proceedings"
                if paper or self.emit_failures:
                    yield paper


ADAPTERS = {"cvf": CvfAdapter, "openreview": OpenReviewAdapter, "acl_anthology": AclAnthologyAdapter,
            "pmlr": PmlrAdapter, "aaai": AaaiAdapter, "generic_html": GenericHtmlAdapter, "neurips": NeuripsAdapter}


def build_adapter(name):
    try:
        return ADAPTERS[name]()
    except KeyError as exc:
        raise ValueError(f"Unknown adapter: {name}") from exc
