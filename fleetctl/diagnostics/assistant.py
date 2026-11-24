"""RAG diagnostics assistant: retrieve device logs + runbook entries, then ask an LLM.

Pipeline:  question -> retrieve (device logs filtered by device, fleet-wide ERRORs,
runbook chunks) -> grounded prompt with numbered evidence -> LLM answer citing [n].

The LLM is any OpenAI-compatible chat endpoint (LLM_API_KEY / LLM_BASE_URL / LLM_MODEL).
Without a key it falls back to a deterministic summary of the retrieved evidence, so the
feature degrades gracefully instead of failing — and tests stay hermetic.
"""
from __future__ import annotations

import logging
import os
from collections import Counter
from pathlib import Path

import httpx

from .vectorstore import Doc, VectorStore

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are an edge-fleet reliability engineer. Answer ONLY from the numbered evidence. "
    "Cite evidence as [n]. Give: 1) most likely root cause, 2) affected scope, "
    "3) next debugging step or remediation. If the evidence is insufficient, say so."
)


class DiagnosticsAssistant:
    def __init__(self, kb_dir: str | Path | None = None, llm_client=None) -> None:
        self.logs = VectorStore()
        self.kb = VectorStore()
        self._n = 0
        self.llm = llm_client if llm_client is not None else OpenAICompatibleLLM.from_env()
        if kb_dir:
            self.load_runbooks(kb_dir)

    # ---- indexing ----
    def load_runbooks(self, kb_dir: str | Path) -> None:
        for path in sorted(Path(kb_dir).glob("*.md")):
            for i, chunk in enumerate(c.strip() for c in path.read_text().split("\n## ") if c.strip()):
                self.kb.add(Doc(f"{path.stem}#{i}", chunk, {"source": path.name}))

    def ingest_log(self, entry: dict) -> None:
        self._n += 1
        text = f"{entry.get('level')} {entry.get('code')} {entry.get('msg')} hw={entry.get('hw_class')} " \
               f"fw={entry.get('firmware')}"
        self.logs.add(Doc(f"log-{self._n}", text, {k: entry.get(k) for k in
                                                   ("device_id", "level", "code", "hw_class", "firmware", "ts")}))

    # ---- query ----
    def retrieve(self, question: str, device_id: str | None = None, k: int = 6) -> list[Doc]:
        hits: list[Doc] = []
        if device_id:
            hits += [d for _, d in self.logs.search(question, k, where={"device_id": device_id})]
        hits += [d for _, d in self.logs.search(question, k, where={"level": "ERROR"})]
        hits += [d for _, d in self.kb.search(question, 3)]
        seen, out = set(), []
        for d in hits:
            if d.doc_id not in seen:
                seen.add(d.doc_id)
                out.append(d)
        return out

    def ask(self, question: str, device_id: str | None = None, k: int = 6) -> dict:
        evidence = self.retrieve(question, device_id, k)
        ctx = "\n".join(f"[{i + 1}] ({d.meta.get('device_id') or d.meta.get('source')}) {d.text[:400]}"
                        for i, d in enumerate(evidence))
        prompt = f"Question: {question}\nDevice: {device_id or 'fleet-wide'}\n\nEvidence:\n{ctx}"
        answer, mode = None, "offline"
        if self.llm and evidence:
            try:
                answer, mode = self.llm.complete(SYSTEM_PROMPT, prompt), "llm"
            except Exception as e:  # network/LLM failure must not break diagnostics
                log.warning("LLM call failed, using offline summary: %s", e)
        if answer is None:
            answer = self._offline_summary(evidence, device_id)
        return {"answer": answer, "mode": mode,
                "evidence": [{"ref": i + 1, "id": d.doc_id, "text": d.text, "meta": d.meta}
                             for i, d in enumerate(evidence)]}

    @staticmethod
    def _offline_summary(evidence: list[Doc], device_id: str | None = None) -> str:
        logs = [d for d in evidence if "device_id" in d.meta]
        own = [d for d in logs if d.meta["device_id"] == device_id]
        logs = own or logs  # the asked-about device's own logs come first
        if not logs:
            return "No matching device logs found."
        # ERRORs outrank WARNs: retries/throttling are symptoms, the ERROR is usually the cause.
        codes = Counter(d.meta["code"] for d in logs if d.meta.get("level") == "ERROR") or \
            Counter(d.meta["code"] for d in logs if d.meta.get("level") == "WARN")
        top = codes.most_common(1)[0][0] if codes else logs[0].meta.get("code")
        hw = Counter(d.meta.get("hw_class") for d in logs if d.meta.get("code") == top)
        fw = Counter(d.meta.get("firmware") for d in logs if d.meta.get("code") == top)
        runbook = next((i + 1 for i, d in enumerate(evidence) if "source" in d.meta and top and
                        top.lower() in d.text.lower()), None)
        refs = ", ".join(f"[{i + 1}]" for i, d in enumerate(evidence) if d.meta.get("code") == top)
        msg = (f"Most frequent failure signal: {top} {refs}. Seen on hw: {dict(hw)}; firmware: {dict(fw)}.")
        if runbook:
            msg += f" See runbook [{runbook}] for remediation."
        return msg


class OpenAICompatibleLLM:
    def __init__(self, api_key: str, base_url: str, model: str, timeout: float = 30.0) -> None:
        self.api_key, self.base_url, self.model, self.timeout = api_key, base_url.rstrip("/"), model, timeout

    @classmethod
    def from_env(cls):
        key = os.getenv("LLM_API_KEY")
        if not key:
            return None
        return cls(key, os.getenv("LLM_BASE_URL", "https://api.openai.com/v1"), os.getenv("LLM_MODEL", "gpt-4o-mini"))

    def complete(self, system: str, user: str) -> str:
        r = httpx.post(f"{self.base_url}/chat/completions", timeout=self.timeout,
                       headers={"Authorization": f"Bearer {self.api_key}"},
                       json={"model": self.model, "temperature": 0.1,
                             "messages": [{"role": "system", "content": system},
                                          {"role": "user", "content": user}]})
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
