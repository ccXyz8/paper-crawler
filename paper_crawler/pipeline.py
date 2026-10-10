from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from dataclasses import replace
from pathlib import Path
import requests
from .adapters.builtin import build_adapter
from .config import load_config
from .http_client import HttpClient
from .keywords import keyword_matches
from .models import AppConfig, Paper, LoginConfig
from .storage import StateStore, filename_for
from .schedule import eligibility, utc_now, validate_meeting_dates
from .zotero import ZoteroClient

LOG = logging.getLogger(__name__)


def valid_pdf(path: Path) -> bool:
    """Check PDF signature and end marker; not a full PDF parser."""
    try:
        with path.open("rb") as handle:
            header = handle.read(1024)
            handle.seek(max(0, path.stat().st_size-4096))
            return b"%PDF-" in header and b"%%EOF" in handle.read()
    except OSError:
        return False


def _download(paper: Paper, client: HttpClient, target: Path) -> bool:
    target.parent.mkdir(parents=True, exist_ok=True)
    if valid_pdf(target):
        return False
    temporary = target.with_suffix(target.suffix+".part")
    for attempt in range(client.config.rate_limit.max_retries+1):
        try:
            with client.get(paper.pdf_url, stream=True) as response:
                media = response.headers.get("Content-Type", "").lower()
                if "text/html" in media or "application/json" in media:
                    raise ValueError(f"PDF URL returned {media}")
                count = 0
                with temporary.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=128*1024):
                        if hasattr(client, "budget"):
                            client.budget.check()
                        if chunk:
                            handle.write(chunk)
                            count += len(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
                length = response.headers.get("Content-Length")
                if length and not response.headers.get("Content-Encoding") and count != int(length):
                    raise ValueError("Incomplete PDF download")
            if not valid_pdf(temporary):
                raise ValueError("Invalid or truncated PDF")
            temporary.replace(target)
            return True
        except requests.HTTPError:
            temporary.unlink(missing_ok=True)
            raise
        except (requests.RequestException, ValueError):
            temporary.unlink(missing_ok=True)
            if attempt >= client.config.rate_limit.max_retries:
                raise
            if hasattr(client, "budget"):
                client.budget.sleep(client.config.rate_limit.backoff_seconds*2**attempt)
            else:
                time.sleep(client.config.rate_limit.backoff_seconds*2**attempt)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    return False


def source_key(source):
    return hashlib.sha256(json.dumps(["adapter-v2", source.name, source.website, source.adapter,
                                      source.years[0], source.options], sort_keys=True).encode()).hexdigest()


def _source_papers(source, config, state, get_client, counters, failure, budget):
    """Yield individual candidates; a failed detail yields None for fair scheduling."""
    variants = [source] + [replace(source, adapter=f["adapter"], website=f.get("website", ""),
                                  options=f.get("options", {}), login=LoginConfig(), fallbacks=[]) for f in source.fallbacks]
    seen = set()
    known = {}
    def identity(p):
        return (p.title.strip().casefold(), p.year, p.venue)
    for candidate in variants:
        for p in state.partial(source_key(candidate), config.cache_ttl_hours, offline=True):
            known.setdefault(identity(p), []).append(p)
    for position, variant in enumerate(variants):
        budget.check()
        key = source_key(variant)
        adapter, iterator = None, None
        errors_read = 0
        complete, collected = False, []
        counters["adapters_tried"].append(variant.adapter)
        if position:
            LOG.info("Trying configured fallback for %s %s: %s", source.name, source.years[0], variant.adapter)
            counters["fallback_used"] = True
        try:
            snapshot = state.cached(key, config.cache_ttl_hours, config.offline)
            if snapshot is not None:
                iterator = iter(snapshot)
                counters["cache_mode"] = "complete"
            elif config.offline:
                partial = state.partial(key, config.cache_ttl_hours, offline=True)
                if not partial:
                    raise ValueError("No cached metadata for this source/year")
                iterator = iter(partial)
                counters["cache_mode"] = "partial_offline"
                counters["partial_cache"] = True
            else:
                adapter = build_adapter(variant.adapter)
                adapter.store, adapter.cache_scope = state, key
                adapter.cache_ttl_hours = config.cache_ttl_hours
                adapter.emit_failures = True
                keywords = variant.keywords if variant.keywords is not None else config.keywords
                variant = replace(variant, keywords=keywords,
                                  options={**variant.options, "prioritize_title_matches": True})
                iterator = adapter.iter_papers(variant, get_client(variant))
            for paper in iterator:
                budget.check()
                if adapter:
                    for error in adapter.errors[errors_read:]:
                        failure(source.name, source.years[0], error["url"], error["error"], counters, error.get("kind"))
                    errors_read = len(adapter.errors)
                if paper is not None:
                    collected.append(paper)
                    locator = paper.landing_url or paper.source_id or paper.stable_id
                    if state.metadata(key, locator, config.cache_ttl_hours) is None:
                        state.save_metadata(key, paper)
                    # Only match differing source IDs by exact title/year/venue AND
                    # a shared author; conflicting explicit DOIs never get merged.
                    authors = {a.strip().casefold() for a in paper.authors}
                    original = next((p for p in known.get(identity(paper), []) if p.stable_id != paper.stable_id
                                     and p.title.strip().casefold() == paper.title.strip().casefold()
                                     and p.year == paper.year and p.venue == paper.venue
                                     and authors.intersection(a.strip().casefold() for a in p.authors)
                                     and not (p.doi and paper.doi and p.stable_id != paper.stable_id)), None)
                    if original:
                        state.alias(paper, original)
                    canonical = state.canonical(paper.stable_id)
                    if canonical in seen:
                        continue
                    seen.add(canonical)
                    if not any(p.stable_id == paper.stable_id for p in known.get(identity(paper), [])):
                        known.setdefault(identity(paper), []).append(paper)
                yield paper
            if adapter:
                for error in adapter.errors[errors_read:]:
                    failure(source.name, source.years[0], error["url"], error["error"], counters, error.get("kind"))
                errors_read = len(adapter.errors)
            if not collected:
                if adapter:
                    adapter.invalidate_pages()
                raise ValueError("No papers found; check source configuration or page structure")
            complete = not counters.get("partial_cache") and (adapter is None or not adapter.errors)
            if complete and adapter is not None:
                state.cache(key, collected)
            if complete or config.offline:
                counters["complete"] = complete
                return
        except Exception as exc:
            if adapter and isinstance(exc, (ValueError, KeyError, TypeError)):
                adapter.invalidate_pages()
            failure(source.name, source.years[0], variant.website, exc, counters)
        finally:
            if iterator is not None and hasattr(iterator, "close"):
                iterator.close()
            if adapter:
                for error in adapter.errors[errors_read:]:
                    failure(source.name, source.years[0], error["url"], error["error"], counters, error.get("kind"))
                counters["cache_hits"] += getattr(adapter, "cache_hits", 0) if isinstance(getattr(adapter, "cache_hits", 0), int) else 0
    counters["complete"] = False


def run(config: AppConfig) -> dict[str, int]:
    from collections import deque
    from .budget import RunBudget, RunStopped
    from .config import validate_runtime_options
    validate_runtime_options(vars_config := {name: getattr(config, name) for name in (
        "max_matches", "max_papers", "max_seconds", "scheduling", "source_batch_size", "newest_first", "metadata_only")})
    for conference in config.conferences:
        validate_meeting_dates(conference.meeting_dates)
    budget = RunBudget(config.max_seconds)
    state = StateStore(config.state_file)
    stats = dict(seen=0, scanned=0, matched=0, downloaded=0, reused=0, imported=0, skipped=0, failed=0, attempted=0)
    report = {"sources": [], "failures": [], "matches": [], **vars_config, "stop_reason": "completed"}
    attempted_ids, scanned_ids = set(), set()
    clients, collections, jobs = {}, {}, []
    zotero = None
    want_import = config.zotero.enabled and not (config.download_only or config.dry_run or config.metadata_only)

    def failure(venue, year, url, exc, counters=None, kind=None):
        stats["failed"] += 1
        if counters is not None:
            counters["failed"] += 1
        response = getattr(exc, "response", None)
        status = response.status_code if response is not None else None
        kind = kind or ("access_denied" if status in {401,403} else "not_found" if status == 404 else "source_error")
        advice = {"access_denied": "Check access/network/API configuration; use a configured official fallback. No access bypass is attempted.",
                  "not_found": "Check publisher links or configure an exact detail_url_map replacement."}.get(kind, "See error and URL; cached successful metadata can be reused.")
        report["failures"].append({"venue": venue, "year": year, "url": url, "error": str(exc), "kind": kind, "advice": advice})
        LOG.error("%s %s: %s (%s)", venue, year, exc, url)

    def get_client(index, source):
        key = (index, source.adapter, source.website)
        if key not in clients:
            clients[key] = HttpClient(config.user_agent, source, budget=budget)
            # Alternate endpoints keep the original source's aggregate rate budget.
            previous = next((c for k, c in clients.items() if k != key and k[0] == index), None)
            if previous is not None:
                clients[key].limiter = previous.limiter
        return clients[key]

    def process(paper, source, index, counters):
        nonlocal zotero
        keywords = source.keywords if source.keywords is not None else config.keywords
        matched, evidence = keyword_matches(paper, keywords, config.keyword_mode,
                                            config.exclude_keywords, config.keyword_word_boundary)
        if not matched:
            return
        counters["matched"] += 1
        stats["matched"] += 1
        report["matches"].append({"id": paper.stable_id, "title": paper.title, "venue": paper.venue,
                                  "year": paper.year, "track": paper.track, "url": paper.landing_url, "evidence": evidence})
        if paper.stable_id in attempted_ids:
            stats["skipped"] += 1
            return
        if config.metadata_only:
            attempted_ids.add(paper.stable_id)
            stats["attempted"] += 1
            return
        previous = state.get(paper)
        target = Path(previous["pdf"]) if previous.get("pdf") else Path(config.download_dir)/source.name/str(paper.year)/filename_for(paper)
        counted = False
        try:
            state.mark(paper, matches=evidence)
            local_valid = valid_pdf(target)
            imported, can_import, collection = {}, False, None
            if want_import:
                if index not in collections:
                    try:
                        zotero = zotero or ZoteroClient(config.zotero, budget=budget)
                        collections[index] = zotero.target_collection(source.name)
                    except Exception as exc:
                        collections[index] = None
                        failure(source.name, paper.year, config.zotero.base_url, exc, counters)
                collection = collections[index]
                can_import = collection is not None
            if can_import:
                scope = zotero.scope
                imported = previous.get("imports", {}).get(scope, {})
                def checkpoint(**values):
                    imports = state.get(paper).get("imports", {})
                    imports[scope] = {**imports.get(scope, {}), **values}
                    state.mark(paper, imports=imports)
                if not imported and previous.get("status") == "imported" and previous.get("zotero_key"):
                    if zotero.legacy_item_matches(previous["zotero_key"], paper):
                        imported = {"status": "imported", "item_key": previous["zotero_key"]}
                        checkpoint(**imported)
            if local_valid and (not want_import or imported.get("status") == "imported"):
                stats["reused"] += 1
                stats["skipped"] += 1
                return
            attempted_ids.add(paper.stable_id)
            stats["attempted"] += 1
            counted = True
            LOG.info("Processing match %s/%s: %s", stats["attempted"], config.max_matches or "unlimited", paper.title)
            if local_valid:
                stats["reused"] += 1
            elif config.offline:
                raise ValueError("Offline mode: PDF is missing or invalid")
            else:
                if _download(paper, get_client(index, source), target):
                    stats["downloaded"] += 1
                    counters["downloaded"] += 1
                    LOG.info("Downloaded: %s", target)
            state.mark(paper, pdf=str(target.resolve()), downloaded=True)
            if can_import and imported.get("status") != "imported":
                budget.check()
                zotero.import_paper(paper, target, collection, progress=imported, checkpoint=checkpoint)
                checkpoint(status="imported")
                stats["imported"] += 1
                counters["imported"] += 1
        except Exception as exc:
            if not counted:
                attempted_ids.add(paper.stable_id)
                stats["attempted"] += 1
            failure(source.name, paper.year, paper.landing_url or paper.pdf_url, exc, counters)

    now = utc_now()
    report["schedule_checked_at"] = now.isoformat()
    report["skipped_sources"] = []
    for index, conference in enumerate(config.conferences):
        if not conference.enabled:
            continue
        years = sorted(set(conference.years), reverse=True) if config.newest_first else list(dict.fromkeys(conference.years))
        for year in years:
            reason = eligibility(conference, year, now)
            if reason:
                report["skipped_sources"].append(dict(venue=conference.name, year=year, reason=reason,
                    meeting_dates=conference.meeting_dates.get(str(year))))
                LOG.info("Skipping %s %s: %s", conference.name, year, reason)
                continue
            source = replace(conference, years=[year])
            counters = dict(venue=source.name, year=year, seen=0, scanned=0, matched=0, missing_abstract=0,
                            downloaded=0, imported=0, failed=0, complete=False, limited=False,
                            cache_hits=0, adapters_tried=[], fallback_used=False)
            iterator = _source_papers(source, config, state, lambda variant, i=index: get_client(i, variant), counters, failure, budget)
            jobs.append((index, source, counters, iterator))
    if config.newest_first:
        jobs.sort(key=lambda job: job[1].years[0], reverse=True)
    queue = deque(jobs)
    try:
        while queue:
            budget.check()
            if config.max_matches and stats["attempted"] >= config.max_matches:
                raise RunStopped("max_matches_reached")
            if config.max_papers and stats["scanned"] >= config.max_papers:
                raise RunStopped("max_papers_reached")
            job = queue.popleft()
            index, source, counters, iterator = job
            if not any(c is counters for c in report["sources"]):
                report["sources"].append(counters)
            done = False
            for _ in range(config.source_batch_size):
                budget.check()
                if config.max_matches and stats["attempted"] >= config.max_matches:
                    raise RunStopped("max_matches_reached")
                if config.max_papers and stats["scanned"] >= config.max_papers:
                    raise RunStopped("max_papers_reached")
                try:
                    paper = next(iterator)
                except StopIteration:
                    done = True
                    break
                # Failures count as scanned candidates; duplicate IDs don't consume global scan quota.
                counters["scanned"] += 1
                if paper is not None and paper.stable_id in scanned_ids:
                    stats["skipped"] += 1
                    continue
                stats["scanned"] += 1
                if paper is not None:
                    scanned_ids.add(paper.stable_id)
                    stats["seen"] += 1
                    counters["seen"] += 1
                    counters["missing_abstract"] += int(not bool(paper.abstract.strip()))
                    process(paper, source, index, counters)
                LOG.info("Progress %s %s: scanned=%s matched=%s attempted=%s downloaded=%s", source.name, source.years[0],
                         stats["scanned"], stats["matched"], stats["attempted"], stats["downloaded"])
            if not done:
                if config.scheduling == "sequential":
                    queue.appendleft(job)
                else:
                    if config.newest_first:
                        # Rotate within the current year; older years wait until it is exhausted.
                        position = next((i for i, pending in enumerate(queue)
                                         if pending[1].years[0] < source.years[0]), len(queue))
                        queue.insert(position, job)
                    else:
                        queue.append(job)
    except RunStopped as stop:
        report["stop_reason"] = stop.reason
        LOG.info("Stopped normally: %s", stop.reason)
    except KeyboardInterrupt:
        stats["interrupted"] = 1
        report["stop_reason"] = "interrupted"
        LOG.warning("Interrupted; saving partial metadata and report")
    finally:
        for _, _, counters, iterator in jobs:
            iterator.close()
            counters["limited"] = report["stop_reason"] != "completed" and not counters["complete"]
            counters["missing_abstract_ratio"] = counters["missing_abstract"]/max(1, counters["seen"])
        state.close()
        for client in clients.values():
            client.close()
        if zotero:
            zotero.close()
        report["stats"] = stats
        report["elapsed_seconds"] = round(time.monotonic()-budget.started, 3)
        destination = Path(config.report_file)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix+".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)
    return stats


def run_from_file(path: str, *, max_matches=None, max_papers=None, max_seconds=None,
                  scheduling=None, source_batch_size=None, metadata_only=None) -> dict[str, int]:
    config = load_config(path)
    overrides = {k: v for k, v in locals().copy().items() if k not in {"path", "config"} and v is not None}
    return run(replace(config, **overrides))
