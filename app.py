"""Research Discovery Hub — end-to-end Information Retrieval assignment.

Run with: streamlit run app.py
All operations (acquisition, preprocessing, indexing, searching, ranking,
recommendations and evaluation) are initiated through this Streamlit UI.
"""

from __future__ import annotations

import csv
import hashlib
import html
import ipaddress
import json
import math
import re
import socket
import sqlite3
import time
import urllib.parse
import urllib.robotparser
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st
from bs4 import BeautifulSoup


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
OUTPUT_DIR = APP_DIR / "output"
DB_PATH = OUTPUT_DIR / "research_discovery.db"
INDEX_PATH = OUTPUT_DIR / "index_snapshot.json"
USER_AGENT = "ResearchDiscoveryHub/1.0 (educational IR assignment)"

STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "been", "being", "but", "by", "can",
    "for", "from", "has", "have", "if", "in", "into", "is", "it", "its", "of", "on", "or",
    "our", "that", "the", "their", "this", "to", "was", "were", "will", "with", "within",
    "we", "you", "your", "about", "across", "after", "all", "also", "among", "any", "both",
    "does", "each", "how", "more", "not", "other", "than", "these", "they", "which", "while",
}
CATEGORY_TERMS = {
    "Machine Learning": {"learning", "neural", "model", "classification", "deep", "algorithm", "ai"},
    "Information Retrieval": {"retrieval", "search", "ranking", "query", "index", "document", "bm25"},
    "Data Engineering": {"data", "pipeline", "database", "stream", "warehouse", "processing"},
    "Computer Vision": {"image", "vision", "visual", "detection", "segmentation"},
    "Natural Language Processing": {"language", "text", "token", "transformer", "semantic", "nlp"},
}


def ensure_dirs() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    OUTPUT_DIR.mkdir(exist_ok=True)


@st.cache_resource
def connection() -> sqlite3.Connection:
    ensure_dirs()
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    initialise_database(conn)
    return conn


