from __future__ import annotations
import logging
from collections import defaultdict

from langchain_anthropic import ChatAnthropic
from langchain_core.documents import Document
from langchain_core.messages import HumanMessage

import config
from rag.vectorstore import search
from rag.prompts import QA_PROMPT, CONDENSE_QUESTION_PROMPT

# Human-readable labels for each source_type stored in ChromaDB metadata.
_SOURCE_LABELS: dict[str, str] = {
    "wiki":              "Internal Wiki (Product, Engineering & Bug Knowledge)",
    "pluginhive_docs":   "PluginHive Official Documentation",
    "pluginhive_seeds":  "PluginHive Tracking App — Knowledge Base & Guides",
    "pdf":               "Test Cases & Acceptance Criteria (PDF)",
    "codebase":          "Automation Codebase (Playwright / TypeScript)",
    "automation":        "Automation Codebase",
    "backend":           "Backend Source Code",
    "frontend":          "Frontend Source Code",
    "app":               "Tracking App UI Knowledge",
    "shopify_app_store": "Shopify App Store Listing",
}

logger = logging.getLogger(__name__)

_llm_instance: ChatAnthropic | None = None


def get_llm() -> ChatAnthropic:
    global _llm_instance
    if _llm_instance is None:
        if not config.ANTHROPIC_API_KEY:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set.\n"
                "Add it to your .env file:  ANTHROPIC_API_KEY=sk-ant-..."
            )
        _llm_instance = ChatAnthropic(
            model=config.DOMAIN_EXPERT_MODEL,
            api_key=config.ANTHROPIC_API_KEY,
            temperature=0.1,
            max_tokens=2048,
        )
    return _llm_instance


class SimpleConversationalChain:
    """
    Conversational RAG chain with two-step retrieval:
    1. Condense follow-up questions using chat history
    2. Retrieve context and answer using the condensed question
    """

    def __init__(self, llm: ChatAnthropic, memory_window: int = 10):
        self.llm = llm
        self.memory_window = memory_window
        self._history: list[dict] = []

    def _format_history(self) -> str:
        if not self._history:
            return ""
        lines = []
        for turn in self._history[-self.memory_window:]:
            lines.append(f"Human: {turn['question']}")
            lines.append(f"Assistant: {turn['answer']}")
        return "\n".join(lines)

    def _invoke_llm(self, prompt: str) -> str:
        response = self.llm.invoke([HumanMessage(content=prompt)])
        return response.content.strip()

    def _condense_question(self, question: str) -> str:
        history = self._format_history()
        if not history:
            return question
        condensed = self._invoke_llm(
            CONDENSE_QUESTION_PROMPT.format(
                chat_history=history,
                question=question,
            )
        )
        return condensed if condensed else question

    @staticmethod
    def _build_labeled_context(docs: list[Document]) -> str:
        """Group retrieved docs by source_type and add clear section headers."""
        groups: dict[str, list[Document]] = defaultdict(list)
        for doc in docs:
            source_type = doc.metadata.get("source_type", "unknown")
            groups[source_type].append(doc)

        sections: list[str] = []
        for source_type in sorted(groups):
            label = _SOURCE_LABELS.get(
                source_type,
                source_type.replace("_", " ").title(),
            )
            chunks_text = []
            for doc in groups[source_type]:
                cat = doc.metadata.get("category", "")
                prefix = f"[{cat}] " if cat else ""
                chunks_text.append(f"{prefix}{doc.page_content}")
            section_body = "\n\n".join(chunks_text)
            sections.append(f"### [{label}]\n{section_body}")

        return "\n\n---\n\n".join(sections)

    def invoke(self, inputs: dict) -> dict:
        question = inputs["question"]
        standalone_question = self._condense_question(question)
        logger.debug("Original: %r → Standalone: %r", question, standalone_question)

        docs = search(standalone_question, k=max(config.TOP_K_RESULTS, 12))
        context = self._build_labeled_context(docs)

        prompt_text = QA_PROMPT.format(context=context, question=standalone_question)
        answer = self._invoke_llm(prompt_text)

        self._history.append({"question": question, "answer": answer})
        if len(self._history) > self.memory_window:
            self._history = self._history[-self.memory_window:]

        return {"answer": answer, "source_documents": docs}


def build_chain(memory=None) -> SimpleConversationalChain:
    """Build and return a conversational RAG chain backed by ChromaDB."""
    llm = get_llm()
    return SimpleConversationalChain(llm=llm, memory_window=config.MEMORY_WINDOW)


def ask(question: str, chain: SimpleConversationalChain) -> dict:
    """Ask a question and return the answer with deduplicated source URLs."""
    result = chain.invoke({"question": question})
    source_docs: list[Document] = result.get("source_documents", [])
    sources = list(
        {
            doc.metadata.get("source_url", doc.metadata.get("source", "Unknown"))
            for doc in source_docs
        }
    )
    return {"answer": result["answer"], "sources": sources}
