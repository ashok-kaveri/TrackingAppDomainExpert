from __future__ import annotations
import logging
from typing import Optional

import chromadb
from langchain_ollama import OllamaEmbeddings
from langchain_chroma import Chroma
from langchain_core.documents import Document

import config

logger = logging.getLogger(__name__)


def get_embeddings() -> OllamaEmbeddings:
    return OllamaEmbeddings(
        model=config.EMBEDDING_MODEL,
        base_url=config.OLLAMA_BASE_URL,
    )


_vectorstore_instance: Optional[Chroma] = None


def get_vectorstore() -> Chroma:
    global _vectorstore_instance
    if _vectorstore_instance is None:
        # collection_metadata configures the HNSW index.
        # hnsw:space=cosine is correct for nomic-embed-text (normalised embeddings).
        # Without explicit settings, Python 3.14 + chromadb allocates a huge
        # link_lists.bin sparse file (60-150GB) due to an integer-overflow in
        # hnswlib's max_elements calculation — this config keeps it sane.
        _vectorstore_instance = Chroma(
            collection_name=config.CHROMA_COLLECTION,
            embedding_function=get_embeddings(),
            persist_directory=config.CHROMA_PATH,
            collection_metadata={
                "hnsw:space": "cosine",
                "hnsw:construction_ef": 100,
                "hnsw:search_ef": 100,
                "hnsw:M": 16,
                "hnsw:batch_size": 100,
                "hnsw:sync_threshold": 1000,
            },
        )
    return _vectorstore_instance


def _reset_vectorstore() -> None:
    """Reset the cached vectorstore instance (used after clear_collection())."""
    global _vectorstore_instance
    _vectorstore_instance = None


# Smaller batch size prevents ChromaDB HNSW from pre-allocating huge link_lists.bin
_CHROMA_BATCH_SIZE = 500


def _deduplicate(documents: list[Document]) -> list[Document]:
    """Remove exact duplicate chunks (same first 200 chars) before inserting."""
    seen: set[str] = set()
    result: list[Document] = []
    for doc in documents:
        key = doc.page_content.strip()[:200]
        if key not in seen:
            seen.add(key)
            result.append(doc)
    removed = len(documents) - len(result)
    if removed:
        logger.info("Deduplication: removed %d duplicate chunks (%d → %d)", removed, len(documents), len(result))
    return result


def add_documents(documents: list[Document]) -> None:
    """Embed and store documents in ChromaDB, deduplicating and batching."""
    if not documents:
        logger.debug("add_documents called with empty list — skipping")
        return
    documents = _deduplicate(documents)
    vectorstore = get_vectorstore()
    total = len(documents)
    for start in range(0, total, _CHROMA_BATCH_SIZE):
        batch = documents[start: start + _CHROMA_BATCH_SIZE]
        vectorstore.add_documents(batch)
        logger.info(
            "Embedded batch %d–%d / %d",
            start + 1,
            min(start + _CHROMA_BATCH_SIZE, total),
            total,
        )
    logger.info("Added %d documents to ChromaDB", total)


def clear_collection() -> None:
    """Delete and recreate the ChromaDB collection."""
    global _vectorstore_instance
    client = chromadb.PersistentClient(path=config.CHROMA_PATH)
    try:
        client.delete_collection(config.CHROMA_COLLECTION)
        logger.info("Cleared ChromaDB collection: %s", config.CHROMA_COLLECTION)
    except Exception:
        logger.debug("Collection %s did not exist — nothing to clear", config.CHROMA_COLLECTION)
    _reset_vectorstore()


def upsert_documents(documents: list[Document], ids: list[str]) -> None:
    """Add or replace documents by stable ID."""
    if not documents:
        logger.debug("upsert_documents called with empty list — skipping")
        return
    if len(documents) != len(ids):
        raise ValueError(
            f"upsert_documents: len(documents)={len(documents)} != len(ids)={len(ids)}"
        )
    vectorstore = get_vectorstore()
    try:
        vectorstore.delete(ids=ids)
        logger.debug("Deleted %d existing document(s) before upsert", len(ids))
    except Exception as exc:
        logger.debug("Delete before upsert raised (OK on first run): %s", exc)
    vectorstore.add_documents(documents, ids=ids)
    logger.info("Upserted %d document(s) into ChromaDB", len(documents))


def get_source_count(source_type: str) -> int:
    """Return the number of chunks stored with a given source_type metadata value."""
    try:
        vs = get_vectorstore()
        results = vs._collection.get(
            where={"source_type": source_type},
            include=[],
        )
        return len(results.get("ids", []))
    except Exception as e:
        logger.debug("get_source_count(%r) failed: %s", source_type, e)
        return 0


def delete_by_source_type(source_type: str) -> int:
    """Delete all chunks whose metadata source_type equals `source_type`."""
    try:
        vs = get_vectorstore()
        results = vs._collection.get(
            where={"source_type": source_type},
            include=[],
        )
        ids = results.get("ids", [])
        if ids:
            vs._collection.delete(ids=ids)
            logger.info("Deleted %d chunk(s) with source_type=%r", len(ids), source_type)
        return len(ids)
    except Exception as e:
        logger.warning("delete_by_source_type(%r) failed: %s", source_type, e)
        return 0


def search(query: str, k: int = 5) -> list[Document]:
    """Return top-k documents most relevant to the query."""
    try:
        vectorstore = get_vectorstore()
        return vectorstore.similarity_search(query, k=k)
    except Exception as e:
        err_str = str(e).lower()
        if "does not exist" in err_str or "collection" in err_str or "no documents" in err_str:
            return []
        logger.exception("Vector store search failed for query: %r", query)
        raise


def search_filtered(
    query: str,
    k: int = 5,
    source_type: Optional[str] = None,
    category: Optional[str] = None,
) -> list[Document]:
    """Return top-k documents filtered by optional metadata constraints."""
    try:
        vectorstore = get_vectorstore()

        conditions: dict = {}
        if source_type:
            conditions["source_type"] = source_type
        if category:
            conditions["category"] = category

        if not conditions:
            where: Optional[dict] = None
        elif len(conditions) == 1:
            where = conditions
        else:
            where = {"$and": [{key: val} for key, val in conditions.items()]}

        return vectorstore.similarity_search(query, k=k, filter=where)
    except Exception as e:
        err_str = str(e).lower()
        if "does not exist" in err_str or "collection" in err_str or "no documents" in err_str:
            return []
        logger.exception(
            "Filtered vector store search failed for query: %r (source_type=%r, category=%r)",
            query, source_type, category,
        )
        raise