def initialise_database(conn: sqlite3.Connection) -> None:
    """Create the local schema; kept separate so core logic is easy to test."""
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE IF NOT EXISTS documents (
            doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            source TEXT NOT NULL,
            author TEXT,
            published TEXT,
            raw_text TEXT NOT NULL,
            clean_text TEXT NOT NULL,
            keywords TEXT NOT NULL,
            category TEXT NOT NULL,
            content_hash TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS links (
            source_doc_id INTEGER NOT NULL,
            target_url TEXT NOT NULL,
            FOREIGN KEY(source_doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE,
            UNIQUE(source_doc_id, target_url)
        );
        CREATE TABLE IF NOT EXISTS feedback (
            user_id TEXT NOT NULL,
            doc_id INTEGER NOT NULL,
            score INTEGER NOT NULL CHECK(score IN (-1, 1)),
            created_at TEXT NOT NULL,
            PRIMARY KEY(user_id, doc_id),
            FOREIGN KEY(doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
        );
        """
    )
    conn.commit()


def normalise_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value.strip())
    if not parsed.scheme:
        value = "https://" + value
        parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must be an absolute public HTTP(S) URL.")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing embedded credentials are not supported.")
    path = parsed.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))


def stem(token: str) -> str:
    """A deliberately light stemmer; it keeps the project dependency-light."""
    # First remove ordinary plural inflections, then apply one derivational
    # suffix.  Keeping ``ment`` intact prevents document -> docu.
    if token.endswith(("ies", "ied")) and len(token) > 5:
        token = token[:-3] + "y"
    elif token.endswith("es") and len(token) > 4:
        token = token[:-2]
    elif token.endswith("s") and len(token) > 3:
        token = token[:-1]
    for suffix in ("ingly", "ation", "edly", "ing", "ed"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            return token[:-len(suffix)]
    return token


def tokenize(text: str, remove_stops: bool = True, use_stemming: bool = False) -> list[str]:
    terms = re.findall(r"[A-Za-z][A-Za-z0-9_-]{1,}", text.lower())
    if remove_stops:
        terms = [term for term in terms if term not in STOP_WORDS]
    return [stem(term) for term in terms] if use_stemming else terms


def extract_keywords(text: str, limit: int = 8) -> list[str]:
    return [term for term, _ in Counter(tokenize(text)).most_common(limit)]


def classify(text: str) -> str:
    terms = set(tokenize(text))
    scores = {category: len(terms & vocabulary) for category, vocabulary in CATEGORY_TERMS.items()}
    category, score = max(scores.items(), key=lambda item: item[1])
    return category if score else "General Research"


def text_fingerprint(text: str) -> set[str]:
    tokens = tokenize(text)
    if len(tokens) < 3:
        return set(tokens)
    return {" ".join(tokens[i:i + 3]) for i in range(len(tokens) - 2)}


def is_near_duplicate(conn: sqlite3.Connection, text: str, threshold: float = 0.86) -> int | None:
    """Jaccard shingle comparison catches obvious near duplicates before insert."""
    candidate = text_fingerprint(text)
    if not candidate:
        return None
    for row in conn.execute("SELECT doc_id, raw_text FROM documents"):
        existing = text_fingerprint(row["raw_text"])
        union = candidate | existing
        if union and len(candidate & existing) / len(union) >= threshold:
            return int(row["doc_id"])
    return None


def add_document(conn: sqlite3.Connection, item: dict[str, Any]) -> tuple[str, int | None]:
    raw_text = re.sub(r"\s+", " ", item.get("raw_text", "")).strip()
    title = re.sub(r"\s+", " ", item.get("title", "Untitled document")).strip()
    try:
        url = normalise_url(item.get("url", ""))
    except ValueError:
        return "invalid_url", None
    if not raw_text or len(raw_text) < 30:
        return "skipped", None
    content_hash = hashlib.sha256(" ".join(tokenize(raw_text)).encode()).hexdigest()
    if conn.execute("SELECT 1 FROM documents WHERE url = ?", (url,)).fetchone():
        return "duplicate_url", None
    if conn.execute("SELECT 1 FROM documents WHERE content_hash = ?", (content_hash,)).fetchone():
        return "duplicate_content", None
    similar_to = is_near_duplicate(conn, raw_text)
    if similar_to is not None:
        return "near_duplicate", similar_to
    clean_text = " ".join(tokenize(raw_text))
    cursor = conn.execute(
        """
        INSERT INTO documents(url, title, source, author, published, raw_text, clean_text,
                              keywords, category, content_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (url, title, item.get("source", "Unknown"), item.get("author", ""), item.get("published", ""),
         raw_text, clean_text, json.dumps(extract_keywords(raw_text)), classify(raw_text), content_hash,
         datetime.now(timezone.utc).isoformat()),
    )
    doc_id = int(cursor.lastrowid)
    for link in item.get("links", []):
        try:
            conn.execute("INSERT OR IGNORE INTO links(source_doc_id, target_url) VALUES (?, ?)",
                         (doc_id, normalise_url(link)))
        except ValueError:
            pass
    conn.commit()
    return "added", doc_id


def load_documents(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM documents ORDER BY doc_id").fetchall()
    documents = []
    for row in rows:
        value = dict(row)
        value["keywords"] = json.loads(value["keywords"])
        documents.append(value)
    return documents


def graph_pagerank(conn: sqlite3.Connection, documents: list[dict[str, Any]], damping: float = 0.85,
                   iterations: int = 30) -> dict[int, float]:
    """PageRank over crawled hyperlinks whose target is present in the corpus."""
    ids_by_url = {doc["url"]: doc["doc_id"] for doc in documents}
    outgoing: dict[int, set[int]] = defaultdict(set)
    for row in conn.execute("SELECT source_doc_id, target_url FROM links"):
        target = ids_by_url.get(row["target_url"])
        if target is not None and target != row["source_doc_id"]:
            outgoing[int(row["source_doc_id"])].add(target)
    ids = list(ids_by_url.values())
    if not ids:
        return {}
    values = {doc_id: 1 / len(ids) for doc_id in ids}
    for _ in range(iterations):
        next_values = {doc_id: (1 - damping) / len(ids) for doc_id in ids}
        dangling = sum(values[doc_id] for doc_id in ids if not outgoing[doc_id])
        for doc_id in ids:
            next_values[doc_id] += damping * dangling / len(ids)
        for source, targets in outgoing.items():
            for target in targets:
                next_values[target] += damping * values[source] / len(targets)
        values = next_values
    return values


def build_index(conn: sqlite3.Connection, documents: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    documents = documents if documents is not None else load_documents(conn)
    postings: dict[str, dict[int, int]] = defaultdict(dict)
    lengths: dict[int, int] = {}
    for doc in documents:
        # Light stemming avoids avoidable vocabulary mismatch such as
        # document/documents and recommendation/recommender.
        counts = Counter(tokenize(doc["clean_text"], remove_stops=False, use_stemming=True))
        lengths[doc["doc_id"]] = sum(counts.values())
        for term, frequency in counts.items():
            postings[term][doc["doc_id"]] = frequency
    pagerank = graph_pagerank(conn, documents)
    index = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "document_count": len(documents),
        "average_length": (sum(lengths.values()) / len(lengths)) if lengths else 0,
        "postings": {term: {str(doc_id): tf for doc_id, tf in hits.items()} for term, hits in postings.items()},
        "lengths": {str(doc_id): value for doc_id, value in lengths.items()},
        "pagerank": {str(doc_id): value for doc_id, value in pagerank.items()},
    }
    ensure_dirs()
    INDEX_PATH.write_text(json.dumps(index, indent=2), encoding="utf-8")
    return index


def get_index(conn: sqlite3.Connection) -> dict[str, Any]:
    if not INDEX_PATH.exists():
        return build_index(conn)
    try:
        index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
        count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        return index if index.get("document_count") == count else build_index(conn)
    except (OSError, json.JSONDecodeError):
        return build_index(conn)


def boolean_candidates(query: str, postings: dict[str, dict[str, int]], documents: list[dict[str, Any]]) -> set[int]:
    """Evaluate phrases and AND/OR/NOT filters (NOT > AND > OR)."""
    all_ids = {doc["doc_id"] for doc in documents}
    tokens = re.findall(r'"[^"]+"|\b(?:AND|OR|NOT)\b|[A-Za-z][A-Za-z0-9_-]*', query,
                        flags=re.IGNORECASE)
    if not tokens:
        return all_ids

    # Split on OR first. Within each group, adjacent operands and explicit AND
    # are intersections; NOT complements the following operand.
    groups: list[list[str]] = [[]]
    for token in tokens:
        if token.upper() == "OR":
            groups.append([])
        else:
            groups[-1].append(token)

    result: set[int] = set()
    for group in groups:
        current: set[int] | None = None
        negate = False
        for token in group:
            upper = token.upper()
            if upper == "AND":
                continue
            if upper == "NOT":
                negate = not negate
                continue
            if token.startswith('"') and token.endswith('"'):
                phrase = token[1:-1].lower()
                hits = {doc["doc_id"] for doc in documents if phrase in doc["raw_text"].lower()}
            else:
                hits = {int(doc_id) for doc_id in postings.get(stem(token.lower()), {})}
            if negate:
                hits = all_ids - hits
                negate = False
            current = hits if current is None else current & hits
        if current is not None:
            result |= current
    return result


def search(query: str, conn: sqlite3.Connection, top_k: int = 10, rank_weight: float = 0.20) -> list[dict[str, Any]]:
    documents = load_documents(conn)
    if not documents or not query.strip():
        return []
    index = get_index(conn)
    postings = index["postings"]
    query_terms = [term for term in tokenize(query, use_stemming=True)
                   if term not in {"and", "or", "not"}]
    candidates = boolean_candidates(query, postings, documents)
    if not candidates:
        return []
    n_docs = len(documents)
    avg_length = float(index["average_length"]) or 1
    k1, b = 1.5, 0.75
    scores: dict[int, float] = defaultdict(float)
    for term in query_terms:
        hits = postings.get(term, {})
        df = len(hits)
        if not df:
            continue
        idf = math.log(1 + (n_docs - df + 0.5) / (df + 0.5))
        for doc_id_string, tf in hits.items():
            doc_id = int(doc_id_string)
            if doc_id not in candidates:
                continue
            length = int(index["lengths"].get(doc_id_string, 0))
            scores[doc_id] += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * length / avg_length))
    # Negative-only filters have no positive lexical term to score; retain their
    # candidates and let PageRank (if enabled) provide a deterministic order.
    if not scores and candidates:
        scores = defaultdict(float, {doc_id: 0.0 for doc_id in candidates})
    max_bm25 = max(scores.values(), default=1.0) or 1.0
    max_pr = max((float(value) for value in index["pagerank"].values()), default=1.0) or 1.0
    by_id = {doc["doc_id"]: doc for doc in documents}
    results = []
    for doc_id in candidates:
        if doc_id not in scores:
            continue
        bm25 = scores[doc_id]
        pr = float(index["pagerank"].get(str(doc_id), 1 / max(n_docs, 1)))
        final = (1 - rank_weight) * (bm25 / max_bm25) + rank_weight * (pr / max_pr)
        result = dict(by_id[doc_id])
        result.update({"bm25": round(bm25, 4), "pagerank": round(pr, 5), "score": round(final, 4)})
        results.append(result)
    return sorted(results, key=lambda result: result["score"], reverse=True)[:top_k]


def tfidf_vectors(documents: list[dict[str, Any]]) -> dict[int, dict[str, float]]:
    counts = {doc["doc_id"]: Counter(tokenize(doc["clean_text"], remove_stops=False, use_stemming=True)) for doc in documents}
    df = Counter(term for value in counts.values() for term in value)
    n_docs = max(len(documents), 1)
    vectors: dict[int, dict[str, float]] = {}
    for doc_id, value in counts.items():
        length = math.sqrt(sum((tf * math.log((n_docs + 1) / (df[term] + 1) + 1)) ** 2 for term, tf in value.items())) or 1
        vectors[doc_id] = {term: (tf * math.log((n_docs + 1) / (df[term] + 1) + 1)) / length for term, tf in value.items()}
    return vectors


def cosine(first: dict[str, float], second: dict[str, float]) -> float:
    small, large = (first, second) if len(first) < len(second) else (second, first)
    return sum(value * large.get(term, 0) for term, value in small.items())


def recommendations(conn: sqlite3.Connection, selected_id: int, user_id: str, top_k: int = 5) -> list[dict[str, Any]]:
    docs = load_documents(conn)
    vectors = tfidf_vectors(docs)
    by_id = {doc["doc_id"]: doc for doc in docs}
    preferred = {row["doc_id"] for row in conn.execute(
        "SELECT doc_id FROM feedback WHERE user_id = ? AND score = 1", (user_id,)
    )}
    disliked = {row["doc_id"] for row in conn.execute(
        "SELECT doc_id FROM feedback WHERE user_id = ? AND score = -1", (user_id,)
    )}
    profile = dict(vectors.get(selected_id, {}))
    for doc_id in preferred - {selected_id}:
        for term, weight in vectors.get(doc_id, {}).items():
            profile[term] = profile.get(term, 0) + 0.35 * weight
    for doc_id in disliked - {selected_id}:
        for term, weight in vectors.get(doc_id, {}).items():
            profile[term] = profile.get(term, 0) - 0.20 * weight
    profile_norm = math.sqrt(sum(value * value for value in profile.values())) or 1
    profile = {term: value / profile_norm for term, value in profile.items()}
    # A lightweight collaborative signal: co-likes by demo/session users if they exist.
    selected_likers = {row["user_id"] for row in conn.execute(
        "SELECT user_id FROM feedback WHERE doc_id = ? AND score = 1", (selected_id,)
    )}
    rows = conn.execute("SELECT user_id, doc_id FROM feedback WHERE score = 1").fetchall()
    co_like = Counter(row["doc_id"] for row in rows if row["user_id"] in selected_likers and row["doc_id"] != selected_id)
    collaborative_max = max(co_like.values(), default=0)
    values = []
    for doc in docs:
        doc_id = doc["doc_id"]
        if doc_id == selected_id or doc_id in disliked:
            continue
        content_score = max(0.0, cosine(profile, vectors.get(doc_id, {})))
        collaborative_score = co_like[doc_id] / collaborative_max if collaborative_max else 0.0
        hybrid = 0.8 * content_score + 0.2 * collaborative_score
        row = dict(doc)
        row.update({"content_similarity": round(content_score, 4),
                    "collaborative_signal": round(collaborative_score, 4),
                    "hybrid_score": round(hybrid, 4)})
        values.append(row)
    return sorted(values, key=lambda item: item["hybrid_score"], reverse=True)[:top_k]


def parse_csv_items(uploaded: Any) -> list[dict[str, Any]]:
    table = pd.read_csv(uploaded)
    required = {"url", "title", "raw_text"}
    if not required.issubset(table.columns):
        raise ValueError("CSV must contain url, title, and raw_text columns.")
    return table.fillna("").to_dict("records")


def fetch_crossref(query: str, rows: int) -> list[dict[str, Any]]:
    response = requests.get(
        "https://api.crossref.org/works", params={"query": query, "rows": rows, "select": "DOI,title,author,published,container-title,abstract"},
        headers={"User-Agent": USER_AGENT}, timeout=20,
    )
    response.raise_for_status()
    items = []
    for record in response.json()["message"]["items"]:
        title = " ".join(record.get("title", [])) or "Untitled Crossref work"
        author = ", ".join(" ".join(filter(None, [person.get("given"), person.get("family")])) for person in record.get("author", [])[:3])
        date_parts = record.get("published", {}).get("date-parts", [[""]])[0]
        published = "-".join(str(value) for value in date_parts)
        abstract = BeautifulSoup(html.unescape(record.get("abstract", "")), "html.parser").get_text(" ")
        container = " ".join(record.get("container-title", []))
        # Crossref metadata is indexed as a concise document profile if an abstract is absent.
        text = abstract or f"{title}. Published in {container}. Authors: {author}. Topic query: {query}."
        items.append({"url": f"https://doi.org/{record['DOI']}", "title": title, "source": "Crossref API",
                      "author": author, "published": published, "raw_text": text, "links": []})
    return items


def safe_get(url: str, *, timeout: int, max_redirects: int = 3) -> requests.Response:
    """Fetch an HTTP(S) URL while revalidating every redirect target."""
    current = normalise_url(url)
    for _ in range(max_redirects + 1):
        if not is_safe_public_url(current):
            raise requests.RequestException(f"blocked non-public or invalid URL: {current}")
        response = requests.get(current, headers={"User-Agent": USER_AGENT}, timeout=timeout,
                                allow_redirects=False)
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get("Location")
        if not location:
            return response
        current = normalise_url(urllib.parse.urljoin(current, location))
    raise requests.TooManyRedirects(f"more than {max_redirects} redirects for {url}")


def can_fetch(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
    try:
        parser = urllib.robotparser.RobotFileParser()
        response = safe_get(robots_url, timeout=10)
        if response.status_code >= 400:
            return True
        parser.set_url(robots_url)
        parser.parse(response.text.splitlines())
        return parser.can_fetch(USER_AGENT, url)
    except (requests.RequestException, ValueError):
        # A failed robots retrieval should not silently block a permitted manual seed.
        return True


def is_safe_public_url(url: str) -> bool:
    """Block localhost/private targets when the crawler is deployed publicly."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return False
    host = parsed.hostname.lower()
    if host == "localhost" or host.endswith(".localhost"):
        return False
    try:
        addresses = {record[4][0] for record in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
        return bool(addresses) and all(ipaddress.ip_address(address).is_global for address in addresses)
    except (OSError, ValueError):
        return False


def crawl(seeds: list[str], depth: int, limit: int, status: Any) -> tuple[list[dict[str, Any]], list[str]]:
    queue: deque[tuple[str, int]] = deque()
    warnings: list[str] = []
    for seed in seeds:
        if not seed.strip():
            continue
        try:
            queue.append((normalise_url(seed), 0))
        except ValueError:
            warnings.append(f"invalid seed URL: {seed.strip()}")
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    while queue and len(items) < limit:
        url, current_depth = queue.popleft()
        if url in seen:
            continue
        seen.add(url)
        if not is_safe_public_url(url):
            warnings.append(f"blocked non-public or invalid URL: {url}")
            continue
        if not can_fetch(url):
            warnings.append(f"robots.txt disallowed: {url}")
            continue
        try:
            response = safe_get(url, timeout=15)
            response.raise_for_status()
            if "text/html" not in response.headers.get("Content-Type", ""):
                warnings.append(f"not HTML: {url}")
                continue
            soup = BeautifulSoup(response.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
                tag.decompose()
            title = soup.title.get_text(" ", strip=True) if soup.title else url
            paragraphs = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
            raw_text = " ".join(value for value in paragraphs if len(value) > 30)
            links = []
            for anchor in soup.find_all("a", href=True):
                linked = urllib.parse.urljoin(url, anchor["href"])
                parsed = urllib.parse.urlsplit(linked)
                if parsed.scheme in {"http", "https"}:
                    clean_link = normalise_url(linked)
                    links.append(clean_link)
                    if current_depth < depth and clean_link not in seen:
                        queue.append((clean_link, current_depth + 1))
            if raw_text:
                items.append({"url": url, "title": title, "source": "Web crawl", "author": "",
                              "published": "", "raw_text": raw_text, "links": links[:100]})
                status.progress(min(len(items) / limit, 1.0), text=f"Fetched {len(items)}/{limit}: {title[:55]}")
            time.sleep(0.35)
        except requests.RequestException as error:
            warnings.append(f"fetch failed ({url}): {error}")
    return items, warnings


def metrics_for_ranking(ranked: list[str], relevance: dict[str, float], k: int) -> dict[str, float]:
    total_relevant = sum(value > 0 for value in relevance.values())
    binary_all = [1 if relevance.get(doc_id, 0) > 0 else 0 for doc_id in ranked]
    binary_at_k = binary_all[:k]
    relevant_retrieved = sum(binary_all)
    relevant_at_k = sum(binary_at_k)
    precision = relevant_retrieved / len(ranked) if ranked else 0.0
    recall = relevant_retrieved / total_relevant if total_relevant else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    ap_parts, relevant_seen = [], 0
    for position, is_relevant in enumerate(binary_all, start=1):
        if is_relevant:
            relevant_seen += 1
            ap_parts.append(relevant_seen / position)
    ap = sum(ap_parts) / total_relevant if total_relevant else 0.0
    rr = next((1 / position for position, is_relevant in enumerate(binary_all, 1) if is_relevant), 0.0)
    retrieved = ranked[:k]
    dcg = sum((2 ** relevance.get(doc_id, 0) - 1) / math.log2(position + 1)
              for position, doc_id in enumerate(retrieved, 1))
    ideal = sorted(relevance.values(), reverse=True)[:k]
    idcg = sum((2 ** value - 1) / math.log2(position + 1) for position, value in enumerate(ideal, 1))
    return {"Precision": precision, "Recall": recall, "F1": f1,
            "Precision@K": relevant_at_k / k if k else 0.0,
            "Recall@K": relevant_at_k / total_relevant if total_relevant else 0.0,
            "AP": ap, "MRR": rr, "NDCG@K": dcg / idcg if idcg else 0.0}


def evaluation_table(conn: sqlite3.Connection, qrels: pd.DataFrame, k: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    required = {"query", "url", "relevance"}
    if not required.issubset(qrels.columns):
        raise ValueError("Qrels CSV must contain query, url, relevance.")
    document_count = int(conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0])
    per_query = []
    for query, group in qrels.groupby("query"):
        relevance = {normalise_url(row.url): float(row.relevance) for row in group.itertuples()}
        for strategy, weight in (("BM25", 0.0), ("BM25 + PageRank", 0.20)):
            ranked = [item["url"] for item in search(str(query), conn, top_k=document_count,
                                                      rank_weight=weight)]
            value = metrics_for_ranking(ranked, relevance, k)
            per_query.append({"Query": query, "Strategy": strategy, **value})
    detailed = pd.DataFrame(per_query)
    summary = detailed.groupby("Strategy", as_index=False)[["Precision", "Recall", "F1", "Precision@K", "Recall@K", "AP", "MRR", "NDCG@K"]].mean()
    summary = summary.rename(columns={"AP": "MAP"})
    return detailed, summary


def top_terms(documents: list[dict[str, Any]], limit: int = 20) -> pd.DataFrame:
    counter = Counter(term for doc in documents for term in tokenize(doc["clean_text"], remove_stops=False))
    return pd.DataFrame(counter.most_common(limit), columns=["term", "frequency"])


def header() -> None:
    st.markdown("""
    <style>
      .block-container {max-width: 1200px; padding-top: 1.6rem; padding-bottom: 2rem;}
      .hero {padding: 1.25rem 1.5rem; border-radius: 14px; background: linear-gradient(115deg,#102A43,#185E8F); color:#fff; margin-bottom:1.2rem;}
      .hero h1 {margin:0; font-size:2.05rem;} .hero p {margin:.35rem 0 0; opacity:.9;}
      [data-testid="stMetricValue"] {font-size:1.45rem;}
      .result {border:1px solid #dbe5ef; border-radius:10px; padding:1rem; margin:.7rem 0; background:#fff;}
    </style>
    <div class="hero"><h1>Research Discovery Hub</h1><p>An end-to-end Information Retrieval workbench for scholarly and technical content.</p></div>
    """, unsafe_allow_html=True)


def ingest_items(conn: sqlite3.Connection, items: list[dict[str, Any]]) -> Counter:
    outcomes: Counter = Counter()
    for item in items:
        outcome, _ = add_document(conn, item)
        outcomes[outcome] += 1
    if outcomes["added"]:
        build_index(conn)
    return outcomes


def seed_demo_links(conn: sqlite3.Connection) -> None:
    """A compact citation/link graph makes the PageRank demonstration reproducible."""
    graph = {
        "information-retrieval-basics": ["bm25-ranking", "pagerank-authority", "query-optimization", "evaluation-metrics"],
        "bm25-ranking": ["pagerank-authority", "ranking-diagnostics", "query-optimization"],
        "pagerank-authority": ["bm25-ranking", "ranking-diagnostics"],
        "web-crawling": ["deduplication", "data-pipelines"],
        "deduplication": ["web-crawling", "data-pipelines", "evaluation-metrics"],
        "text-preprocessing": ["tfidf-content-recommender", "classification", "semantic-search"],
        "tfidf-content-recommender": ["collaborative-recommender", "text-preprocessing"],
        "collaborative-recommender": ["tfidf-content-recommender", "semantic-search"],
        "evaluation-metrics": ["bm25-ranking", "ranking-diagnostics"],
        "query-optimization": ["bm25-ranking", "semantic-search"],
        "semantic-search": ["bm25-ranking", "tfidf-content-recommender"],
        "ranking-diagnostics": ["bm25-ranking", "pagerank-authority", "evaluation-metrics"],
    }
    sources = conn.execute("SELECT doc_id, url FROM documents WHERE url LIKE 'https://demo.irhub.local/%'").fetchall()
    by_slug = {row["url"].rsplit("/", 1)[-1]: row["doc_id"] for row in sources}
    for source_slug, targets in graph.items():
        if source_slug not in by_slug:
            continue
        for target_slug in targets:
            conn.execute("INSERT OR IGNORE INTO links(source_doc_id, target_url) VALUES (?, ?)",
                         (by_slug[source_slug], f"https://demo.irhub.local/{target_slug}"))
    conn.commit()


def dashboard(conn: sqlite3.Connection) -> None:
    documents = load_documents(conn)
    index = get_index(conn)
    st.subheader("Dashboard")
    st.caption("Use the sidebar in order: acquire a corpus → build the index → search and rank → recommend → evaluate.")
    categories = len({doc["category"] for doc in documents})
    links = conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
    one, two, three, four = st.columns(4)
    one.metric("Indexed documents", len(documents))
    two.metric("Vocabulary", len(index.get("postings", {})))
    three.metric("Categories", categories)
    four.metric("Crawled links", links)
    if documents:
        values = pd.DataFrame(documents).groupby("category", as_index=False).size()
        st.plotly_chart(px.bar(values, x="category", y="size", color="category", title="Corpus by automatic category",
                               labels={"size": "documents", "category": "category"}), width="stretch")
        st.info("Tip: the demo corpus is intentionally small for an explainable lab demonstration. Add web/API/CSV documents through the UI to scale the experiment.")
    else:
        st.warning("No corpus yet. Open **Acquire & Crawl** and load the demo corpus or ingest your own data.")


def acquisition(conn: sqlite3.Connection) -> None:
    st.subheader("Acquire & Crawl")
    st.caption("Sources are intentionally heterogeneous: a local CSV dataset, a public Crossref API, and standards-compliant web crawling.")
    tab_demo, tab_csv, tab_api, tab_crawl = st.tabs(["Demo dataset", "CSV dataset", "Crossref API", "Web crawler"])
    with tab_demo:
        st.write("Load the supplied research-discovery dataset (15 compact documents) for a reproducible demonstration.")
        if st.button("Load or refresh demo corpus", type="primary"):
            outcomes = ingest_items(conn, parse_csv_items(DATA_DIR / "sample_documents.csv"))
            seed_demo_links(conn)
            build_index(conn)
            st.success(f"Added {outcomes['added']} documents. Duplicate protection skipped {sum(outcomes.values()) - outcomes['added']}.")
    with tab_csv:
        upload = st.file_uploader("Upload a CSV", type="csv", help="Required columns: url, title, raw_text. Optional: source, author, published.")
        if upload and st.button("Ingest CSV"):
            try:
                outcomes = ingest_items(conn, parse_csv_items(upload))
                st.success(f"Added {outcomes['added']} documents; URL/content/near-duplicate skips: {sum(outcomes.values()) - outcomes['added']}.")
            except (ValueError, pd.errors.ParserError) as error:
                st.error(str(error))
    with tab_api:
        query = st.text_input("Crossref topic query", "information retrieval")
        rows = st.slider("Records to request", 5, 30, 10)
        if st.button("Acquire from Crossref"):
            try:
                with st.spinner("Requesting public Crossref metadata…"):
                    outcomes = ingest_items(conn, fetch_crossref(query, rows))
                st.success(f"Added {outcomes['added']} Crossref records; duplicates skipped: {sum(outcomes.values()) - outcomes['added']}.")
            except requests.RequestException as error:
                st.error(f"Crossref request failed: {error}")
    with tab_crawl:
        seeds = st.text_area("Seed URLs (one per line)", "https://en.wikipedia.org/wiki/Information_retrieval")
        col_depth, col_limit = st.columns(2)
        depth = col_depth.slider("Crawl depth", 0, 2, 0)
        limit = col_limit.slider("Maximum pages", 1, 25, 5)
        st.caption("The crawler checks robots.txt, normalizes URL fragments, waits between requests, and only collects HTML paragraphs.")
        if st.button("Start crawl"):
            progress = st.progress(0, text="Preparing crawl…")
            items, warnings = crawl(seeds.splitlines(), depth, limit, progress)
            progress.empty()
            outcomes = ingest_items(conn, items)
            st.success(f"Crawled {len(items)} pages and added {outcomes['added']} documents.")
            if warnings:
                with st.expander(f"Crawl notes ({len(warnings)})"):
                    st.code("\n".join(warnings[:30]))


def index_management(conn: sqlite3.Connection) -> None:
    st.subheader("Index Management")
    documents = load_documents(conn)
    index = get_index(conn)
    if not documents:
        st.warning("Acquire documents first.")
        return
    left, right = st.columns([1, 2])
    with left:
        st.metric("Inverted-index terms", len(index["postings"]))
        st.metric("Average document length", f"{index['average_length']:.1f} terms")
        st.metric("Snapshot", index["created_at"][:19].replace("T", " "))
        if st.button("Rebuild inverted index", type="primary"):
            index = build_index(conn)
            st.success(f"Rebuilt index with {len(index['postings'])} terms.")
        confirm_clear = st.checkbox("Confirm permanent removal of the local corpus")
        if st.button("Clear all corpus data", disabled=not confirm_clear):
            conn.execute("DELETE FROM feedback")
            conn.execute("DELETE FROM links")
            conn.execute("DELETE FROM documents")
            conn.commit()
            if INDEX_PATH.exists():
                INDEX_PATH.unlink()
            st.rerun()
    with right:
        st.write("**Storage separation:** metadata (title, URL, source, author, category) and document contents (raw/clean text) are separate database fields; the generated JSON snapshot holds the inverted index and PageRank values.")
        detail = pd.DataFrame(documents)[["doc_id", "title", "source", "category", "published", "keywords"]]
        st.dataframe(detail, width="stretch", hide_index=True)
        st.download_button("Download index snapshot", json.dumps(index, indent=2), file_name="index_snapshot.json", mime="application/json")


def search_page(conn: sqlite3.Connection) -> None:
    st.subheader("Search & Ranking")
    if not load_documents(conn):
        st.warning("Acquire and index documents first.")
        return
    query = st.text_input("Search query", placeholder='Try: "information retrieval" AND ranking')
    col_top, col_weight = st.columns(2)
    top_k = col_top.slider("Results", 3, 20, 8)
    rank_weight = col_weight.slider("PageRank influence", 0.0, 0.5, 0.20, 0.05,
                                    help="0 = lexical BM25 only; higher values blend hyperlink authority.")
    st.caption("Query support: terms, quoted phrases, AND / OR / NOT. Candidate filtering occurs before BM25 scoring, then PageRank is blended into the final score.")
    if query:
        results = search(query, conn, top_k, rank_weight)
        if not results:
            st.info("No matching documents. Try fewer Boolean constraints or add corpus documents.")
            return
        st.success(f"{len(results)} ranked results")
        for position, result in enumerate(results, 1):
            keyword_text = ", ".join(result["keywords"][:6])
            excerpt = result["raw_text"][:340] + ("…" if len(result["raw_text"]) > 340 else "")
            st.markdown(
                f"<div class='result'><b>#{position} · {html.escape(result['title'])}</b><br>"
                f"<small>{html.escape(result['source'])} · {html.escape(result['category'])} · "
                f"BM25 {result['bm25']:.3f} · PageRank {result['pagerank']:.4f} · final {result['score']:.3f}</small><br>"
                f"{html.escape(excerpt)}<br><small>Keywords: {html.escape(keyword_text)}</small></div>", unsafe_allow_html=True)
            st.link_button("Open source", result["url"], key=f"open_{result['doc_id']}")
        chart = pd.DataFrame(results)[["title", "bm25", "pagerank", "score"]].set_index("title")
        st.plotly_chart(px.bar(chart, barmode="group", title="Why the result order changes: lexical relevance + authority"), width="stretch")


def recommendation_page(conn: sqlite3.Connection) -> None:
    st.subheader("Recommendation Panel")
    docs = load_documents(conn)
    if len(docs) < 2:
        st.warning("Acquire at least two documents first.")
        return
    if "user_id" not in st.session_state:
        st.session_state.user_id = "session-" + hashlib.sha256(str(time.time()).encode()).hexdigest()[:8]
    labels = {f"{doc['doc_id']} — {doc['title'][:80]}": doc["doc_id"] for doc in docs}
    selected_label = st.selectbox("Document used as the recommendation seed", list(labels))
    selected_id = labels[selected_label]
    max_k = min(15, len(docs) - 1)
    top_k = st.slider("Number of recommendations (K)", 1, max_k, min(5, max_k))
    rating = st.radio("Your feedback on this document", ["No preference", "Relevant / save", "Not relevant"], horizontal=True)
    if st.button("Record feedback"):
        score = {"Relevant / save": 1, "Not relevant": -1}.get(rating)
        if score is None:
            conn.execute("DELETE FROM feedback WHERE user_id = ? AND doc_id = ?", (st.session_state.user_id, selected_id))
        else:
            conn.execute("INSERT OR REPLACE INTO feedback(user_id, doc_id, score, created_at) VALUES (?, ?, ?, ?)",
                         (st.session_state.user_id, selected_id, score, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        st.success("Feedback saved for this session.")
    values = recommendations(conn, selected_id, st.session_state.user_id, top_k)
    st.caption("Hybrid score = 80% TF-IDF cosine content similarity (including saved and disliked-item feedback) + 20% collaborative co-like signal when multiple user profiles are available. Items marked not relevant are excluded.")
    table = pd.DataFrame(values)[["doc_id", "title", "category", "content_similarity", "collaborative_signal", "hybrid_score"]]
    st.dataframe(table, width="stretch", hide_index=True)
    if values:
        st.plotly_chart(px.bar(table, x="title", y=["content_similarity", "collaborative_signal", "hybrid_score"],
                               barmode="group", title="Top-K recommendation explanation"), width="stretch")


def evaluation_page(conn: sqlite3.Connection) -> None:
    st.subheader("Evaluation Dashboard")
    if not load_documents(conn):
        st.warning("Acquire and index documents first.")
        return
    st.caption("Upload relevance judgments with columns `query`, `url`, `relevance` (0 = non-relevant; larger values = more relevant). The supplied qrels are aligned to the demo corpus.")
    use_demo = st.checkbox("Use supplied demo relevance judgments", value=True)
    upload = None if use_demo else st.file_uploader("Qrels CSV", type="csv")
    k = st.slider("Evaluation cutoff K", 3, 15, 5)
    if st.button("Run evaluation", type="primary"):
        try:
            qrels = pd.read_csv(DATA_DIR / "sample_qrels.csv") if use_demo else pd.read_csv(upload)
            detailed, summary = evaluation_table(conn, qrels, k)
            st.markdown("#### Comparative retrieval metrics")
            st.dataframe(summary.style.format({column: "{:.3f}" for column in summary.columns if column != "Strategy"}),
                         width="stretch", hide_index=True)
            st.plotly_chart(px.bar(summary.melt(id_vars="Strategy", var_name="Metric", value_name="Score"),
                                   x="Metric", y="Score", color="Strategy", barmode="group",
                                   title="BM25 versus BM25 + PageRank"), width="stretch")
            with st.expander("Per-query results"):
                st.dataframe(detailed.style.format({column: "{:.3f}" for column in detailed.columns if column not in {"Query", "Strategy"}}),
                             width="stretch", hide_index=True)
            st.download_button("Download evaluation table", summary.to_csv(index=False), "evaluation_summary.csv", "text/csv")
        except (ValueError, pd.errors.ParserError, TypeError) as error:
            st.error(f"Evaluation could not run: {error}")


def mining_page(conn: sqlite3.Connection) -> None:
    st.subheader("Text Preprocessing & Mining")
    docs = load_documents(conn)
    if not docs:
        st.warning("Acquire documents first.")
        return
    selected = st.selectbox("Document profile", docs, format_func=lambda item: f"{item['doc_id']} — {item['title'][:90]}")
    option = st.selectbox("Feature strategy", ["Lowercase + tokenization", "Stop-word removal", "Stop-word removal + stemming"])
    remove_stops = option != "Lowercase + tokenization"
    use_stemming = option.endswith("stemming")
    terms = tokenize(selected["raw_text"], remove_stops=remove_stops, use_stemming=use_stemming)
    original = tokenize(selected["raw_text"], remove_stops=False)
    profile_left, profile_right = st.columns(2)
    profile_left.metric("Tokens", len(terms))
    profile_right.metric("Vocabulary", len(set(terms)))
    st.write(f"**Class:** {selected['category']}  \\n+**Extracted keywords:** {', '.join(extract_keywords(' '.join(terms)))}")
    frequency = pd.DataFrame(Counter(terms).most_common(15), columns=["term", "frequency"])
    st.plotly_chart(px.bar(frequency, x="term", y="frequency", title=f"Top features — {option}"), width="stretch")
    comparison = pd.DataFrame([
        {"Strategy": "Tokenization", "Tokens": len(original), "Vocabulary": len(set(original))},
        {"Strategy": "Stop-word removal", "Tokens": len(tokenize(selected["raw_text"])), "Vocabulary": len(set(tokenize(selected["raw_text"])))},
        {"Strategy": "Stop-word + stemming", "Tokens": len(tokenize(selected["raw_text"], use_stemming=True)), "Vocabulary": len(set(tokenize(selected["raw_text"], use_stemming=True)))}
    ])
    st.dataframe(comparison, width="stretch", hide_index=True)
    terms_df = top_terms(docs)
    st.plotly_chart(px.bar(terms_df, x="term", y="frequency", title="Corpus feature distribution (index vocabulary)"), width="stretch")


def analytics_page(conn: sqlite3.Connection) -> None:
    st.subheader("Performance Analytics")
    docs = load_documents(conn)
    if not docs:
        st.warning("Acquire documents first.")
        return
    table = pd.DataFrame(docs)
    table["tokens"] = table["clean_text"].map(lambda text: len(tokenize(text, remove_stops=False)))
    source_counts = table.groupby("source", as_index=False).size()
    left, right = st.columns(2)
    with left:
        st.plotly_chart(px.pie(source_counts, names="source", values="size", title="Acquisition-source mix"), width="stretch")
    with right:
        st.plotly_chart(px.histogram(table, x="tokens", nbins=12, title="Document-length distribution"), width="stretch")
    ranking = graph_pagerank(conn, docs)
    rank_table = pd.DataFrame([{"doc_id": doc["doc_id"], "title": doc["title"], "PageRank": ranking.get(doc["doc_id"], 0)} for doc in docs])
    rank_table = rank_table.sort_values("PageRank", ascending=False).head(10)
    st.plotly_chart(px.bar(rank_table, x="title", y="PageRank", title="Link-authority distribution"), width="stretch")
    st.dataframe(table[["doc_id", "title", "source", "category", "tokens", "keywords"]], width="stretch", hide_index=True)


def main() -> None:
    st.set_page_config(page_title="Research Discovery Hub", page_icon="🔎", layout="wide")
    conn = connection()
    header()
    pages = {
        "Dashboard": dashboard,
        "Acquire & Crawl": acquisition,
        "Index Management": index_management,
        "Search & Ranking": search_page,
        "Recommendations": recommendation_page,
        "Evaluation": evaluation_page,
        "Analytics & Mining": mining_page,
        "Performance Analytics": analytics_page,
    }
    st.sidebar.title("IR workflow")
    page = st.sidebar.radio("Navigate", list(pages))
    st.sidebar.divider()
    st.sidebar.caption("Stack: Streamlit · SQLite · BM25 · PageRank · TF-IDF cosine · standard IR metrics")
    pages[page](conn)


if __name__ == "__main__":
    main()
